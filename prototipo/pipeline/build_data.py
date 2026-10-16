"""Pipeline de dados do protótipo Hidro Congo.

Baixa dados abertos leves, deriva covariáveis por célula da grade e grava os
arquivos que a API e o frontend consomem em ../data/.

Fontes reais:
  * Natural Earth 10m (limites dos departamentos, rios principais, estradas,
    cidades, lagos)                     -> github.com/nvkelso/natural-earth-vector
  * AWS Terrain Tiles (Terrarium, derivado de SRTM/NASADEM) -> elevação, declive,
    curvatura e rede de drenagem derivada (D8 + priority-flood, estilo HydroSHEDS)
  * NOAA GHCN-Daily (estações do Congo e vizinhos) -> normais mensais de
    precipitação e temperatura (PET por Hargreaves)

Covariáveis sintéticas (marcadas em meta.json): domínios hidrogeológicos,
permeabilidade do solo, NDVI, vazões de literatura dos grandes rios.

Uso:
    python pipeline/build_data.py            # baixa (com cache) e gera data/
"""
from __future__ import annotations

import csv
import heapq
import io
import json
import math
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import LineString, MultiLineString, Point, box, mapping, shape
from shapely.ops import linemerge, unary_union
from shapely.strtree import STRtree

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data"
CACHE = Path(os.environ.get("HIDRO_CACHE", ROOT / ".cache"))

NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
GHCN = "https://noaa-ghcn-pds.s3.amazonaws.com/"

# Área de processamento (um pouco maior que o país para não truncar bacias)
BBOX = (10.5, -6.0, 19.5, 4.5)
CELL_DEG = 0.2          # ~22 km
DEM_ZOOM = 7            # ~1,2 km por pixel
STREAM_MIN_KM2 = 50     # limiar de drenagem (densidade / distância à drenagem)
RIVER_MIN_KM2 = 500     # limiar de "rio bombeável" derivado do DEM
BUDYKO_W = 3.5          # calibrado para escoamento ~350-400 mm/ano na bacia do Congo

# Vazões médias anuais aproximadas (m³/s), valores de literatura (GRDC / ORE-HYBAM).
# Regime: amortecimento da sazonalidade (0 = constante, 1 = segue a chuva local).
MAJOR_RIVERS = {
    "Congo": (40500, 0.25), "Ubangi": (4000, 0.5), "Sangha": (1600, 0.45),
    "Kouilou": (900, 0.6), "Niari": (450, 0.65), "Alima": (600, 0.4),
    "Lobaye": (300, 0.6), "Dja": (400, 0.55), "Kadéï": (300, 0.6),
    "Inkisi": (250, 0.6), "Kasai": (9900, 0.35), "Ogooué": (4700, 0.5),
}

M_DAYS = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def fetch(url: str, name: str) -> bytes:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / name
    if not p.exists():
        log("baixando", url)
        with urllib.request.urlopen(url, timeout=120) as r:
            p.write_bytes(r.read())
    return p.read_bytes()


def ne(name: str) -> dict:
    return json.loads(fetch(NE_BASE + name + ".geojson", name + ".geojson"))


# ----------------------------------------------------------------------------
# Geometria auxiliar
# ----------------------------------------------------------------------------
def km_per_deg(lat: float) -> tuple[float, float]:
    return 111.32 * math.cos(math.radians(lat)), 110.57


def to_local_km(geom, lat0: float):
    """Projeção equiretangular simples centrada no país (erro < 1% aqui)."""
    from shapely.affinity import scale
    kx, ky = km_per_deg(lat0)
    return scale(geom, xfact=kx, yfact=ky, origin=(0, 0))


def rnd(geom, nd=3):
    def r(c):
        if isinstance(c, (list, tuple)) and c and isinstance(c[0], (int, float)):
            return [round(c[0], nd), round(c[1], nd)]
        return [r(x) for x in c]
    g = mapping(geom)
    return {"type": g["type"], "coordinates": r(g["coordinates"])}


