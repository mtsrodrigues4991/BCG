from fastapi.testclient import TestClient

from app.main import app
from app.model import AHP_CR, Params, evaluate

client = TestClient(app)


def test_health_and_meta():
    assert client.get("/api/health").json()["status"] == "ok"
    m = client.get("/api/meta").json()
    assert m["gpi"]["CR"] < 0.1  # matriz AHP consistente
    assert abs(sum(m["gpi"]["weights"]) - 1) < 1e-3


def test_recommendations_all_cells():
    r = client.post("/api/recommendations", json={"params": {"horizon_months": 6}}).json()
    assert r["params"]["horizon_months"] == 6
    assert len(r["results"]) > 700
    classes = {x["cls"] for x in r["results"]}
    assert classes <= {"poco_alto", "poco_viavel", "rio", "inconclusivo", "nao_recomendado"}
    assert all(0 <= x["P_poco"] <= 1 for x in r["results"])
    assert len(r["departments"]) == 12


def test_point_and_region_and_explain():
    r = client.get("/api/recommendation/point", params={"lat": 1.62, "lon": 18.05}).json()  # Impfondo
    assert r["cell"]["dept"] == "Likouala"
    assert "Recomendação preliminar" in r["explanation"]
    assert client.get("/api/recommendation/point", params={"lat": 40, "lon": 0}).status_code == 404
    reg = client.get("/api/regions/Cuvette").json()
    assert reg["summary"]["dept"] == "Cuvette" and reg["cells"]
    assert client.get("/api/explain/0").json()["text"]


def test_ask_extractive():
    r = client.post("/api/ask", json={"question": "turfeiras na Cuvette"}).json()
    assert r["sources"] and r["mode"] in ("extrativo", "azure_openai")


def test_formulas_behave():
    from app.main import cells
    c = dict(cells()[0])
    p = Params()
    base = evaluate(c, p)
    deeper = evaluate(c | {"HAND": c["HAND"] + 80}, p)
    assert deeper["WTD"] > base["WTD"] and deeper["P_poco"] <= base["P_poco"]
    far = evaluate(c | {"stream": c["stream"] | {"d_km": 50}, "major": c["major"] | {"d_km": 50}}, p)
    assert far["A_sup"] == 0
    assert AHP_CR < 0.1
