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

```bash
python -B -m pytest -p no:cacheprovider local_runner/tests datamind_console/orchestrator/tests
# → 248 passed, 5 skipped
```

- Se corren **por carpeta**: hay varios paquetes `tests/` con el mismo nombre y pytest no los mezcla bien en una sola invocación.
- Los tests que **necesitan una base poblada** están marcados como de integración y se saltan solos. Actívalos con `DB_DSN=... DATAMIND_RUN_DB_TESTS=1` (asumen datos reales de una región, no la muestra).
- 3 tests de resolución geográfica verifican comportamientos del catálogo de la región original y están omitidos (`@unittest.skip`) hasta que los adaptes al tuyo.

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
- Antes de publicar tu fork: `gitleaks detect` y revisa que no subas `.env`, dumps ni datos.

## Licencia

[MIT](LICENSE)

## Autores

Josué Arcos y Jhair Jiménez