# ----------------------------------------------------------------------------
# DEM: mosaico Terrarium + priority-flood + D8
# ----------------------------------------------------------------------------
def tile_xy(lon, lat, z):
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def load_dem():
    z = DEM_ZOOM
    x0, y0 = tile_xy(BBOX[0], BBOX[3], z)
    x1, y1 = tile_xy(BBOX[2], BBOX[1], z)
    rows = []
    for ty in range(y0, y1 + 1):
        row = []
        for tx in range(x0, x1 + 1):
            png = fetch(TERRARIUM.format(z=z, x=tx, y=ty), f"t{z}_{tx}_{ty}.png")
            a = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.float64)
            row.append(a[..., 0] * 256 + a[..., 1] + a[..., 2] / 256 - 32768)
        rows.append(np.hstack(row))
    dem = np.vstack(rows)
    n = 2 ** z
    H, W = dem.shape
    px = (np.arange(W) + 0.5) / 256 + x0
    py = (np.arange(H) + 0.5) / 256 + y0
    lons = px / n * 360 - 180
    lats = np.degrees(np.arctan(np.sinh(math.pi * (1 - 2 * py / n))))
    cmask = (lons >= BBOX[0]) & (lons <= BBOX[2])
    rmask = (lats >= BBOX[1]) & (lats <= BBOX[3])
    return dem[np.ix_(rmask, cmask)], lons[cmask], lats[rmask]


def hydrology(dem, lons, lats, runoff_mm):
    """Priority-flood (Barnes 2014) dá direção de fluxo e ordem topológica."""
    H, W = dem.shape
    N = H * W
    flat = dem.ravel()
    ocean = flat <= 0
    filled = flat.copy()
    down = np.full(N, -1, dtype=np.int64)
    seen = np.zeros(N, dtype=bool)
    heap = []
    for i in range(N):
        r, c = divmod(i, W)
        if ocean[i] or r == 0 or c == 0 or r == H - 1 or c == W - 1:
            heapq.heappush(heap, (filled[i], i))
            seen[i] = True
    order = []
    nbr = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    while heap:
        e, i = heapq.heappop(heap)
        order.append(i)
        r, c = divmod(i, W)
        for dr, dc in nbr:
            rr, cc = r + dr, c + dc
            if 0 <= rr < H and 0 <= cc < W:
                j = rr * W + cc
                if not seen[j]:
                    seen[j] = True
                    filled[j] = max(filled[j], e + 1e-4)
                    down[j] = i
                    heapq.heappush(heap, (filled[j], j))
    order = np.array(order)
    # área de cada pixel (km²)
    dx = abs(lons[1] - lons[0])
    dys = np.abs(np.gradient(lats))
    area_row = dx * 111.32 * np.cos(np.radians(lats)) * dys * 110.57
    area = np.repeat(area_row, W)
    acc = area.copy()
    vol = runoff_mm.ravel() / 1000 * area * 1e6  # m³/ano
    for i in order[::-1]:
        d = down[i]
        if d >= 0:
            acc[d] += acc[i]
            vol[d] += vol[i]
    q = vol / (365.25 * 86400)
    return acc.reshape(H, W), q.reshape(H, W), down.reshape(H, W), ocean.reshape(H, W)


def stream_lines(acc, down, lons, lats, thr):
    H, W = acc.shape
    segs = []
    idx = np.flatnonzero(acc.ravel() >= thr)
    for i in idx:
        d = down.ravel()[i]
        if d < 0:
            continue
        r, c = divmod(int(i), W)
        rd, cd = divmod(int(d), W)
        segs.append(((lons[c], lats[r]), (lons[cd], lats[rd])))
    merged = linemerge(MultiLineString(segs))
    return merged


# ----------------------------------------------------------------------------
# Clima: GHCN-Daily -> normais mensais
# ----------------------------------------------------------------------------
def ra_mm_day(lat_deg, month):
    """Radiação extraterrestre (FAO-56) em mm/dia equivalentes, dia 15 do mês."""
    j = sum(M_DAYS[:month]) + 15
    phi = math.radians(lat_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi * j / 365)
    dec = 0.409 * math.sin(2 * math.pi * j / 365 - 1.39)
    ws = math.acos(max(-1, min(1, -math.tan(phi) * math.tan(dec))))
    ra = 24 * 60 / math.pi * 0.0820 * dr * (ws * math.sin(phi) * math.sin(dec) +
                                              math.cos(phi) * math.cos(dec) * math.sin(ws))
    return ra * 0.408


