**Español** | [English](README.md)

# DataMind GTFS — plantilla de pipeline y consola para construir feeds GTFS

Plantilla open source de una **consola de operador y un pipeline de cinco fases** que construye, valida y publica **feeds GTFS de transporte público** a partir de datos abiertos (OpenStreetMap), con revisión humana en los puntos críticos.

> **Extraído de un proyecto real de análisis de transporte público en Quito.**

> La base de datos que trae es **vacía**; incluye una **muestra sintética y diminuta** (3 líneas, 26 paradas, 6 lugares de una ciudad inventada). No hay datos reales, catálogos de ninguna región, modelos entrenados, teselas ni extractos de mapa. Ver [Qué incluye y qué no](#qué-incluye-y-qué-no).

## Qué hace

Convierte "paradas y rutas sueltas en el mapa" en un feed GTFS consistente:

1. **Encuentra las paradas** y los lugares de referencia de una zona.
2. **Les da significado** (nombres canónicos, alias, búsqueda semántica).
3. **Construye las rutas** (secuencia de paradas + geometría sobre la red vial) y las somete a controles de calidad.
4. **Nombra las rutas** con evidencia (operador, extremos, referencias).
5. **Compila el GTFS** (`agency`, `routes`, `trips`, `stop_times`, `shapes`…) y lo publica.

Todo el trabajo pasa por una **consola web (Streamlit)** donde un operador aprueba, corrige o rechaza. Un motor de orquestación (**HADES**) automatiza los pasos repetitivos con "puertas de aprobación" para que nada se publique sin permiso.

## Capturas

Consola en modo local con la muestra sintética:

| Inicio | Auditoría · inventario |
|---|---|
| ![Inicio](docs/screenshots/01-consola-inicio.jpg) | ![Inventario](docs/screenshots/02-auditoria-inventario.jpg) |
| **Auditoría · rutas** | |
| ![Rutas](docs/screenshots/03-auditoria-rutas.jpg) | |

## Arquitectura del pipeline de 5 fases

```
 OpenStreetMap (Overpass)                                     Valhalla (ruteo)
        │                                                            │
        ▼                                                            ▼
 ┌────────────┐   ┌────────────────┐   ┌───────────────┐   ┌────────────────┐   ┌──────────────┐
 │ Fase 1     │──►│ Fase 2         │──►│ Fase 3        │──►│ Fase 4         │──►│ Fase 5       │
 │ NODOS      │   │ SEMÁNTICA      │   │ RUTAS         │   │ NOMBRES        │   │ GTFS         │
 │ paradas y  │   │ lugares, alias │   │ secuencia +   │   │ evidencia,     │   │ compilar y   │
 │ POIs       │   │ embeddings     │   │ geometría +   │   │ ranking de     │   │ publicar     │
 │ (ML+DBSCAN)│   │ búsqueda       │   │ puertas de    │   │ candidatos     │   │ (local / AWS)│
 └────────────┘   └────────────────┘   │ calidad       │   └────────────────┘   └──────────────┘
   node_raw/work/prod   geo_raw/work/prod  route_raw/work/prod   semantics             gtfs_work / gtfs
                                       └───────────────┘
        ▲                 ▲                    ▲                   ▲                     ▲
        └─────────────────┴────────────────────┴───────────────────┴─────────────────────┘
                     Consola de operador (Streamlit)  ·  HADES (autopilot + enforcers)
                     PostgreSQL + PostGIS + pgvector  ·  cada fase: raw → work → prod
```

| Fase | Carpeta | Esquemas | Qué produce |
|---|---|---|---|
| 1 · Nodos | `phase1_nodes/` | `node_raw`, `node_work`, `node_prod` | Paradas y POIs limpios (extracción OSM, clustering, clasificación ML) |
| 2 · Semántica | `phase2_semantics/` | `geo_raw`, `geo_work`, `geo_prod` | Lugares canónicos, alias, embeddings y búsqueda |
| 3 · Rutas | `phase3_routes/` | `route_raw`, `route_work`, `route_prod`, `route_review`, `route_trash` | Rutas con secuencia de paradas y geometría, con controles de calidad |
| 4 · Nombres | `phase4_naming/` | `semantics` | Nombres de ruta con evidencia y un *ranker* de candidatos |
| 5 · GTFS | `phase5_gtfs/` | `gtfs_work`, `gtfs`, `gtfs_prod`, `catalog` | Feed GTFS compilado, validado y publicado |

Componentes transversales:

- `datamind_console/` — la consola (vistas por fase, orquestador, servicios, persistencia, asistente de IA).
- `hades/` — *enforcers* que aplican reglas de calidad (geometría, cobertura de paradas, reclasificación…).
- `pipeline/` — snappers y fase 4.5 (auditoría y *commit* de correcciones).
- `local_runner/` — automatización no interactiva de prompts (opcional).
- `datamind_core/` — configuración global (DSN, esquemas, servicios) y `dsn.py`.
- `db/schema.sql` — **el esquema completo de la base** (solo estructura).

## Requisitos

- Python 3.10+.
- PostgreSQL 15+ (probado con 17) con las extensiones **postgis**, **vector (pgvector)**, `pgcrypto`, `pg_trgm`, `citext`, `unaccent` y `dblink`.
- Opcionales según la función que uses: Overpass y Valhalla (extracción y ruteo reales), OpenSearch (búsqueda semántica), una clave de OpenAI (asistente de IA), acceso SSH/AWS (publicación remota).

## Instalación paso a paso

**1. Clona e instala dependencias**

```bash
git clone <URL-DE-TU-REPO>.git datamind-gtfs
cd datamind-gtfs
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

**2. Copia `.env.example` a `.env` y pon tus propias llaves**

```bash
cp .env.example .env
```

Abre `.env` y define **tu** conexión a la base (`DB_DSN`). El archivo está comentado variable por variable, y al final lista todas las demás que lee el código. **Nada viene "de fábrica"**: si falta la conexión, todos los scripts y la consola se detienen con un mensaje que dice qué variable poner. No subas `.env` a Git (ya está ignorado).

Mínimo para el modo local con la muestra:

```ini
DB_DSN=postgresql://USUARIO:CONTRASEÑA@127.0.0.1:5432/datamind_ml
LOCAL_DB_DSN=postgresql://USUARIO:CONTRASEÑA@127.0.0.1:5432/datamind_ml
DATA_MODE=local
DATAMIND_LOCAL_ONLY_MODE=true
```

**3. Crea la base vacía** (la base `datamind_ml` debe existir: `createdb datamind_ml`)

```bash
python scripts/init_db.py          # extensiones + esquema completo (19 esquemas, 134 tablas)
```

Es transaccional (si algo falla no queda nada a medias) y se niega a pisar una base ya creada. `--reset --yes` borra los esquemas de DataMind y los recrea.

**4. Carga la muestra sintética**

```bash
python scripts/load_sample_data.py           # 3 rutas · 26 paradas · 6 lugares
python scripts/load_sample_data.py --remove  # elimina solo las filas de la muestra
```

Es repetible: volver a correrlo reemplaza la muestra sin duplicarla.

**5. Arranca la consola**

```bash
streamlit run datamind_console/app.py --server.address 127.0.0.1
```

(La consola y los scripts leen tu `.env` automáticamente.)

Abre <http://127.0.0.1:8501>. En modo local no pide login (Streamlit debe escuchar solo en `127.0.0.1`, que es lo que hace `.streamlit/config.toml`). Entra a **Sample Region Audit** para ver la muestra. Para un despliegue remoto con usuarios y sesiones, ver `ML_DATAMIND_REMOTE_UI` en `.env.example`.

## La consola

La consola es bilingüe (English / Español; se cambia en la barra lateral y queda
en el parámetro `?lang=` de la URL) y cada página tiene un enlace directo
(`/review`, `/phases?phase=3&tab=re_entry`, …).

| Sección | Pantalla | Para qué |
| --- | --- | --- |
| Operar | Resumen | Inicio operativo construido solo con datos reales: frescura del pipeline, qué necesita atención, conteos canónicos de rutas, actividad reciente, feed vigente. Sin base de datos muestra un estado claro de «base no disponible» y el resto de la consola igual arranca. Nunca elige una región ni un trabajo. |
| Operar | Fases | Espacios de trabajo por fase (1 Nodos · 2 Semántica · 3 Rutas · 4 Nombres · 5 GTFS). La fase 4 incluye **Evidencia** y **Matching** (abajo). |
| Operar | Operaciones GTFS | Validar un ZIP GTFS (chequeo de estructura, validador de MobilityData a pedido), publicar localmente, despliegue en AWS tras confirmación explícita. Ver [docs/GTFS_VALIDATION.md](docs/GTFS_VALIDATION.md). |
| Operar | Revisar GTFS actual | Revisar un feed existente. |
| Revisión | Revisión | Vista de solo lectura de cada cola que espera a una persona, con enlaces a donde se decide. |
| Revisión | Paradas sintéticas | Paradas que el pipeline generó para cubrir huecos: verificar o rechazar cada una, sobre un mapa. Las rechazadas quedan fuera de todo export GTFS. |
| Revisión | Otras propuestas | Propuestas de ruta fuera de la revisión de reingreso: ver la evidencia y rechazar con motivo. Nada se aprueba automáticamente. |
| Auditoría | Auditoría de la región de ejemplo | Inspección de solo lectura de los sentidos de ruta, paradas y cohortes de la región de ejemplo, con las métricas canónicas. |
| Integración | Páginas de Geo API | Vistas de Geo API, producción y fusión. |
| Sistema | Asistencia de IA | Muestra un contexto de trabajo revisable y abre una terminal local interactiva de Claude Code o Codex solo después de revisar el alcance; no autentica ni amplía permisos. |
| Sistema | Configuración | Apariencia e idioma; si el autopilot firma con una clave propia; en modo local, la configuración vigente (los secretos solo como configurado / no configurado); en modo remoto, usuarios y roles. |

**Revisión humana.** El pipeline propone, una persona decide. Cada decisión en
esas pantallas lleva el nombre de quien revisa y se escribe junto con una fila
de auditoría en `console.phase_decisions`, en una sola transacción; nada se
decide al abrir una página ni de forma automática. Ver
[docs/REVIEW.md](docs/REVIEW.md).

**Fase 4 · Evidencia y Matching.** Explora registros de evidencia (relaciones
de OSM, horarios, declaraciones de operadores), agrega uno a mano, pide rutas
candidatas puntuadas por un matcher determinista y explicado, y etiqueta cada
candidata (correcta / parcial / no es esta ruta). Las etiquetas son decisiones
humanas con su fila de auditoría; no hay un modelo entrenado, así que no se
ofrece predicción por modelo. Las dos tablas vienen de la migración `V4_8`
(unidad de esquema `phase4.evidence_matching`). Ver
[docs/PHASE4_EVIDENCE_MATCHING.md](docs/PHASE4_EVIDENCE_MATCHING.md).

Los números significan lo mismo en todas partes: ver
[docs/METRICS.md](docs/METRICS.md) y [docs/TERMINOLOGY.md](docs/TERMINOLOGY.md).
Qué hace de verdad cada capacidad hoy (activa, heurística, experimental,
heredada…) está en [docs/CURRENT_CAPABILITIES.md](docs/CURRENT_CAPABILITIES.md).

## Acceso a la base de datos y esquema

Abrir una página nunca cambia la base de datos. Las páginas verifican el
esquema que necesitan y, si falta, lo dicen. Las migraciones se aplican solo a
pedido:

```bash
python -m datamind_console.db.schema_cli status
```
```bash
python -m datamind_console.db.schema_cli plan <unidad>
```
```bash
python -m datamind_console.db.schema_cli apply <unidad> --yes
```

`apply` ejecuta el SQL de migración contra la base configurada; `plan` muestra
qué haría cada archivo, incluidos los cambios de datos. El código lee con los
helpers de `datamind_console/db/db.py` (`query_all`, `read_only_connection`,
`transaction`, …). Ver [docs/DB_ACCESS.md](docs/DB_ACCESS.md).

Para explorar la consola contra una base real sin ningún riesgo de escritura,
dale a la sesión solo lectura por defecto:
`options=-c default_transaction_read_only=on` en el DSN (codificado en URL), o
`PGOPTIONS="-c default_transaction_read_only=on"`.

## Servidor MCP (solo lectura)

`tools/mcp_server.py` expone la base de datos a clientes MCP. Cada sesión es de
solo lectura con un límite de 30 s por consulta. `query_db` acepta una sola
sentencia `SELECT` / `WITH` / `EXPLAIN` / `SHOW` / `VALUES` / `TABLE` y rechaza
funciones con efectos (`dblink*`, `pg_terminate_backend`, `lo_*`,
`set_config`, `pg_sleep`, …). Devuelve como máximo 500 filas.

## Adapta el pipeline a tu región

La muestra vive en una región inventada (`sample_region`, "Sample City"). Para tu región reemplaza la configuración:

| Qué | Dónde |
|---|---|
| Regiones activas, caja operativa, sesgos de geocodificación | `workspace/config/supported_provinces.json` |
| Cajas de extracción por sector, anclas, catálogo de POIs y etiquetas OSM | `phase1_nodes/catalogs/*.json` |
| Grupos de búsqueda geográfica | `datamind_console/common/nominatim_group_bias.json` |
| Jurisdicciones y reglas de calidad de la puesta en tierra de paradas | `datamind_console/phases/phase3_routes/stop_grounding/catalogs/` |
| Operadores y plantillas de nombres | `phase4_naming/catalogs/` |
| Servicios externos (Overpass, Valhalla, OpenSearch) | `.env` (`OVERPASS_URL`, `VALHALLA_URL`, `OPENSEARCH_URL`) |

Overpass y Valhalla se pueden levantar con `docker-compose.yml` (edítalo para apuntar a un extracto OSM de tu región en `./data/overpass/`).

## Tests

Corre las suites **de a una carpeta** (varios paquetes `tests/` comparten
nombre), por ejemplo:

```bash
python -B -m pytest -p no:cacheprovider datamind_console --ignore=datamind_console/phases/phase3_routes/tests
```
```bash
python -B -m pytest -p no:cacheprovider tests
```

Las otras carpetas son `datamind_console/phases/phase3_routes/tests`,
`tools/tests`, `phase1_nodes`, `phase2_semantics`, `phase3_routes/diagnostics`,
`local_runner/tests` y `phase3_routes/services/route_constructor/tests`
(desde ese directorio). Ninguna necesita base de datos, AWS ni una API externa.
Ver [docs/TEST_CLASSIFICATION.md](docs/TEST_CLASSIFICATION.md).

- Los tests que **necesitan una base con datos** se saltan solos. Actívalos con
  `DB_DSN=... DATAMIND_RUN_DB_TESTS=1` (suponen datos reales de una región, no la
  muestra). Los que escriben (papelera, revisión humana, evidencia de la fase 4)
  solo corren con `DATAMIND_DISPOSABLE_TEST_DSN` apuntando a una base cuyo
  nombre contenga `test`, `scratch` o `disposable`.
- Los tests escritos contra los archivos de datos de la región original
  (catálogo de lugares, catálogos de la fase 3, catálogo de bboxes) se reportan
  como **omitidos** mientras esos archivos no estén; ver `conftest.py`. Con los
  archivos de tu región, corren.

## Qué incluye y qué no

**Incluye:** el código de las cinco fases, la consola, HADES, el esquema completo de la base, una muestra sintética diminuta y configuración de ejemplo. El logo de la consola es el del proyecto original (`datamind_console/ui/assets/`).

**No incluye (a propósito):** datos reales, catálogos de una región concreta, dumps, modelos entrenados (LightGBM: reentrénalos con `ai_training/` y tu propia base), teselas de Valhalla, extractos OSM, salidas de corridas, ni logs.

**Limitaciones que debes conocer**

- El pipeline se desarrolló para **una región concreta** (Quito). Aunque la configuración ya es de muestra, algunas heurísticas de nombres de lugares (por ejemplo en `geography_guardrails.py` y `geography_input_resolver.py`) y varios comentarios y pruebas siguen mencionando esa región. Adáptalos a la tuya.
- `db/schema.sql` es un volcado (saneado) del esquema real. Los archivos de `*/migrations` y `*/sql` se conservan como **historial** de cambios, pero por sí solos no reconstruyen la base completa: usa `scripts/init_db.py`.
- Las fases 3 y 5 esperan servicios reales (Valhalla, OTP) para ejecutarse de punta a punta; sin ellos puedes explorar la consola y la muestra, pero no construir rutas nuevas.

## Seguridad

- Sin credenciales incrustadas: la conexión sale siempre de `DB_DSN` / `.env`. Si falta, el código falla con un mensaje claro (`datamind_core/dsn.py`).
- La consola en modo local solo debe exponerse en `127.0.0.1`. Para acceso remoto usa el modo con login y sesiones.
- El asistente de IA solo abre un terminal local con un flag explícito (`ML_DATAMIND_LOCAL_CLI_LAUNCH_ENABLED`) y tras revisar el alcance.
- La API de la fase 4 exige `X-API-Key` (`PHASE4_API_KEY`) en los endpoints que escriben y limita CORS a la consola local.
- Secretos: `python tools/secret_scan.py --tracked` revisa cada archivo versionado y muestra huellas, nunca valores. `tools/git-hooks/pre-push` rechaza un push cuyos commits nuevos agreguen un secreto; instálalo con `cp tools/git-hooks/pre-push .git/hooks/pre-push`. Ver [docs/SECURITY_HYGIENE.md](docs/SECURITY_HYGIENE.md).
- El autopilot firma las decisiones del operador con `HADES_OPERATOR_SIGNING_SECRET`. Crea la tuya con `python -m datamind_console.orchestrator.signing_key --write .env`; sin ella la consola avisa que se está usando la clave pública de desarrollo.
- Antes de publicar tu fork: `gitleaks detect` y revisa que no subas `.env`, dumps ni datos.

## Licencia

[MIT](LICENSE)

## Autores

Josué Arcos y Jhair Jiménez
