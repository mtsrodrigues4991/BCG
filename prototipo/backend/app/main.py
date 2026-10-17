"""API Hidro Congo (FastAPI).

Endpoints principais:
  GET  /api/health
  GET  /api/meta                      parâmetros padrão, pesos AHP, fontes de dados
  GET  /api/cells                     geometria + covariáveis das células (GeoJSON)
  GET  /api/layers/{nome}             departments | rivers | cities | stations
  POST /api/recommendations           resultados de todas as células + resumo por província
  GET  /api/recommendation/point      recomendação para uma coordenada (lat, lon)
  GET  /api/regions/{dept}            resumo e células de uma província
  POST /api/cells/{id}/evaluate       detalhe completo de uma célula com parâmetros
  GET  /api/explain/{cell_id}         explicação em linguagem natural
  POST /api/ask                       perguntas à IA (Claude) sobre o plano e, opcionalmente, uma célula
  POST /api/chat                      chat do assistente hídrico com busca web (rotas_chat.py)
"""
from __future__ import annotations

import json
import math
import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import rag
from .explain import explain
from .model import AHP_CR, AHP_LAMBDA, GPI_FACTORS, GPI_W, Params, evaluate, summarize
from .rotas_chat import router as chat_router

BASE = Path(__file__).resolve().parent.parent.parent
DATA = Path(os.environ.get("HIDRO_DATA", BASE / "data"))
FRONTEND = Path(os.environ.get("HIDRO_FRONTEND", BASE / "frontend"))