def load_stations():
    txt = fetch(GHCN + "ghcnd-stations.txt", "ghcnd-stations.txt").decode()
    out = []
    for line in txt.splitlines():
        lat, lon = float(line[12:20]), float(line[21:30])
        if BBOX[1] - 0.5 <= lat <= BBOX[3] + 0.5 and BBOX[0] - 0.5 <= lon <= BBOX[2] + 0.5:
            sid = line[:11]
            try:
                raw = fetch(GHCN + f"csv/by_station/{sid}.csv", f"ghcn_{sid}.csv").decode()
            except Exception as e:  # estação sem arquivo
                log("sem dados", sid, e)
                continue
            m = defaultdict(lambda: defaultdict(list))
            for r in csv.DictReader(io.StringIO(raw)):
                if r["Q_FLAG"] or r["ELEMENT"] not in ("PRCP", "TMAX", "TMIN"):
                    continue
                m[r["ELEMENT"]][r["DATE"][:6]].append(float(r["DATA_VALUE"]) / 10)
            prcp = [[] for _ in range(12)]
            for ym, v in m["PRCP"].items():
                if len(v) >= 25:  # mês quase completo
                    mo = int(ym[4:]) - 1
                    prcp[mo].append(sum(v) / len(v) * M_DAYS[mo])
            if min(len(p) for p in prcp) < 8:
                continue
            tx = [[] for _ in range(12)]
            tn = [[] for _ in range(12)]
            for el, dst in (("TMAX", tx), ("TMIN", tn)):
                for ym, v in m[el].items():
                    if len(v) >= 15:
                        dst[int(ym[4:]) - 1].append(sum(v) / len(v))
            st = {
                "id": sid, "name": line[41:71].strip().title(), "lat": lat, "lon": lon,
                "elev": float(line[31:37]),
                "prcp": [round(float(np.mean(p)), 1) for p in prcp],
                "n_months": sum(len(p) for p in prcp),
            }
            if min(len(t) for t in tx) >= 5 and min(len(t) for t in tn) >= 5:
                st["tmax"] = [float(np.mean(t)) for t in tx]
                st["tmin"] = [float(np.mean(t)) for t in tn]
            out.append(st)
    log(len(out), "estações com normais válidas")
    return out


def idw(stations, key, lat, lon, k=6, p=2.0, elev=None):
    cand = [s for s in stations if key in s]
    d = []
    for s in cand:
        kx, ky = km_per_deg((lat + s["lat"]) / 2)
        dist = math.hypot((lon - s["lon"]) * kx, (lat - s["lat"]) * ky)
        d.append((max(dist, 1.0), s))
    d.sort(key=lambda t: t[0])
    d = d[:k]
    wsum = sum(1 / di ** p for di, _ in d)
    res = np.zeros(12)
    for di, s in d:
        v = np.array(s[key], dtype=float)
        if elev is not None:  # correção de temperatura por gradiente adiabático
            v = v - 0.0065 * (elev - s["elev"])
        res += v / di ** p
    return res / wsum, d[0][0]


def hargreaves_month(tmax, tmin, lat):
    out = []
    for m in range(12):
        tm = (tmax[m] + tmin[m]) / 2
        et0 = 0.0023 * ra_mm_day(lat, m) * (tm + 17.8) * math.sqrt(max(tmax[m] - tmin[m], 0.1))
        out.append(et0 * M_DAYS[m])
    return np.array(out)


def budyko_et(p, pet, w=BUDYKO_W):
    phi = pet / max(p, 1)
    return p * (1 + phi - (1 + phi ** w) ** (1 / w))


