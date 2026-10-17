# Hidro Congo — protótipo

Protótipo do hackathon que implementa o [plano de arquitetura](../plano_arquitetura.md): para cada
província da República Democrática do Congo (RDC), recomenda **poço tubular** ou **bombeamento de rio**, com probabilidade
de sucesso do poço, profundidade, custo e os fatores que pesaram na decisão.

O país é dividido em células de **0,4° (~44 km, 1.245 células, padrão)** ou **0,2° (~22 km, 4.847
células)**, à escolha no mapa, agregadas nas **26 províncias**.

No mapa, a barra de camadas tem dois grupos: **Análise** (recomendação, probabilidade do poço, confiança)
e **Aquíferos** (domínio hidrogeológico, profundidade do lençol, vazão por poço, com as turfeiras em
contorno tracejado). A **sobreposição** pode ser desligada ou ter a opacidade ajustada para ver o mapa
base do Azure Maps (ruas ou satélite).

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
python3 -m pip install numpy shapely pillow earthengine-api
python3 pipeline/build_data.py      # grade, DEM, clima, rios (cache em .cache/) -> data/
python3 pipeline/build_bgs.py       # produtividade de aquíferos do BGS (modelo v2)
python3 pipeline/build_gee.py       # GLOBGM, CHIRPS, SoilGrids, WorldPop, JRC via Earth Engine (~6 min)
python3 pipeline/build_wpdx.py      # pontos de água do WPdx (validação)
python3 pipeline/build_tiles.py     # mapas base Relief e Land cover (blocos em frontend/tiles/, ~1 min)
```

O `build_gee.py` precisa de `EE_PROJECT` e `EE_SERVICE_ACCOUNT_KEY` (JSON de conta de serviço,
guardado fora do repositório) ou do login de `earthengine authenticate`. Só ele usa o Earth Engine:
o app lê os arquivos gerados em `data/` e não precisa da credencial.

O app tem dois modelos (bloco **Model**): **v1** com as aproximações descritas abaixo e **v2** com
dados reais (BGS, GLOBGM, CHIRPS, SoilGrids). A comparação dos dois com pontos de água reais (WPdx)
está no painel **Validation** e em [VALIDACAO.md](VALIDACAO.md).

## Deploy na Azure

No ar em https://ca-hidrocongo.bravegrass-2f493583.germanywestcentral.azurecontainerapps.io
(Azure Container Apps + Azure Maps no resource group `SAO-PAULO-team-2`, imagem no Docker Hub). A
arquitetura cloud, os recursos, os segredos e os comandos de deploy estão no [README da raiz](../README.md#5-arquitetura-cloud).

## Configuração

| Variável | Efeito |
|---|---|
| `AZURE_MAPS_KEY` | chave do Azure Maps: acrescenta os mapas base **Roads** e **Satellite** (ao vivo). Sem ela continuam disponíveis **Relief** e **Land cover**, gerados uma vez no Earth Engine e servidos como arquivos (`frontend/tiles/`). A chave vai para o navegador via `/api/meta`; restrinja-a por domínio no portal |
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
| POST | `/api/recommendations` | `{"params": {...}}` → resultado de todas as células + resumo por província |
| POST | `/api/cells/{id}/evaluate` | detalhe completo de uma célula |
| GET | `/api/recommendation/point?lat=&lon=` | recomendação por coordenada |
| GET | `/api/regions/{dept}` | resumo e células de uma província (ex.: `Kinshasa`, `Équateur`) |
| GET | `/api/explain/{id}` | texto explicativo |
| POST | `/api/ask` | `{"question": "...", "cell_id": 10, "params": {...}}` → resposta do Claude com trechos do plano e, se `cell_id` vier, os números da célula |
| POST | `/api/chat` | `{"message": "...", "history": [{"role": "user", "content": "..."}]}` → resposta do assistente com busca web e `sources` (usado pelo widget de chat) |

Parâmetros aceitos em `params`: `horizon_months` (6, 12 ou qualquer valor de 1 a 60), `start_month`,
`area_ha`, `irrigation_mm_day`, `hours_per_day`, `WTD_max`, `R_ref`, `Dz_max`, `D_max_km`,
`env_flow_frac`, custos (`C_fixo`, `c_perf`, `c_revest`, `C_bomba`, `C_inst`, `C_bomba_rio`,
`C_captacao`, `c_energia`), `eta` e os pesos `w1`…`w5`. Padrões em `model.py`.

O que cada controle do bloco **Irrigation project** faz, as fórmulas e os parâmetros fixos estão em
[PARAMETROS.md](PARAMETROS.md).

## Dados: o que é real e o que é sintético

**Reais (abertos):**
- geoBoundaries (gbOpen COD ADM1, derivado do OSM): limites das 26 províncias atuais. O Natural Earth
  ainda traz as 11 províncias anteriores à reforma de 2015.
- Natural Earth 10m: rios principais, estradas e cidades.
- AWS Terrain Tiles (Terrarium, derivado de SRTM/NASADEM), ~2,4 km: elevação, declive, curvatura.
- Rede de drenagem e vazão **derivadas do DEM** (priority-flood + D8, como no HydroSHEDS), com escoamento
  por Budyko (w = 3,5, calibrado para ~350–400 mm/ano observados na bacia do Congo).
- NOAA GHCN-Daily: normais mensais de chuva e temperatura de 22 estações da RDC e vizinhos.
- NASA POWER (climatologia MERRA-2 2001–2025): 102 pontos em grade de 1,5°, que cobrem as
  lacunas do GHCN na RDC. PET por Hargreaves (FAO-56); interpolação IDW das duas fontes. A confiança
  da recomendação usa a distância à estação GHCN (medida) mais próxima.

**Sintéticos (marcados no app e em `data/meta.json`):**
- Domínios hidrogeológicos (costeira de Moanda, Kongo Central, rift Albertino/Kivu, Cuvette Centrale,
  Kalahari/Batéké, cráton do Kasai, Katanga, embasamento do norte e leste), definidos por regras
  lat/lon, com
  favorabilidade, permeabilidade do solo e vazão típica por poço, aproximados a partir do Africa
  Groundwater Atlas / MacDonald et al. (2012).
- NDVI estimado pela chuva; índice populacional por gravidade das cidades.
- Coeficientes β da regressão de WTD (não há poços de calibração).
- Vazões médias dos grandes rios (Congo, Kasai, Ubangi, Lualaba, Lomami…): ordens de grandeza de
  literatura, a conferir no GRDC.
- Área das turfeiras da Cuvette Centrale (baixadas da bacia entre 16,5–21,5°E e 2,5°S–2,5°N).

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
- **Grade de 0,4°**: a RDC tem ~7× a área da República do Congo; 0,4° mantém o mapa e o pipeline
  com o mesmo peso da versão de 0,2° para Brazzaville.

## Limitações

- Resultados preliminares: sondagem e teste de bombeamento são sempre necessários antes da obra.
- Com 5 ha, o capex domina o custo, então mudar o horizonte de 6 meses para 1 ano altera poucas
  células; o painel de cada célula mostra o comparativo de custos em 6, 12, 24 e 60 meses.
- Rede de drenagem a ~1,2 km: pequenos córregos não aparecem, o que tende a favorecer o poço longe dos
  rios principais.
