# Hidro Congo — protótipo

Protótipo do hackathon que implementa o [plano de arquitetura](../plano_arquitetura.md): para cada
região da República do Congo, recomenda **poço tubular** ou **bombeamento de rio**, com probabilidade
de sucesso do poço, profundidade, custo e os fatores que pesaram na decisão.

O país é dividido em **761 células de 0,2° (~22 km)**, agregadas nos **12 departamentos**.

## Como rodar

### Docker (recomendado)

```bash
cd prototipo
docker compose up --build
# abra http://localhost:8000  (API em http://localhost:8000/docs)
```

### Sem Docker

```bash
cd prototipo/backend
python3 -m pip install -r requirements.txt
uvicorn app.main:app --port 8000
# abra http://localhost:8000
```

### Demo estática (sem servidor)

`dist/hidro-congo.html` é um arquivo único com dados e modelo embutidos; os cálculos rodam no navegador
com `frontend/model.js`. Regere com `python3 pipeline/build_artifact.py`.

### Testes

```bash
cd prototipo/backend && python3 -m pytest -q
```

`test_parity.py` roda o modelo em Python e em JavaScript (Node) sobre todas as células e confere que
os números batem.

### Regerar os dados

```bash
python3 -m pip install numpy shapely pillow
python3 pipeline/build_data.py      # baixa com cache em .cache/ e grava data/
```

## Deploy na Azure

O plano do MVP (2 recursos no resource group `SAO-PAULO-team-2`, imagem no Docker Hub, com comandos `docker` e `az`) está no documento
"Hidro Congo na Azure: plano do MVP para o hackathon".

## Configuração

| Variável | Efeito |
|---|---|
| `MAP_PROVIDER` | `vector` (padrão, só camadas vetoriais), `osm` (OpenStreetMap) ou `azure` |
| `AZURE_MAPS_KEY` | chave do Azure Maps, usada quando `MAP_PROVIDER=azure` |
| `ANTHROPIC_API_KEY` | liga a IA (Claude) no `/api/ask` e a caixa "Pergunte à IA" no painel da célula; sem ela a resposta é extrativa (BM25 local) |
| `ANTHROPIC_MODEL` | modelo do Claude (padrão `claude-opus-5-5`) |
| `BING_SUBSCRIPTION_KEY` | chave do Bing Web Search usada pelo chat (`/api/chat`); sem ela o chat usa a busca web nativa do Claude |
| `BING_SEARCH_URL` | endpoint do Bing (padrão `https://api.bing.microsoft.com/v7.0/search`) |

Veja `.env.example`.

## O que tem em cada parte

```
pipeline/build_data.py     ETL: DEM, drenagem, clima, grade e covariáveis  -> data/
backend/app/model.py       fórmulas da seção 3 (WTD, R, GPI/AHP, P_poço, C_poço, A_sup, C_bombeamento, IR)
backend/app/main.py        API FastAPI + serve o frontend
backend/app/rag.py         RAG: BM25 local sobre o plano + resposta do Claude (SDK anthropic)
backend/app/rotas_chat.py  chat com busca web (Claude + tool calling: Bing ou web_search)
backend/app/explain.py     explicação em linguagem natural por célula
frontend/                  Leaflet + JS puro (mesmo modelo em model.js para a demo estática)
```

### Endpoints

| Método | Rota | Uso |
|---|---|---|
| GET | `/api/health` | status |
| GET | `/api/meta` | parâmetros padrão, pesos AHP e CR, fontes de dados |
| GET | `/api/cells` | células (GeoJSON) com covariáveis |
| GET | `/api/layers/{departments,rivers,cities,stations}` | camadas de apoio |
| POST | `/api/recommendations` | `{"params": {...}}` → resultado de todas as células + resumo por departamento |
| POST | `/api/cells/{id}/evaluate` | detalhe completo de uma célula |
| GET | `/api/recommendation/point?lat=&lon=` | recomendação por coordenada |
| GET | `/api/regions/{dept}` | resumo e células de um departamento |
| GET | `/api/explain/{id}` | texto explicativo |
| POST | `/api/ask` | `{"question": "...", "cell_id": 10, "params": {...}}` → resposta do Claude com trechos do plano e, se `cell_id` vier, os números da célula |
| POST | `/api/chat` | `{"message": "...", "history": [{"role": "user", "content": "..."}]}` → resposta do assistente com busca web e `sources` (usado pelo widget de chat) |