# ----------------------------------------------------------------------------
# Domínios hidrogeológicos (SINTÉTICO, aproximação a partir do Africa
# Groundwater Atlas / MacDonald et al. 2012 e literatura da Cuvette/Pointe-Noire)
# ----------------------------------------------------------------------------
ZONES = {
    # nome: (favorabilidade litológica, permeabilidade do solo, vazão por poço L/s, WTD base m)
    "costeira": ("Bacia sedimentar costeira", 0.75, 0.70, 3.0, 12.0),
    "mayombe": ("Mayombe (cristalino fraturado)", 0.30, 0.45, 0.5, 15.0),
    "niari": ("Vale do Niari (calcários/karst)", 0.60, 0.55, 2.0, 18.0),
    "chaillu": ("Maciço do Chaillu (embasamento)", 0.30, 0.45, 0.5, 14.0),
    "bateke": ("Planaltos Batéké (areias)", 0.65, 0.90, 2.5, 30.0),
    "cuvette": ("Bacia da Cuvette (sedimentar quaternária)", 0.85, 0.50, 4.0, 4.0),
    "noroeste": ("Embasamento do noroeste", 0.35, 0.50, 0.7, 12.0),
}


def zone_of(lon, lat, dist_ocean_km):
    if dist_ocean_km < 45:
        return "costeira"
    if dist_ocean_km < 95 and lat < -2.4:
        return "mayombe"
    if 12.6 <= lon <= 14.4 and -4.8 <= lat <= -3.45:
        return "niari"
    if lon < 14.1 and -3.45 < lat <= -1.4:
        return "chaillu"
    if 14.1 <= lon <= 16.4 and -4.9 <= lat <= -1.4:
        return "bateke"
    if lon < 14.9 + max(0.0, lat) * 0.25 and lat > -1.4:
        return "noroeste"
    return "cuvette"