app = FastAPI(title="Hidro Congo API", version="0.1.0",
              description="Recomendação de poço tubular x bombeamento de rio na República Democrática do Congo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(chat_router)


@lru_cache
def cells_fc() -> dict:
    return json.loads((DATA / "cells.geojson").read_text())


@lru_cache
def cells() -> list[dict]:
    return [f["properties"] for f in cells_fc()["features"]]


def _round(obj, nd=4):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _round(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_round(v, nd) for v in obj]
    return obj


class RecRequest(BaseModel):
    params: dict = {}
    detail: bool = False   # True = devolve todos os campos; False = só o necessário ao mapa


SLIM = ("id", "cls", "rec", "IR", "P_poco", "WTD", "H", "C_poco_total", "confianca", "fator_pos", "fator_neg")


@app.get("/api/health")
def health():
    return {"status": "ok", "cells": len(cells()), "rag": rag.status()}


@app.get("/api/meta")
def meta():
    m = json.loads((DATA / "meta.json").read_text())
    m["params_default"] = Params().to_dict()
    m["gpi"] = {"factors": GPI_FACTORS, "weights": GPI_W, "lambda_max": AHP_LAMBDA, "CR": AHP_CR}
    m["map"] = {"provider": os.environ.get("MAP_PROVIDER", "vector"),
                "azure_maps_key": os.environ.get("AZURE_MAPS_KEY", "")}
    return _round(m)


@app.get("/api/cells")
def get_cells():
    return cells_fc()


@app.get("/api/layers/{name}")
def layer(name: str):
    files = {"departments": "departments.geojson", "rivers": "rivers.geojson",
             "cities": "cities.json", "stations": "stations.json"}
    if name not in files:
        raise HTTPException(404, "camada desconhecida")
    return JSONResponse(json.loads((DATA / files[name]).read_text()))


def run(params: dict):
    p = Params.from_dict(params)
    res = [evaluate(c, p) for c in cells()]
    return p, res


@app.post("/api/recommendations")
def recommendations(req: RecRequest):
    p, res = run(req.params)
    out = res if req.detail else [{k: r[k] for k in SLIM} | {"rio_nome": r["rio"]["name"],
                                                             "C_rio_total": r["rio"]["C_total"]} for r in res]
    return _round({"params": p.to_dict(), "results": out, "departments": summarize(cells(), res)})


def nearest_cell(lat: float, lon: float) -> dict:
    best = min(cells(), key=lambda c: (c["lat"] - lat) ** 2 + ((c["lon"] - lon) * math.cos(math.radians(lat))) ** 2)
    d = math.hypot((best["lat"] - lat) * 110.57, (best["lon"] - lon) * 111.32 * math.cos(math.radians(lat)))
    if d > 30:
        raise HTTPException(404, "coordenada fora da área da RDC coberta pela grade")
    return best


@app.get("/api/recommendation/point")
def point(lat: float, lon: float, horizon_months: int = 12, start_month: int = 6, area_ha: float = 5.0):
    c = nearest_cell(lat, lon)
    p = Params.from_dict({"horizon_months": horizon_months, "start_month": start_month, "area_ha": area_ha})
    r = evaluate(c, p)
    return _round({"cell": {k: v for k, v in c.items()}, "result": r, "explanation": explain(c, r, p)})


@app.get("/api/regions/{dept}")
def region(dept: str, horizon_months: int = 12, start_month: int = 6, area_ha: float = 5.0):
    p = Params.from_dict({"horizon_months": horizon_months, "start_month": start_month, "area_ha": area_ha})
    sel = [c for c in cells() if dept in c["depts"]]
    if not sel:
        raise HTTPException(404, f"província '{dept}' não encontrada")
    res = [evaluate(c, p) for c in sel]
    summary = next(s for s in summarize(sel, res) if s["dept"] == dept)
    return _round({"summary": summary, "cells": [{k: r[k] for k in SLIM} for r in res]})


@app.post("/api/cells/{cell_id}/evaluate")
def evaluate_cell(cell_id: int, req: RecRequest):
    if not 0 <= cell_id < len(cells()):
        raise HTTPException(404, "célula inexistente")
    c = cells()[cell_id]
    p = Params.from_dict(req.params)
    r = evaluate(c, p)
    return _round({"cell": c, "result": r, "explanation": explain(c, r, p)})


@app.get("/api/explain/{cell_id}")
def explain_cell(cell_id: int, horizon_months: int = Query(12), start_month: int = Query(6),
                 area_ha: float = Query(5.0)):
    if not 0 <= cell_id < len(cells()):
        raise HTTPException(404, "célula inexistente")
    c = cells()[cell_id]
    p = Params.from_dict({"horizon_months": horizon_months, "start_month": start_month, "area_ha": area_ha})
    r = evaluate(c, p)
    return {"cell_id": cell_id, "text": explain(c, r, p)}


class AskRequest(BaseModel):
    question: str
    k: int = 3
    cell_id: int | None = None   # opcional: inclui a recomendação desta célula no contexto
    params: dict = {}


def cell_context(cell_id: int, params: dict) -> dict:
    c = cells()[cell_id]
    p = Params.from_dict(params)
    r = evaluate(c, p)
    keep = ("cls", "IR", "P_poco", "GPI", "WTD", "H", "n_pocos", "C_poco_total", "A_sub", "A_sup",
            "E_custo", "R_clima_poco", "R_clima_rio", "A_acesso", "fator_pos", "fator_neg", "turfeira")
    return _round({"provincia": c["dept"], "lat": c["lat"], "lon": c["lon"], "zona": c["zone"],
                   "chuva_mm_ano": c["P"], "horizonte_meses": p.horizon_months, "area_ha": p.area_ha,
                   "resultado": {k: r[k] for k in keep},
                   "rio": {k: r["rio"][k] for k in ("name", "D_km", "Q_rio", "dz", "C_total")},
                   "explicacao": explain(c, r, p)})


@app.post("/api/ask")
def ask(req: AskRequest):
    ctx = None
    if req.cell_id is not None:
        if not 0 <= req.cell_id < len(cells()):
            raise HTTPException(404, "célula inexistente")
        ctx = cell_context(req.cell_id, req.params)
    return rag.answer(req.question, req.k, ctx)


# --- frontend estático ------------------------------------------------------
if FRONTEND.exists():
    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html")

    app.mount("/", StaticFiles(directory=FRONTEND), name="frontend")
