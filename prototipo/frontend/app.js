/* Hidro Congo: frontend.
 * Modo API: dados e cálculos vêm do FastAPI (/api/...).
 * Modo estático: window.HIDRO_DATA embutido e cálculos por model.js. */
(function () {
  "use strict";
  const M = window.HidroModel;
  const STATIC = window.HIDRO_DATA || null;
  const CLS = {
    poco_alto: { label: "Borehole highly recommended", short: "Borehole (high)", v: "--c-poco-alto" },
    poco_viavel: { label: "Borehole viable", short: "Borehole viable", v: "--c-poco-viavel" },
    rio: { label: "River pumping", short: "River", v: "--c-rio" },
    inconclusivo: { label: "Inconclusive (field study)", short: "Inconclusive", v: "--c-inconclusivo" },
    nao_recomendado: { label: "Not recommended", short: "Not recommended", v: "--c-nao" },
  };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const ZONE_TXT = {
    costeira: "Moanda coastal basin", kongo_central: "Kongo Central (Mayombe and limestones)",
    rift: "Albertine/Kivu rift (volcanic)", cuvette: "Cuvette Centrale (sedimentary)",
    kalahari: "Kalahari/Batéké plateaus (sands)", kasai: "Kasai craton (basement)",
    katanga: "Katanga (metasediments/dolomites)", embasamento: "northern and eastern basement",
  };
  const PARAM_IDS = ["horizon_months", "start_month", "area_ha", "c_energia", "WTD_max", "D_max_km", "w1", "w2", "w3", "w4", "w5"];
  const $ = (id) => document.getElementById(id);
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const usd = (x) => "USD " + Math.round(x).toLocaleString("en-US");
  const pct = (x) => Math.round(100 * x) + "%";
  const fmt = (x, d = 0) => Number(x).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });

  const state = { ai: false, layer: "rec", cells: [], cellById: {}, results: [], resById: {}, depts: [], selDept: null, selCell: null, meta: null,
                  res: null, overlay: true, opacity: 0.8, base: "road" };
  let map, cellLayer, deptLayer, deptLabels, selLayer, riverLayer, cityLayer, baseLayer, renderer;
  const AQUIFER_LAYERS = ["zones", "wtd", "yield"];
  const ZONE_ORDER = ["cuvette", "kalahari", "kasai", "katanga", "rift", "kongo_central", "embasamento", "costeira"];
  const q = (url) => url + (url.includes("?") ? "&" : "?") + "res=" + encodeURIComponent(state.res);

  // ---------------------------------------------------------------- dados
  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    if (!r.ok) throw new Error(url + " respondeu " + r.status);
    return r.json();
  }

  async function loadData() {
    if (STATIC) return STATIC;
    const meta = await getJSON("api/meta");
    state.res = Object.keys(meta.resolutions || { [meta.cell_deg]: 1 })[0];
    const [cells, departments, rivers, cities] = await Promise.all([
      getJSON(q("api/cells")), getJSON("api/layers/departments"),
      getJSON("api/layers/rivers"), getJSON("api/layers/cities"),
    ]);
    return { meta, cells, departments, rivers, cities };
  }

  function readParams() {
    const p = {};
    for (const id of PARAM_IDS) p[id] = Number($(id).value);
    return p;
  }

  async function compute() {
    const p = readParams();
    if (STATIC) {
      const pp = M.params(p);
      const full = state.cells.map((c) => M.evaluate(c, pp));
      state.results = full;
      state.depts = M.summarize(state.cells, full);
    } else {
      const r = await getJSON(q("api/recommendations"), {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ params: p }),
      });
      state.results = r.results;
      state.depts = r.departments;
    }
    state.resById = {};
    for (const r of state.results) state.resById[r.id] = r;
  }

  async function cellDetail(id) {
    const p = readParams();
    if (STATIC) {
      const pp = M.params(p);
      const c = state.cellById[id];
      const r = M.evaluate(c, pp);
      return { cell: c, result: r, explanation: explainJS(c, r, pp) };
    }
    return getJSON(q("api/cells/" + id + "/evaluate"), {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ params: p }),
    });
  }

  function explainJS(c, r, p) {
    const rio = r.rio;
    const t = [
      `Domain: ${ZONE_TXT[c.zone] || c.zone}. IR = ${fmt(r.IR, 2)} (above 0.5 favours a borehole).`,
      `Main factor in favour: ${r.fator_pos}. Main limitation: ${r.fator_neg}.`,
      `Best surface intake: ${rio.name}, ${fmt(rio.D_km, 1)} km away with ${fmt(Math.max(rio.dz, 0))} m of lift.`,
    ];
    if (r.turfeira) t.push("Cuvette Centrale peatland area: environmentally sensitive, requires careful permitting.");
    if (!r.vazao_ok) t.push(`The typical borehole yield (${fmt(r.vazao_poco_Ls, 1)} L/s) cannot cover ${fmt(p.area_ha)} ha even with ${r.n_pocos} boreholes.`);
    t.push("Preliminary recommendation: test drilling and a pumping test before construction.");
    return t.join(" ");
  }

  // ---------------------------------------------------------------- cores
  function ramp(t, stops) {
    t = Math.max(0, Math.min(1, t));
    const n = stops.length - 1, i = Math.min(n - 1, Math.floor(t * n)), f = t * n - i;
    const a = stops[i], b = stops[i + 1];
    const h = (s) => [1, 3, 5].map((k) => parseInt(s.slice(k, k + 2), 16));
    const [x, y] = [h(a), h(b)];
    return "#" + x.map((v, k) => Math.round(v + (y[k] - v) * f).toString(16).padStart(2, "0")).join("");
  }
  const PROB_STOPS = ["#f2ead0", "#c9dc8e", "#7fbf6a", "#2f8f4e", "#0d5a32"];
  const CONF_STOPS = ["#f4e3d7", "#e2a37c", "#b8613f", "#7a3020"];
  // rampas sequenciais de um só matiz (claro = raso / pouca vazão; escuro = fundo / muita vazão)
  const WTD_STOPS = ["#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"];
  const WTD_MAX = 60;
  const YIELD_STOPS = ["#fde5d6", "#f5b38f", "#eb6834", "#b54a1d", "#6e2a0e"];
  const YIELD_MAX = 4;

  function cellStyle(feature) {
    const c = feature.properties;
    const r = state.resById[c.id];
    const aquifer = AQUIFER_LAYERS.includes(state.layer);
    if (!r && !aquifer) return { stroke: false, fillOpacity: 0 };
    let fill, op = 1;
    if (state.layer === "rec") fill = css(CLS[r.cls].v);
    else if (state.layer === "prob") {
      if (r.rec === "poco" && r.cls !== "nao_recomendado") fill = ramp(r.P_poco / 0.8, PROB_STOPS);
      else { fill = css("--muted"); op = 0.22; }
    } else if (state.layer === "conf") fill = ramp((r.confianca - 0.35) / 0.6, CONF_STOPS);
    else if (state.layer === "zones") fill = css("--z-" + c.zone);
    else if (state.layer === "wtd") fill = ramp((r ? r.WTD : c.WTD0) / WTD_MAX, WTD_STOPS);
    else fill = ramp(c.yield_Ls / YIELD_MAX, YIELD_STOPS);
    const st = { stroke: true, color: css("--land"), weight: 0.4, dashArray: null, fillColor: fill, fillOpacity: op * state.opacity };
    if (aquifer && c.peat) {  // turfeiras: contorno tracejado, mais leve na grade fina
      const fine = Number(state.res) < 0.3;
      Object.assign(st, { color: css("--ink"), weight: fine ? 0.7 : 1.1, dashArray: fine ? "2 3" : "3 3" });
    }
    return st;
  }

  // ---------------------------------------------------------------- mapa
  function initMap(data) {
    map = L.map("map", { preferCanvas: true, zoomSnap: 0.25, minZoom: 4, maxZoom: 11, attributionControl: true }).setView([-2.9, 23.6], 5);
    map.createPane("basemap").style.zIndex = 150;
    map.createPane("labels").style.zIndex = 450;
    map.getPane("labels").style.pointerEvents = "none";
    applyBase();
    map.attributionControl.addAttribution("geoBoundaries · Natural Earth · SRTM · NOAA GHCN · NASA POWER");

    renderer = L.canvas({ padding: 0.3 });
    buildCellLayer(data.cells);

    riverLayer = L.geoJSON(data.rivers, {
      renderer, interactive: false,
      style: (f) => ({ color: css("--river"), weight: f.properties.kind === "major" ? 2.6 : 0.9, opacity: f.properties.kind === "major" ? 0.95 : 0.7 }),
    }).addTo(map);

    deptLayer = L.geoJSON(data.departments, {
      renderer, interactive: false,
      style: () => ({ color: css("--ink"), weight: 1.1, opacity: 0.55, fill: false }),
    }).addTo(map);
    selLayer = L.layerGroup().addTo(map);
    deptLabels = L.layerGroup().addTo(map);

    cityLayer = L.layerGroup().addTo(map);
    for (const c of data.cities) {
      if (c.pop < 9000) continue;
      L.circleMarker([c.lat, c.lon], { renderer, radius: c.pop > 500000 ? 4.5 : 3, color: css("--ink"), weight: 1, fillColor: css("--land"), fillOpacity: 1, interactive: false }).addTo(cityLayer);
      L.marker([c.lat, c.lon], { interactive: false, icon: L.divIcon({ className: "city-label", html: c.name, iconAnchor: [-6, 14] }) }).addTo(cityLayer);
    }
    const fit = () => { map.invalidateSize(); map.fitBounds(deptLayer.getBounds(), { padding: [10, 10] }); };
    fit();
    setTimeout(fit, 150);
  }

  function buildCellLayer(fc) {
    if (cellLayer) map.removeLayer(cellLayer);
    cellLayer = L.geoJSON(fc, {
      renderer, style: cellStyle,
      onEachFeature: (f, layer) => {
        layer.bindTooltip(() => tooltipHTML(f.properties.id), { sticky: true, direction: "top", opacity: 1 });
        layer.on("click", () => openDetail(f.properties.id));
      },
    });
    if (state.overlay) cellLayer.addTo(map);
    if (riverLayer) riverLayer.bringToFront();
  }

  // ---------------------------------------------------------------- mapa base (Azure Maps)
  const azureKey = () => (state.meta && state.meta.map && state.meta.map.azure_maps_key) || "";
  function azureTiles(tileset, pane) {
    return L.tileLayer("https://atlas.microsoft.com/map/tile?api-version=2024-04-01&tilesetId=" + tileset +
      "&zoom={z}&x={x}&y={y}&tileSize=256&language=en-US&subscription-key=" + encodeURIComponent(azureKey()),
      { pane, maxZoom: 19, attribution: "© Microsoft Azure Maps, © TomTom" });
  }
  function applyBase() {
    if (baseLayer) { map.removeLayer(baseLayer); baseLayer = null; }
    if (STATIC || !azureKey()) return;
    const dark = getComputedStyle(document.documentElement).colorScheme.includes("dark");
    baseLayer = state.base === "imagery"
      ? L.layerGroup([azureTiles("microsoft.imagery", "basemap"), azureTiles("microsoft.base.hybrid.road", "labels")])
      : azureTiles(dark ? "microsoft.base.darkgrey" : "microsoft.base.road", "basemap");
    baseLayer.addTo(map);
  }
  function setBase(name) {
    state.base = name;
    for (const b of document.querySelectorAll("[data-base]")) b.setAttribute("aria-checked", b.dataset.base === name ? "true" : "false");
    applyBase();
  }

  // ---------------------------------------------------------------- sobreposição e resolução
  function setOverlay(on) {
    state.overlay = on;
    for (const l of [cellLayer, riverLayer, cityLayer, deptLabels]) {
      if (!l) continue;
      if (on) l.addTo(map); else map.removeLayer(l);
    }
    $("legend").hidden = !on;
    $("opacity").disabled = !on;
  }

  async function setRes(res) {
    if (STATIC || res === state.res) return;
    const prev = state.res;
    state.res = res;
    syncRes();
    $("stats").textContent = "Loading grid…";
    try {
      const fc = await getJSON(q("api/cells"));
      state.cells = fc.features.map((f) => f.properties);
      state.cellById = {};
      for (const c of state.cells) state.cellById[c.id] = c;
      state.selCell = null;
      $("detail").hidden = true;
      await compute();
      buildCellLayer(fc);
      restyle();
    } catch (e) {
      state.res = prev; syncRes();
      $("stats").textContent = "Could not switch resolution: " + e.message;
    }
  }
  function syncRes() {
    for (const b of document.querySelectorAll("[data-res]")) b.setAttribute("aria-checked", b.dataset.res === state.res ? "true" : "false");
  }

  function tooltipHTML(id) {
    const r = state.resById[id], c = state.cellById[id];
    if (!r) return "";
    const rows = [
      ["Province", c.dept], ["Aquifer", ZONE_TXT[c.zone] || c.zone], ["Estimated water table", fmt(r.WTD) + " m"],
      ["Borehole yield", fmt(c.yield_Ls, 1) + " L/s"], ["Borehole probability", pct(r.P_poco)], ["Estimated drilling depth", fmt(r.H) + " m"],
      ["Borehole cost", usd(r.C_poco_total)], ["River cost", usd(r.rio ? r.rio.C_total : r.C_rio_total)],
      ["Positive factor", r.fator_pos], ["Negative factor", r.fator_neg],
    ];
    return `<div class="tt"><h3>${CLS[r.cls].label}</h3><table>${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</table></div>`;
  }

  function deptCentroid(name) {
    const sel = state.cells.filter((c) => c.dept === name);
    if (!sel.length) return null;
    return [sel.reduce((s, c) => s + c.lat, 0) / sel.length, sel.reduce((s, c) => s + c.lon, 0) / sel.length];
  }

  function renderDeptLabels() {
    deptLabels.clearLayers();
    if (state.layer !== "prob") return;
    for (const d of state.depts) {
      const at = deptCentroid(d.dept);
      if (!at || d.P_poco_media === null || d.dept === "Kinshasa") continue;
      L.marker(at, { interactive: false, icon: L.divIcon({ className: "city-label", html: `<span style="font-size:14px">${d.dept} ${pct(d.P_poco_media)}</span>`, iconAnchor: [30, 8] }) }).addTo(deptLabels);
    }
  }

  // ---------------------------------------------------------------- UI
  const PEAT_PILL = `<span class="pill"><span class="dot" style="background:transparent;border:1.5px dashed var(--ink)"></span>Peatlands (sensitive area)</span>`;
  function renderLegend() {
    const el = $("legend");
    if (state.layer === "rec") {
      el.innerHTML = Object.values(CLS).map((k) => `<span class="pill"><span class="dot" style="background:${css(k.v)}"></span>${k.label}</span>`).join("");
    } else if (state.layer === "prob") {
      el.innerHTML = `<strong>Borehole success probability</strong><div class="ramp" style="background:linear-gradient(90deg,${PROB_STOPS.join(",")})"></div>
        <div class="ticks"><span>0%</span><span>40%</span><span>80%</span></div><span class="pill"><span class="dot" style="background:${css("--muted")};opacity:.3"></span>river or not recommended</span>`;
    } else if (state.layer === "conf") {
      el.innerHTML = `<strong>Model confidence</strong><div class="ramp" style="background:linear-gradient(90deg,${CONF_STOPS.join(",")})"></div>
        <div class="ticks"><span>low</span><span>high</span></div><span style="color:var(--muted)">distance to stations and IR margin</span>`;
    } else if (state.layer === "zones") {
      const present = new Set(state.cells.map((c) => c.zone));
      el.innerHTML = "<strong>Hydrogeological domain</strong>" + ZONE_ORDER.filter((z) => present.has(z)).map((z) =>
        `<span class="pill"><span class="dot" style="background:${css("--z-" + z)}"></span>${ZONE_TXT[z]}</span>`).join("") + PEAT_PILL;
    } else if (state.layer === "wtd") {
      el.innerHTML = `<strong>Estimated water-table depth</strong><div class="ramp" style="background:linear-gradient(90deg,${WTD_STOPS.join(",")})"></div>
        <div class="ticks"><span>0 m</span><span>${WTD_MAX / 2} m</span><span>${WTD_MAX}+ m</span></div><span style="color:var(--muted)">lighter = shallower</span>` + PEAT_PILL;
    } else {
      el.innerHTML = `<strong>Typical borehole yield</strong><div class="ramp" style="background:linear-gradient(90deg,${YIELD_STOPS.join(",")})"></div>
        <div class="ticks"><span>0</span><span>${YIELD_MAX / 2}</span><span>${YIELD_MAX}+ L/s</span></div><span style="color:var(--muted)">domain average (approx.)</span>` + PEAT_PILL;
    }
  }

  function renderStats() {
    const n = {}; for (const r of state.results) n[r.cls] = (n[r.cls] || 0) + 1;
    const tot = state.results.length;
    const well = (n.poco_alto || 0) + (n.poco_viavel || 0);
    $("stats").textContent = `${tot} cells · borehole ${pct(well / tot)} · river ${pct((n.rio || 0) / tot)} · field ${pct((n.inconclusivo || 0) / tot)} · none ${pct((n.nao_recomendado || 0) / tot)}`;
  }

  function renderDepts() {
    $("dept-body").innerHTML = state.depts.map((d) => {
      const k = CLS[d.classe_dominante];
      return `<tr data-dept="${d.dept}" class="${state.selDept === d.dept ? "sel" : ""}" tabindex="0">
        <td>${d.dept}</td><td><span class="pill"><span class="dot" style="background:${css(k.v)}"></span>${k.short}</span></td>
        <td class="num">${d.pct_poco}%</td><td class="num">${d.P_poco_media === null ? "–" : pct(d.P_poco_media)}</td></tr>`;
    }).join("");
  }

  function selectDept(name) {
    state.selDept = state.selDept === name ? null : name;
    selLayer.clearLayers();
    renderDepts();
    if (!state.selDept) return;
    deptLayer.eachLayer((l) => {
      if (l.feature.properties.name === name) {
        L.geoJSON(l.feature, { interactive: false, style: { color: css("--accent"), weight: 3, fill: false } }).addTo(selLayer);
        map.fitBounds(l.getBounds(), { padding: [30, 30], maxZoom: 9 });
      }
    });
  }

  function periodTable(c, p) {
    const rows = [6, 12, 24, 60].map((h) => {
      const r = M.evaluate(c, M.params(Object.assign({}, p, { horizon_months: h })));
      return `<tr${h === p.horizon_months ? ' class="cur"' : ""}><td>${h} months</td><td class="num">${usd(r.C_poco_total)}</td><td class="num">${usd(r.rio.C_total)}</td><td>${CLS[r.cls].short}</td></tr>`;
    }).join("");
    return `<div><h4>Comparison by period</h4><div class="table-wrap"><table class="period"><thead><tr><th>Horizon</th><th class="num">Borehole</th><th class="num">River</th><th>Recommendation</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }

  function bar(label, v) {
    return `<div class="bar"><span>${label}</span><span class="track"><span class="fill" style="display:block;width:${Math.max(0, Math.min(1, v)) * 100}%"></span></span><span>${fmt(v, 2)}</span></div>`;
  }

  async function openDetail(id) {
    state.selCell = id;
    const el = $("detail");
    let d;
    try { d = await cellDetail(id); } catch (e) { el.hidden = false; el.innerHTML = `<p>Could not compute this cell: ${e.message}</p>`; return; }
    if (state.selCell !== id) return;
    const { cell: c, result: r } = d;
    const rio = r.rio, comp = r.componentes, p = readParams();
    el.hidden = false;
    el.innerHTML = `
      <header><div><h3>${c.dept}</h3><div class="loc">${fmt(c.lat, 2)}°, ${fmt(c.lon, 2)}° · cell ${c.id} · ${ZONE_TXT[c.zone] || c.zone}</div></div>
        <button type="button" class="close" id="close-detail" aria-label="Close">×</button></header>
      <div class="verdict"><span class="dot" style="background:${css(CLS[r.cls].v)}"></span>${CLS[r.cls].label}<span class="big">IR ${fmt(r.IR, 2)}</span></div>
      ${r.turfeira ? '<div class="warn">Cuvette Centrale peatlands: environmentally sensitive area.</div>' : ""}
      <div class="cols">
        <div><h4>Borehole</h4><dl>
          <dt>P_borehole</dt><dd>${pct(r.P_poco)}</dd><dt>GPI</dt><dd>${fmt(r.GPI, 2)}</dd>
          <dt>Water table (WTD)</dt><dd>${fmt(r.WTD)} m</dd><dt>Drilling depth H</dt><dd>${fmt(r.H)} m</dd>
          <dt>Boreholes</dt><dd>${r.n_pocos} × ${fmt(r.vazao_poco_Ls, 1)} L/s</dd><dt>Recharge R</dt><dd>${fmt(r.R)} mm</dd>
          <dt>Capex</dt><dd>${usd(r.C_poco)}</dd><dt>Energy</dt><dd>${usd(r.C_poco_op)}</dd></dl></div>
        <div><h4>River pumping</h4><dl>
          <dt>Source</dt><dd>${rio.name}</dd><dt>Distance</dt><dd>${fmt(rio.D_km, 1)} km</dd>
          <dt>Flow</dt><dd>${fmt(rio.Q_rio, 1)} m³/s</dd><dt>Lift Δz</dt><dd>${fmt(Math.max(rio.dz, 0))} m</dd>
          <dt>H_t</dt><dd>${fmt(rio.H_t)} m</dd><dt>Pipe</dt><dd>${rio.pipe_d_mm} mm</dd>
          <dt>Capex</dt><dd>${usd(rio.C_capex)}</dd><dt>Energy</dt><dd>${usd(rio.C_bombeamento)}</dd></dl></div>
      </div>
      <div><h4>Total cost over ${p.horizon_months} months</h4><dl>
        <dt>Borehole</dt><dd>${usd(r.C_poco_total)}</dd><dt>River</dt><dd>${usd(rio.C_total)}</dd></dl></div>
      ${periodTable(c, p)}
      <div class="bars"><h4>IR components</h4>
        ${bar("A_sub (borehole)", comp.A_sub)}${bar("1 − A_sup (river)", comp.A_sup_inv)}${bar("E_cost", comp.E_custo)}
        ${bar("R_climate", comp.R_clima)}${bar("A_access", comp.A_acesso)}</div>
      <p class="explain">${d.explanation}</p>
      ${state.ai ? `<form class="ask" id="ask-form"><h4>Ask the AI about this cell</h4>
        <textarea id="ask-q" rows="2">Why is this the recommendation here, and what could change with a field study?</textarea>
        <button type="submit" id="ask-btn">Ask Claude</button><div class="ask-out" id="ask-out" aria-live="polite"></div></form>` : ""}`;
    if (state.ai) $("ask-form").addEventListener("submit", (e) => { e.preventDefault(); askAI(id); });
    $("close-detail").onclick = () => { el.hidden = true; state.selCell = null; };
  }

  async function askAI(id) {
    const out = $("ask-out"), btn = $("ask-btn");
    btn.disabled = true; out.textContent = "Asking Claude…";
    try {
      const r = await getJSON("api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: $("ask-q").value, cell_id: id, params: readParams(), res: state.res }) });
      out.textContent = r.answer;
      if (r.mode !== "claude") out.textContent = "AI unavailable right now; most relevant excerpt from the plan: " + r.answer;
    } catch (e) { out.textContent = "Could not reach the AI: " + e.message; }
    btn.disabled = false;
  }

  function syncOutputs() {
    const f = {
      area_ha: (v) => fmt(v) + " ha", c_energia: (v) => "USD " + fmt(v, 2) + "/kWh", WTD_max: (v) => fmt(v) + " m",
      D_max_km: (v) => fmt(v) + " km",
    };
    for (const id of ["area_ha", "c_energia", "WTD_max", "D_max_km", "w1", "w2", "w3", "w4", "w5"]) {
      $("o_" + id).textContent = (f[id] || ((v) => fmt(v, 2)))(Number($(id).value));
    }
    const h = Number($("horizon_months").value);
    for (const b of document.querySelectorAll(".seg button")) {
      const on = b.dataset.h === String(h) || (b.dataset.h === "custom" && h !== 6 && h !== 12);
      b.setAttribute("aria-checked", on ? "true" : "false");
    }
  }

  function setLayer(name) {
    state.layer = name;
    for (const b of document.querySelectorAll("[data-layer]")) b.setAttribute("aria-checked", b.dataset.layer === name ? "true" : "false");
    restyle();
  }

  function restyle() {
    cellLayer.setStyle(cellStyle);
    riverLayer.setStyle((f) => ({ color: css("--river") }));
    deptLayer.setStyle(() => ({ color: css("--ink") }));
    renderLegend(); renderStats(); renderDepts(); renderDeptLabels();
  }

  let timer = null, seq = 0;
  function schedule() {
    syncOutputs();
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const my = ++seq;
      try { await compute(); } catch (e) { $("stats").textContent = "Calculation error: " + e.message; return; }
      if (my !== seq) return;
      restyle();
      if (state.selCell !== null) openDetail(state.selCell);
    }, STATIC ? 60 : 200);
  }

  async function main() {
    $("start_month").innerHTML = MONTHS.map((m, i) => `<option value="${i + 1}">${m}</option>`).join("");
    $("start_month").value = "6";
    let data;
    try { data = await loadData(); } catch (e) {
      $("stats").textContent = "Could not load the data: " + e.message + ". Check that the API is running.";
      return;
    }
    state.meta = data.meta;
    if (!STATIC) { try { state.ai = (await getJSON("api/health")).rag === "claude"; } catch (e) { state.ai = false; } }
    state.cells = data.cells.features.map((f) => f.properties);
    for (const c of state.cells) state.cellById[c.id] = c;
    $("mode").textContent = STATIC ? "static demo" : "FastAPI";
    $("src-real").textContent = data.meta.sources.real.join("; ") + ".";
    $("src-syn").textContent = data.meta.sources.synthetic.join("; ") + ".";

    initMap(data);
    for (const b of document.querySelectorAll(".seg button")) {
      b.addEventListener("click", () => {
        if (b.dataset.h !== "custom") $("horizon_months").value = b.dataset.h;
        else { $("horizon_months").value = "24"; $("horizon_months").focus(); }
        schedule();
      });
    }
    for (const b of document.querySelectorAll("[data-layer]")) b.addEventListener("click", () => setLayer(b.dataset.layer));
    // explicações dos controles (ⓘ): abre/fecha a caixa logo abaixo do slider
    for (const b of document.querySelectorAll(".info")) {
      b.addEventListener("click", (e) => {
        e.preventDefault();
        const tip = $(b.getAttribute("aria-controls")), open = tip.hidden;
        tip.hidden = !open;
        b.setAttribute("aria-expanded", String(open));
      });
      b.addEventListener("keydown", (e) => { if (e.key === "Escape") { $(b.getAttribute("aria-controls")).hidden = true; b.setAttribute("aria-expanded", "false"); } });
    }
    $("overlay").addEventListener("change", (e) => setOverlay(e.target.checked));
    $("opacity").addEventListener("input", (e) => { state.opacity = Number(e.target.value); cellLayer.setStyle(cellStyle); });
    for (const b of document.querySelectorAll("[data-base]")) {
      b.disabled = !azureKey();
      b.addEventListener("click", () => setBase(b.dataset.base));
    }
    $("base-hint").hidden = !!azureKey();
    $("base-group").hidden = !!STATIC;
    const resolutions = (!STATIC && data.meta.resolutions) || {};
    $("res-group").hidden = Object.keys(resolutions).length < 2;
    $("res-seg").innerHTML = Object.entries(resolutions).map(([k, v]) =>
      `<button type="button" data-res="${k}" role="radio" title="${v.n_cells} cells">${v.km} km</button>`).join("");
    for (const b of document.querySelectorAll("[data-res]")) b.addEventListener("click", () => setRes(b.dataset.res));
    syncRes();
    setBase(state.base);
    for (const id of PARAM_IDS) $(id).addEventListener("input", schedule);
    $("reset").addEventListener("click", () => {
      for (const id of PARAM_IDS) $(id).value = M.DEFAULTS[id];
      schedule();
    });
    $("dept-body").addEventListener("click", (e) => { const tr = e.target.closest("tr"); if (tr) selectDept(tr.dataset.dept); });
    $("dept-body").addEventListener("keydown", (e) => { const tr = e.target.closest("tr"); if (tr && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectDept(tr.dataset.dept); } });
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onTheme = () => setTimeout(() => { restyle(); if (state.base === "road") applyBase(); }, 30);  // ruas claras/escuras
    (mq.addEventListener ? mq.addEventListener.bind(mq, "change") : mq.addListener.bind(mq))(onTheme);
    new MutationObserver(onTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    setLayer("rec");
    syncOutputs();
    await compute();
    restyle();
  }

  main();
})();