# ----------------------------------------------------------------------------
def main():
    OUT.mkdir(parents=True, exist_ok=True)

    log("1/6 limites e camadas vetoriais (Natural Earth)")
    adm1 = [f for f in ne("ne_10m_admin_1_states_provinces")["features"]
            if f["properties"].get("adm0_a3") == "COG"]
    depts = []
    for f in adm1:
        g = shape(f["geometry"]).buffer(0)
        depts.append((f["properties"]["name"].replace("Pointe Noire", "Pointe-Noire"), g))
    # O polígono "Brazzaville" do Natural Earth (~6.000 km²) inclui parte do Pool.
    # Mantemos a comuna (~15 km ao redor do centro) e devolvemos o resto ao Pool.
    dd = dict(depts)
    bzv_city = Point(15.27, -4.26).buffer(0.14).intersection(dd["Brazzaville"])
    dd["Pool"] = dd["Pool"].union(dd["Brazzaville"].difference(bzv_city)).buffer(0)
    dd["Brazzaville"] = bzv_city
    depts = list(dd.items())
    country = unary_union([g for _, g in depts])
    lat0 = country.centroid.y

    countries = ne("ne_10m_admin_0_countries")["features"]
    land = unary_union([shape(f["geometry"]).buffer(0) for f in countries
                        if shape(f["geometry"]).intersects(box(*BBOX).buffer(2))])
    coast = land.boundary.intersection(box(*BBOX).buffer(1))
    ocean_poly = box(*BBOX).buffer(1).difference(land)

    bb = box(*BBOX)
    rivers_major = []
    for f in ne("ne_10m_rivers_lake_centerlines")["features"]:
        nm = f["properties"].get("name")
        g = shape(f["geometry"])
        if nm in MAJOR_RIVERS and g.intersects(bb):
            rivers_major.append((nm, g.intersection(bb)))
    roads = [shape(f["geometry"]) for f in ne("ne_10m_roads")["features"]
             if shape(f["geometry"]).intersects(bb)]
    places = [f for f in ne("ne_10m_populated_places_simple")["features"]
              if shape(f["geometry"]).intersects(bb)]
    cities = [{"name": f["properties"]["name"].replace("Loubomo", "Dolisie"),
               "pop": int(f["properties"]["pop_max"]),
               "lon": f["geometry"]["coordinates"][0], "lat": f["geometry"]["coordinates"][1],
               "country": f["properties"]["adm0_a3"]} for f in places]

    log("2/6 normais climáticas (GHCN-Daily)")
    stations = load_stations()

    log("3/6 DEM (Terrarium/SRTM)")
    dem, lons, lats = load_dem()
    H, W = dem.shape
    log("  DEM", dem.shape)

    # campo de escoamento coarse para acumular vazão (Budyko) em grade 0.5°
    log("4/6 escoamento e rede de drenagem (priority-flood + D8)")
    runoff = np.zeros_like(dem)
    step = 40
    for r0 in range(0, H, step):
        for c0 in range(0, W, step):
            la, lo = lats[min(r0 + step // 2, H - 1)], lons[min(c0 + step // 2, W - 1)]
            el = float(np.median(dem[r0:r0 + step, c0:c0 + step]))
            pm, _ = idw(stations, "prcp", la, lo)
            tx, _ = idw(stations, "tmax", la, lo, elev=el)
            tn, _ = idw(stations, "tmin", la, lo, elev=el)
            pet = hargreaves_month(tx, tn, la).sum()
            p = pm.sum()
            runoff[r0:r0 + step, c0:c0 + step] = p - budyko_et(p, pet)
    acc, qdem, down, ocean = hydrology(dem, lons, lats, runoff)
    streams_small = stream_lines(acc, down, lons, lats, STREAM_MIN_KM2)
    streams_big = stream_lines(acc, down, lons, lats, RIVER_MIN_KM2)
    log("  drenagem ok")

    # pixels de rio (para distância/vazão/elevação da lâmina d'água)
    rpix = np.argwhere((acc >= RIVER_MIN_KM2) & ~ocean)
    rp_lon = lons[rpix[:, 1]]
    rp_lat = lats[rpix[:, 0]]
    rp_q = qdem[rpix[:, 0], rpix[:, 1]]
    rp_z = dem[rpix[:, 0], rpix[:, 1]]
    rp_acc = acc[rpix[:, 0], rpix[:, 1]]
    spix = np.argwhere((acc >= STREAM_MIN_KM2) & ~ocean)
    sp_lon, sp_lat = lons[spix[:, 1]], lats[spix[:, 0]]
    sp_z = dem[spix[:, 0], spix[:, 1]]
    kx0, ky0 = km_per_deg(lat0)

    # pontos densos ao longo dos grandes rios
    major_pts = []
    for nm, g in rivers_major:
        geoms = getattr(g, "geoms", [g])
        for ln in geoms:
            if ln.length == 0:
                continue
            n = max(2, int(ln.length / 0.02))
            for k in range(n + 1):
                p = ln.interpolate(k / n, normalized=True)
                major_pts.append((nm, p.x, p.y))
    mp_lon = np.array([p[1] for p in major_pts])
    mp_lat = np.array([p[2] for p in major_pts])
    mp_name = [p[0] for p in major_pts]

    def dem_at(lon, lat, win=1):
        c = int(np.clip(np.searchsorted(lons, lon), 0, W - 1))
        r = int(np.clip(np.argmin(np.abs(lats - lat)), 0, H - 1))
        return float(dem[max(0, r - win):r + win + 1, max(0, c - win):c + win + 1].min())

    log("5/6 grade de células e covariáveis")
    small_tree_geoms = list(getattr(streams_small, "geoms", [streams_small]))
    small_tree = STRtree(small_tree_geoms)
    road_tree = STRtree(roads)
    country_local_cities = [c for c in cities if c["pop"] >= 8000]

    cells = []
    gx = np.arange(BBOX[0], BBOX[2], CELL_DEG)
    gy = np.arange(BBOX[1], BBOX[3], CELL_DEG)
    cid = 0
    for y in gy:
        for x in gx:
            sq = box(x, y, x + CELL_DEG, y + CELL_DEG)
            if not sq.intersects(country):
                continue
            part = sq.intersection(country)
            small_dept = any(n in ("Pointe-Noire", "Brazzaville") and g.intersection(sq).area > 0.003
                             for n, g in depts)
            if part.area < 0.15 * sq.area and not small_dept:
                continue
            c = part.representative_point() if not part.contains(part.centroid) else part.centroid
            lon, lat = c.x, c.y
            shares = sorted(((d[1].intersection(part).area / part.area, d[0]) for d in depts), reverse=True)
            dept = shares[0][1]
            dept_list = [n for a, n in shares if a >= 0.05 or n == dept]
            # DEM na célula
            cm = (lons >= x) & (lons < x + CELL_DEG)
            rm = (lats >= y) & (lats < y + CELL_DEG)
            sub = dem[np.ix_(rm, cm)]
            if sub.size == 0:
                continue
            kx, ky = km_per_deg(lat)
            px_x = abs(lons[1] - lons[0]) * kx * 1000
            px_y = abs(lats[1] - lats[0]) * ky * 1000
            gy_, gx_ = np.gradient(sub, px_y, px_x)
            slope = float(np.mean(np.hypot(gx_, gy_)) * 100)
            lap = np.gradient(gx_, px_x, axis=1) + np.gradient(gy_, px_y, axis=0)
            curv = float(np.mean(lap) * 1e5)  # 1/100 km
            elev = float(np.mean(sub))
            # ponto de implantação: terço baixo do relevo da célula (onde ficam
            # as lavouras e a captação), usado para HAND e desnível ao rio
            e_low = float(np.percentile(sub, 25))
            # drenagem
            pt = Point(lon, lat)
            near = small_tree.nearest(pt)
            ns = small_tree_geoms[near]
            d_d = to_local_km(LineString([pt, ns.interpolate(ns.project(pt))]), lat0).length
            clip = [small_tree_geoms[i].intersection(sq) for i in small_tree.query(sq)]
            dens = sum(to_local_km(g, lat0).length for g in clip) / (to_local_km(sq, lat0).area)
            # rio derivado do DEM mais próximo (melhor relação vazão/distância)
            dk = np.hypot((rp_lon - lon) * kx0, (rp_lat - lat) * ky0)
            within = np.flatnonzero(dk <= max(25.0, float(dk.min()) + 1))
            j = int(within[np.argmax(rp_q[within] / (1 + dk[within]))])
            stream = {"d_km": round(float(dk[j]), 2), "q": round(float(rp_q[j]), 2),
                      "z": round(float(rp_z[j]), 1), "acc_km2": round(float(rp_acc[j]))}
            # HAND: altura acima da drenagem mais próxima (limiar pequeno)
            ds = np.hypot((sp_lon - lon) * kx0, (sp_lat - lat) * ky0)
            near_s = np.argsort(ds)[:5]
            hand = max(0.0, e_low - float(np.min(sp_z[near_s])))
            # grande rio (Natural Earth + vazão de literatura)
            dm = np.hypot((mp_lon - lon) * kx0, (mp_lat - lat) * ky0)
            m = int(np.argmin(dm))
            nm = mp_name[m]
            major = {"name": nm, "d_km": round(float(dm[m]), 2), "q": MAJOR_RIVERS[nm][0],
                     "regime": MAJOR_RIVERS[nm][1], "z": round(dem_at(mp_lon[m], mp_lat[m]), 1)}
            # clima
            pm, d_st = idw(stations, "prcp", lat, lon)
            tx, _ = idw(stations, "tmax", lat, lon, elev=elev)
            tn, _ = idw(stations, "tmin", lat, lon, elev=elev)
            pet_m = hargreaves_month(tx, tn, lat)
            p, pet = float(pm.sum()), float(pet_m.sum())
            et = budyko_et(p, pet)
            # acesso
            rd = road_tree.nearest(pt)
            d_road = to_local_km(LineString([pt, roads[rd].interpolate(roads[rd].project(pt))]), lat0).length
            dtown = min(country_local_cities, key=lambda cc: (cc["lon"] - lon) ** 2 + (cc["lat"] - lat) ** 2)
            d_town = math.hypot((dtown["lon"] - lon) * kx, (dtown["lat"] - lat) * ky)
            pop_idx = sum(cc["pop"] / (1 + (math.hypot((cc["lon"] - lon) * kx, (cc["lat"] - lat) * ky) / 30) ** 2)
                          for cc in cities)
            d_ocean = to_local_km(LineString([pt, coast.interpolate(coast.project(pt))]), lat0).length \
                if not ocean_poly.contains(pt) else 0.0
            z = zone_of(lon, lat, d_ocean)
            peat = z == "cuvette" and lat > -0.6 and lon > 16.4  # turfeiras da Likouala/Cuvette (aprox.)
            ndvi = float(np.clip((p - 800) / 1400, 0.35, 0.9))  # SINTÉTICO (proxy por chuva)
            cells.append({
                "id": cid, "dept": dept, "depts": dept_list, "lon": round(lon, 4), "lat": round(lat, 4),
                "geom": part.simplify(0.005),
                "E": round(elev, 1), "E_low": round(e_low, 1), "S": round(slope, 2), "C": round(curv, 3), "HAND": round(hand, 1),
                "D_d_km": round(d_d, 2), "dens": round(dens, 3),
                "P": round(p), "P_m": [round(float(v), 1) for v in pm],
                "PET": round(pet), "PET_m": [round(float(v), 1) for v in pet_m], "ET": round(et),
                "zone": z, "lito": ZONES[z][1], "So": ZONES[z][2], "yield_Ls": ZONES[z][3],
                "WTD0": ZONES[z][4], "NDVI": round(ndvi, 2), "peat": peat,
                "stream": stream, "major": major,
                "D_road_km": round(d_road, 2), "D_town_km": round(d_town, 1), "town": dtown["name"],
                "pop_idx": round(pop_idx), "D_station_km": round(d_st, 1),
            })
            cid += 1
    log(" ", len(cells), "células")

    log("6/6 gravando arquivos")
    fc = {"type": "FeatureCollection", "features": []}
    for c in cells:
        g = c.pop("geom")
        fc["features"].append({"type": "Feature", "geometry": rnd(g), "properties": c})
    (OUT / "cells.geojson").write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")))

    dfc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": n}, "geometry": rnd(g.simplify(0.01))} for n, g in depts]}
    (OUT / "departments.geojson").write_text(json.dumps(dfc, ensure_ascii=False, separators=(",", ":")))

    clipbox = country.buffer(0.6)
    rv = []
    for nm, g in rivers_major:
        gg = g.intersection(clipbox)
        if not gg.is_empty:
            rv.append({"type": "Feature", "properties": {"name": nm, "kind": "major", "q": MAJOR_RIVERS[nm][0]},
                       "geometry": rnd(gg.simplify(0.01))})
    sb = streams_big.intersection(clipbox).simplify(0.01)
    rv.append({"type": "Feature", "properties": {"name": "Drenagem derivada do DEM", "kind": "dem"},
               "geometry": rnd(sb)})
    (OUT / "rivers.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": rv},
                                                   ensure_ascii=False, separators=(",", ":")))
    cfc = [c for c in cities if country.buffer(0.05).contains(Point(c["lon"], c["lat"]))]
    (OUT / "cities.json").write_text(json.dumps(cfc, ensure_ascii=False))
    (OUT / "stations.json").write_text(json.dumps(
        [{k: s[k] for k in ("id", "name", "lat", "lon", "elev", "n_months")} | {"P": round(sum(s["prcp"]))}
         for s in stations], ensure_ascii=False))
    meta = {
        "cell_deg": CELL_DEG, "bbox": BBOX, "n_cells": len(cells),
        "zones": {k: v[0] for k, v in ZONES.items()},
        "sources": {
            "real": [
                "Natural Earth 10m: departamentos, rios principais, estradas, cidades",
                "AWS Terrain Tiles (Terrarium; SRTM/NASADEM): elevação, declive, curvatura",
                "Rede de drenagem e vazão derivadas do DEM (priority-flood + D8, escoamento por Budyko)",
                f"NOAA GHCN-Daily: normais mensais de chuva e temperatura de {len(stations)} estações",
                "PET por Hargreaves (FAO-56) a partir das temperaturas das estações",
            ],
            "synthetic": [
                "Domínios hidrogeológicos, favorabilidade litológica e vazão por poço (aprox. Africa Groundwater Atlas / MacDonald 2012)",
                "Permeabilidade do solo por domínio (no lugar do SoilGrids)",
                "NDVI estimado a partir da chuva (no lugar do Sentinel/MODIS)",
                "Coeficientes β da regressão de WTD (sem poços de calibração)",
                "Vazões médias dos grandes rios: valores aproximados de literatura",
                "Índice populacional por gravidade das cidades (no lugar do WorldPop)",
                "Turfeiras da Cuvette/Likouala: área aproximada",
            ],
        },
    }
    (OUT / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
    log("pronto:", OUT)


if __name__ == "__main__":
    main()