Parâmetros aceitos em `params`: `horizon_months` (6, 12 ou qualquer valor de 1 a 60), `start_month`,
`area_ha`, `irrigation_mm_day`, `hours_per_day`, `WTD_max`, `R_ref`, `Dz_max`, `D_max_km`,
`env_flow_frac`, custos (`C_fixo`, `c_perf`, `c_revest`, `C_bomba`, `C_inst`, `C_bomba_rio`,
`C_captacao`, `c_energia`), `eta` e os pesos `w1`…`w5`. Padrões em `model.py`.

## Dados: o que é real e o que é sintético

**Reais (abertos):**
- Natural Earth 10m: departamentos, rios principais, estradas e cidades.
- AWS Terrain Tiles (Terrarium, derivado de SRTM/NASADEM), ~1,2 km: elevação, declive, curvatura.
- Rede de drenagem e vazão **derivadas do DEM** (priority-flood + D8, como no HydroSHEDS), com escoamento
  por Budyko (w = 3,5, calibrado para ~350–400 mm/ano observados na bacia do Congo).
- NOAA GHCN-Daily: normais mensais de chuva e temperatura de 25 estações do Congo e vizinhos;
  PET por Hargreaves (FAO-56); interpolação IDW.

**Sintéticos (marcados no app e em `data/meta.json`):**
- Domínios hidrogeológicos (costeira, Mayombe, Niari, Chaillu, Batéké, Cuvette, noroeste), com
  favorabilidade, permeabilidade do solo e vazão típica por poço, aproximados a partir do Africa
  Groundwater Atlas / MacDonald et al. (2012).
- NDVI estimado pela chuva; índice populacional por gravidade das cidades.
- Coeficientes β da regressão de WTD (não há poços de calibração).
- Vazões médias dos grandes rios (Congo, Ubangi, Sangha, Kouilou…): valores aproximados de literatura.
- Área das turfeiras da Cuvette/Likouala.

Hosts como CHIRPS, SoilGrids, HydroSHEDS, WorldClim e OSM/Overpass não estavam acessíveis no ambiente
de construção. A troca é localizada em `build_data.py`: cada covariável sintética tem um ponto único
de cálculo.

## Decisões de modelagem (onde o plano deixava margem)

- **Ponto de implantação**: dentro de cada célula usa-se o quartil inferior do relevo (onde ficam
  lavouras e captação) para HAND e desnível ao rio.
- **Sinal do A_sup no IR**: como IR > 0,5 significa poço, a adequação do rio entra como `1 − A_sup`.
- **E_custo** = `C_rio_total / (C_poço_total + C_rio_total)` no horizonte escolhido, onde cada total
  é capex + energia no período (poços: n poços para cobrir a vazão demandada; rio: bomba, captação e
  adutora por Hazen-Williams).
- **R_clima** relativo: `0,5 + 0,5·(R_clima_poço − R_clima_rio)`. Rio: fração dos meses do horizonte
  em que 10% da vazão (vazão ecológica) cobre a demanda. Poço: `0,6 + 0,4·(chuva no período / média)`,
  pelo amortecimento do aquífero.
- **A_acesso** favorece o poço (mobilização da sonda depende de estrada).
- **GPI**: 7 fatores com pesos por AHP (autovetor principal; CR = 0,016 < 0,1).
- **Classes do mapa**: vermelho quando `max(A_sub, A_sup) < 0,12`; amarelo quando `|IR − 0,5| < 0,03`;
  verde-escuro quando poço e `P_poço ≥ 45%`.
- O polígono "Brazzaville" do Natural Earth (~6.000 km²) inclui parte do Pool; o pipeline mantém só a
  comuna (~15 km do centro) e devolve o resto ao Pool.

## Limitações

- Resultados preliminares: sondagem e teste de bombeamento são sempre necessários antes da obra.
- Com 5 ha, o capex domina o custo, então mudar o horizonte de 6 meses para 1 ano altera poucas
  células; o painel de cada célula mostra o comparativo de custos em 6, 12, 24 e 60 meses.
- Rede de drenagem a ~1,2 km: pequenos córregos não aparecem, o que tende a favorecer o poço longe dos
  rios principais.
