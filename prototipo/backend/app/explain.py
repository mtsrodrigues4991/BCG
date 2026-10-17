"""Explicação em linguagem natural de uma recomendação (sem LLM, por template)."""
from __future__ import annotations

from .model import Params

CLS_TXT = {
    "poco_alto": "poço tubular altamente recomendado",
    "poco_viavel": "poço tubular viável",
    "rio": "bombeamento de rio recomendado",
    "inconclusivo": "análise inconclusiva, precisa de estudo de campo",
    "nao_recomendado": "nenhuma das duas opções é recomendada",
}
ZONE_TXT = {
    "costeira": "bacia sedimentar costeira de Moanda",
    "kongo_central": "Kongo Central (Mayombe cristalino e calcários do Schisto-calcaire, com possível carste)",
    "rift": "rift Albertino/Kivu (rochas vulcânicas e fraturadas, alta variabilidade)",
    "cuvette": "bacia sedimentar da Cuvette Centrale (lençol raso)",
    "kalahari": "planaltos Kalahari/Batéké (areias espessas, lençol profundo nos topos)",
    "kasai": "cráton do Kasai (embasamento cristalino)",
    "katanga": "Katanga (metassedimentos e dolomitos, possível carste)",
    "embasamento": "embasamento cristalino do norte e leste",
}


def usd(x: float) -> str:
    return f"USD {x:,.0f}".replace(",", ".")


def explain(c: dict, r: dict, p: Params) -> str:
    rio = r["rio"]
    parts = [
        f"Célula em {c['dept']} ({c['lat']:.2f}, {c['lon']:.2f}), domínio {ZONE_TXT.get(c['zone'], c['zone'])}.",
        f"Resultado: {CLS_TXT[r['cls']]} (IR = {r['IR']:.2f}; acima de 0,5 favorece poço).",
        f"Poço: probabilidade de sucesso {100 * r['P_poco']:.0f}%, lençol estimado a {r['WTD']:.0f} m, "
        f"perfuração de {r['H']:.0f} m, {r['n_pocos']} poço(s) para {p.area_ha:g} ha, "
        f"custo no horizonte de {p.horizon_months} meses {usd(r['C_poco_total'])}.",
        f"Rio: melhor captação em {rio['name']} a {rio['D_km']:.1f} km, desnível {max(rio['dz'], 0):.0f} m, "
        f"adequação {r['A_sup']:.2f}, custo no horizonte {usd(rio['C_total'])}.",
        f"Principal fator a favor: {r['fator_pos']}. Principal limitação: {r['fator_neg']}.",
    ]
    if r["turfeira"]:
        parts.append("Atenção: área de turfeiras da Cuvette Centrale, ambientalmente sensível; "
                     "drenagem e perfuração exigem licenciamento cuidadoso.")
    if not r["vazao_ok"]:
        parts.append("A vazão típica por poço neste domínio não cobre a demanda mesmo com o número máximo de poços.")
    parts.append("Recomendação preliminar: sondagem e teste de bombeamento são necessários antes da obra.")
    return " ".join(parts)
