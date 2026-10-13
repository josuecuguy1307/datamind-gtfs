<div align="center">

**Español** · [English](README.md)

<img src="docs/media/logo.svg" alt="Logo de ML DataMind" width="84"><a href="#mcp-con-claude-code"><img src="docs/media/claude-mark.svg" alt="con Claude" height="84"></a>

# ML DataMind

### Reconstruye la red de buses de una ciudad como un feed GTFS, con una persona aprobando cada decisión.

Un pipeline y una consola de operador de código disponible (*source available*). Convierte datos abiertos de mapas y evidencia de campo en las rutas, paradas, trazados y horarios que necesitan las apps de transporte y quienes planifican la ciudad.

[![Licencia: Source Available](https://img.shields.io/badge/licencia-Source%20Available-17145e)](#licencia)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-17145e)
![PostgreSQL + PostGIS](https://img.shields.io/badge/PostgreSQL-PostGIS%20%C2%B7%20pgvector-17145e)
![Salida: GTFS](https://img.shields.io/badge/salida-GTFS-d99a2b)
![Revisión humana](https://img.shields.io/badge/revisi%C3%B3n-humana-d99a2b)
![Origen: Antari](https://img.shields.io/badge/naci%C3%B3%20de-la%20app%20Antari-5b5f73)
[![MCP con Claude Code](https://img.shields.io/badge/MCP%20con-Claude%20Code-D97757?logo=claude&logoColor=white)](#mcp-con-claude-code)

[**▶ Ver el recorrido por la consola**](#recorrido-por-la-consola) &nbsp;·&nbsp;
[MCP con Claude Code](#mcp-con-claude-code) &nbsp;·&nbsp;
[Cómo funciona](#cómo-funciona) &nbsp;·&nbsp;
[Flujo de trabajo](#flujo-de-trabajo-típico) &nbsp;·&nbsp;
[Inicio rápido](#inicio-rápido) &nbsp;·&nbsp;
[Documentación](#documentación)

<a href="#recorrido-por-la-consola">
  <img src="docs/media/hero-route-map.webp" alt="La consola de DataMind: una ruta de bus reconstruida en Quito dibujada sobre el mapa, con sus 11 paradas en orden" width="900">
</a>

<sub>Una ruta reconstruida por DataMind (*Bolivia – Tejar – Chorrera*, Quito): el trazado sigue las calles y las paradas aparecen en orden. Haz clic para ver un recorrido de 70 segundos por la consola (interfaz en inglés).</sub>

</div>

## Recorrido por la consola

Setenta segundos en la consola real, sin narración: una ruta en el mapa, una limpieza antes y después, las colas de revisión, la puerta de validación GTFS y el alcance de la IA.

https://github.com/user-attachments/assets/f634cf72-a150-4647-8c69-1a7f9836e610

<sub>¿No ves el reproductor? [Descarga el video](docs/media/datamind-console-demo.mp4).</sub>

---

## ¿Estás construyendo datos de transporte para una ciudad?

- **Dale una estrella a ML DataMind** si este flujo de trabajo es relevante para ti.
- **[Prueba la demostración GTFS sintética para consumidores](https://github.com/josuecuguy1307/datamind-gtfs/blob/34856474e8e520ae7e12d429a9d4d584aa12f28b/docs/SYNTHETIC_GTFS_PROOF.md)** propuesta en el [PR #1](https://github.com/josuecuguy1307/datamind-gtfs/pull/1), sin datos privados de Ecuador ni una base de datos. Es una muestra de prueba inventada, no un feed operativo ni una prueba de reconstrucción de principio a fin.
- **[Trae tu ciudad](https://github.com/josuecuguy1307/datamind-gtfs/issues/new?template=city-data.md)**: describe la ciudad, las fuentes disponibles, las rutas, paradas u horarios faltantes, la cobertura OSM y dónde se rompe tu flujo de trabajo.
- **[Únete a la comunidad de ML DataMind](https://discord.gg/58qExtuE5p)** en `#ml-datamind` para conversar sobre GTFS, OSM, validación y operación asistida por MCP. Los motores del pipeline y la revisión humana conservan la autoridad; la asistencia de IA no autoriza publicar.

---

## Por qué existe DataMind

<table>
<tr>
<td width="20%" align="center">🚌<br><b>1 · Una app</b></td>
<td width="20%" align="center">🧩<br><b>2 · Un muro</b></td>
<td width="20%" align="center">🗺️<br><b>3 · El problema real</b></td>
<td width="20%" align="center">🛠️<br><b>4 · DataMind</b></td>
<td width="20%" align="center">📲<br><b>5 · De vuelta a la gente</b></td>
</tr>
<tr>
<td valign="top">Empezamos construyendo una app de transporte para usuarios en Ecuador.</td>
<td valign="top">La app necesitaba rutas, paradas y horarios. No existía un feed público completo y confiable.</td>
<td valign="top">Lo difícil eran los datos: reconstruir las paradas de cada ruta, su orden y el camino que sigue por las calles.</td>
<td valign="top">Así que construimos la infraestructura para reconstruir esos datos con evidencia y revisión humana.</td>
<td valign="top">El resultado es un feed GTFS estándar que cualquier app o herramienta de planificación puede usar.</td>
</tr>
</table>

> [!NOTE]
> **Nacido de una necesidad real de un producto de transporte. Caso de uso downstream: Antari.**
> Antari es la app de movilidad para usuarios que dejó al descubierto el problema de los datos. DataMind nació de ahí como la **infraestructura de datos upstream**: Antari consume datos de transporte y DataMind los produce. Son productos distintos: Antari es donde apareció el problema, y es el uso downstream para el que se construye la salida de DataMind.

---

## MCP con Claude Code

<a href="#mcp-con-claude-code"><img src="https://img.shields.io/badge/MCP%20con-Claude%20Code-D97757?style=for-the-badge&logo=claude&logoColor=white" alt="MCP con Claude Code"></a> <img src="https://img.shields.io/badge/servidor%20MCP-18%20herramientas%20·%20solo%20lectura-1f1b4d?style=for-the-badge&logo=modelcontextprotocol&logoColor=white" alt="Servidor MCP: 18 herramientas, solo lectura">

Durante dos meses llenamos los huecos de los datos casi a mano, y era lento. El 25 de febrero de 2026 sumamos a **Claude Code** al proyecto como orquestador del pipeline. Ejecutó nuestros motores fase por fase, siguiendo nuestros propios playbooks, y llegó a la base de datos a través de un **servidor MCP** que escribimos para él. El ritmo cambió en días.

https://github.com/user-attachments/assets/ecbd7b14-2567-403c-ae1d-ea7e59109152

<sub>«La historia», 90 segundos, con las fechas y cifras reales de nuestra base de datos y del historial de git.</sub>

| | Antes de Claude <br><sub>enero – febrero de 2026</sub> | Con Claude como orquestador |
|---|---|---|
| **Corridas de extracción de OpenStreetMap** | 42 en dos meses | **1.392** en la semana del 2 de marzo |
| **Registros de ruta limpios** | 7 | **241** construidos en cuatro semanas (30 de marzo – 26 de abril) |
| **Investigación de huecos** | a mano | **2.075** huecos investigados y **3.592** puntos de referencia propuestos (23 de abril – 6 de mayo), cada uno comprobado por nuestro código |
| **Resultado** | | **218 líneas de bus limpias** en la región de Quito (275 registros de un sentido) |
| **Guayaquil** | | **301** sentidos de ruta construidos con el mismo pipeline **en tres días** (9 – 11 de abril de 2026), **98** desplegados. Su revisión de limpieza sigue pendiente |

**Por qué el MCP fue esencial.** El MCP le dio a Claude una ventana segura al trabajo: `tools/mcp_server.py` expone **18 herramientas, todas de solo lectura** (catálogo de rutas, detalle de ruta, resúmenes por fase, corridas de extracción, decisiones recientes, `query_db`…). Claude podía ver el estado real de cada ruta y de cada fase, compararlo con referencias y decidir qué ejecutar después, **sin poder cambiar ni una fila por su cuenta**. Los motores que ejecutó son nuestro propio código, y nuestros playbooks (skills, uno por fase) le indicaban cómo ejecutarlos.

> [!IMPORTANT]
> **Claude hizo el trabajo a velocidad. Nosotros mantuvimos el control.** Nada se publica sin la aprobación de una persona, y las 794 paradas que el sistema rellenó en rutas limpias siguen marcadas como pendientes de revisión. Son las fechas y cifras propias de nuestro proyecto en Ecuador; no hacemos ninguna comparación con otros equipos ni instituciones.

---

## Cómo funciona

DataMind es un pipeline de cinco fases con una persona en el circuito. Las máquinas proponen y concilian. Las personas deciden qué se publica.

```mermaid
flowchart LR
    A["📥 Evidencia<br/><sub>OpenStreetMap, notas de campo,<br/>horarios de operadores</sub>"] --> B["🗺️ Reconstrucción<br/><sub>paradas, lugares, secuencia<br/>y geometría de la ruta</sub>"]
    B --> C["🧑‍⚖️ Revisión humana<br/><sub>verificar, corregir o rechazar,<br/>con rastro de auditoría</sub>"]
    C --> D["📦 Exportar GTFS<br/><sub>compilar, validar<br/>por capas, publicar</sub>"]
    D --> E["📲 Downstream<br/><sub>apps de transporte, planificadores<br/>de viaje, planificación urbana</sub>"]
```

| | Qué entra | Qué sale |
|---|---|---|
| **1 · Nodos** | Datos de mapa de una zona (OpenStreetMap vía Overpass) | Paradas y puntos de interés limpios |
| **2 · Semántica** | Nombres de lugares en bruto | Lugares canónicos, alias y búsqueda |
| **3 · Rutas** | Paradas más la red vial (Valhalla) | Cada ruta como una secuencia ordenada de paradas con un trazado que sigue las calles, pasada por controles de calidad |
| **4 · Nombres** | Evidencia (relaciones de OSM, horarios, declaraciones de operadores) | Nombres de ruta, cada uno atado a la evidencia que lo respalda |
| **5 · GTFS** | Rutas y paradas aprobadas | `agency`, `routes`, `trips`, `stop_times`, `shapes`… como un feed que primero se valida y luego se publica |

> [!TIP]
> **Nada sale en piloto automático.** El motor de orquestación (**HADES**) automatiza los pasos repetitivos, pero se detiene en puertas de aprobación. Cada decisión humana se guarda con su fila de auditoría, y publicar un feed es siempre una acción humana explícita.

---

## Un vistazo por dentro

<sub>Estas capturas salen de la consola corriendo sobre los datos privados del proyecto original en Ecuador. **Este repositorio no trae datos reales**, solo una muestra sintética diminuta. Ver [Qué incluye y qué no](#qué-incluye-y-qué-no).</sub>

<table>
<tr>
<td width="50%" valign="top">
<img src="docs/media/es/before-after.jpg" alt="Mapas de antes y después de la misma ruta en Guayaquil: paradas ajustadas a la línea canónica"><br>
<b>Limpiar con reglas, no con suposiciones.</b> Cada cambio muestra la ruta antes y después: las paradas alineadas no se mueven, las cercanas se ajustan a la línea y las huérfanas se eliminan.
</td>
<td width="50%" valign="top">
<img src="docs/media/es/synthetic-stops.jpg" alt="Pantalla de paradas sintéticas: 2.423 pendientes, 0 verificadas, 0 rechazadas"><br>
<b>El pipeline propone; una persona decide.</b> Las paradas que el pipeline generó para cubrir huecos esperan en una cola hasta que alguien verifique o rechace cada una. Los contadores son honestos: todavía no se ha decidido ninguna.
</td>
</tr>
<tr>
<td width="50%" valign="top">
<img src="docs/media/es/gtfs-validation.jpg" alt="Operaciones GTFS: validación por capas con chequeo de estructura y el validador de MobilityData"><br>
<b>Validación por capas.</b> Antes de poder publicar algo corren un chequeo de estructura y el validador GTFS de MobilityData. Un feed que no pasó queda marcado como «todavía no listo para publicar».
</td>
<td width="50%" valign="top">
<img src="docs/media/es/ai-scope.jpg" alt="Asistencia de IA: un alcance revisado y el contexto exacto que recibe un agente local"><br>
<b>IA con límites claros.</b> Puedes darle a un agente de IA local un contexto revisado y acotado. Ese contexto es información, no autorización: no otorga permisos de base de datos, nube ni Git.
</td>
</tr>
</table>

<details>
<summary><b>Más: el resumen operativo</b></summary>
<br>
<img src="docs/media/es/overview.jpg" alt="Resumen: conteos canónicos de rutas por provincia y el trabajo que necesita atención" width="760">

La pantalla de inicio muestra los conteos canónicos (cada número está definido en [docs/METRICS.md](docs/METRICS.md)) y todas las colas que esperan a una persona.
</details>

---

## Qué puedes hacer con DataMind

- 🗺️ **Reconstruir rutas** a partir de datos abiertos: paradas, su orden y un trazado que sigue las calles reales.
- 🔎 **Auditar lo que existe**: inspeccionar cualquier sentido de ruta, sus paradas y su clase de calidad sobre un mapa.
- 🧑‍⚖️ **Llevar colas de revisión humana** para paradas sintéticas, limpiezas de reingreso y propuestas de ruta, con el nombre de quien revisa y una fila de auditoría en cada decisión.
- 🏷️ **Nombrar rutas con evidencia**: adjuntar relaciones de OSM, horarios o declaraciones de operadores, y obtener candidatas de un matcher determinista y explicado.
- 📦 **Construir y validar GTFS**: compilar el feed, revisarlo por capas y publicarlo localmente o en AWS tras una confirmación explícita.
- 🤖 **Consultarlo desde herramientas de IA**: un servidor MCP de solo lectura ([detalles](#bajo-el-capó)) expone la base a clientes MCP.
- 🌎 **Adaptarlo a tu ciudad**: cambiar la configuración regional y correr el mismo pipeline para tu propia organización.

---

## Flujo de trabajo típico

Así se ve el día de un operador en la consola:

| Paso | Dónde, en la consola | Qué haces |
|---|---|---|
| **1. Ubicarte** | *Resumen* | Ver la frescura del pipeline, los conteos canónicos y lo que necesita atención. |
| **2. Traer evidencia** | *Fases → 1 Nodos / 2 Semántica* | Extraer paradas y lugares de tu zona y revisar los clusters. |
| **3. Construir rutas** | *Fases → 3 Rutas* | Construir rutas, inspeccionar la geometría y devolver las débiles por reingreso. |
| **4. Revisar** | *Revisión · Paradas sintéticas · Otras propuestas* | Verificar, corregir o rechazar. Cada decisión registra quién la tomó. |
| **5. Nombrar** | *Fases → 4 Nombres (Evidencia y Matching)* | Adjuntar evidencia y etiquetar las rutas candidatas. |
| **6. Publicar** | *Fases → 5 GTFS*, luego *Operaciones GTFS* | Compilar el feed, validarlo y publicarlo solo cuando pase. |
| **7. Usarlo** | Fuera de DataMind | Cargar el GTFS en un planificador de viajes, una app para usuarios o una herramienta de planificación. |

---

## Inicio rápido

> [!IMPORTANT]
> Necesitas **Python 3.10+** y **PostgreSQL 15+** con las extensiones `postgis`, `vector` (pgvector), `pgcrypto`, `pg_trgm`, `citext`, `unaccent` y `dblink`. Overpass, Valhalla, OpenSearch, una clave de OpenAI y AWS son opcionales. Solo hacen falta para las funciones que los usan.

**1. Clona e instala**

```bash
git clone <URL-DE-TU-REPO>.git datamind-gtfs
cd datamind-gtfs
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

**2. Configura.** Copia el archivo de ejemplo y define **tu** conexión a la base (`DB_DSN`). Nada viene «de fábrica»: si falta la conexión, todos los scripts y la consola se detienen y dicen qué variable poner. `.env` ya está ignorado por Git.

```bash
cp .env.example .env
```

Mínimo para el modo local con la muestra:

```ini
DB_DSN=postgresql://USUARIO:CONTRASEÑA@127.0.0.1:5432/datamind_ml
LOCAL_DB_DSN=postgresql://USUARIO:CONTRASEÑA@127.0.0.1:5432/datamind_ml
DATA_MODE=local
DATAMIND_LOCAL_ONLY_MODE=true
```

**3. Crea la base vacía** (corre antes `createdb datamind_ml`)

```bash
python scripts/init_db.py
```

Crea las extensiones y el esquema completo (19 esquemas, 134 tablas). Corre en una sola transacción y se niega a pisar una base ya creada. `--reset --yes` borra los esquemas de DataMind y los recrea.

**4. Carga la muestra sintética**

```bash
python scripts/load_sample_data.py
```

Carga 3 rutas, 26 paradas y 6 lugares de una ciudad inventada. Es repetible, y `--remove` elimina solo las filas de la muestra.

**5. Arranca la consola**

```bash
streamlit run datamind_console/app.py --server.address 127.0.0.1
```

Abre <http://127.0.0.1:8501> y entra a **Sample Region Audit** para ver la muestra. El modo local no pide login, así que Streamlit debe escuchar solo en `127.0.0.1` (lo que ya fija `.streamlit/config.toml`). Para un despliegue remoto con usuarios y sesiones, ver `ML_DATAMIND_REMOTE_UI` en `.env.example`.

---

## Bajo el capó

<details>
<summary><b>La arquitectura de 5 fases</b></summary>

```
 OpenStreetMap (Overpass)                                     Valhalla (ruteo)
        │                                                            │
        ▼                                                            ▼
 ┌────────────┐   ┌────────────────┐   ┌───────────────┐   ┌────────────────┐   ┌──────────────┐
 │ Fase 1     │──►│ Fase 2         │──►│ Fase 3        │──►│ Fase 4         │──►│ Fase 5       │
 │ NODOS      │   │ SEMÁNTICA      │   │ RUTAS         │   │ NOMBRES        │   │ GTFS         │
 │ paradas y  │   │ lugares, alias │   │ secuencia +   │   │ evidencia,     │   │ compilar y   │
 │ POIs       │   │ embeddings     │   │ geometría +   │   │ ranking de     │   │ publicar     │
 │ (ML+DBSCAN)│   │ búsqueda       │   │ controles     │   │ candidatos     │   │ (local / AWS)│
 └────────────┘   └────────────────┘   └───────────────┘   └────────────────┘   └──────────────┘
   node_raw/work/prod   geo_raw/work/prod  route_raw/work/prod   semantics             gtfs_work / gtfs
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

- `datamind_console/`: la consola (vistas por fase, orquestador, servicios, persistencia, asistente de IA).
- `hades/`: *enforcers* que aplican reglas de calidad (geometría, cobertura de paradas, reclasificación…).
- `pipeline/`: snappers y fase 4.5 (auditoría y *commit* de correcciones).
- `local_runner/`: automatización no interactiva de prompts (opcional).
- `datamind_core/`: configuración global (DSN, esquemas, servicios) y `dsn.py`.
- `db/schema.sql`: **el esquema completo de la base** (solo estructura).

</details>

<details>
<summary><b>La consola, pantalla por pantalla</b></summary>

La consola es bilingüe (English / Español). El idioma se cambia en la barra lateral y queda guardado en el parámetro `?lang=` de la URL. Cada página tiene un enlace directo (`/review`, `/phases?phase=3&tab=re_entry`, …).

| Sección | Pantalla | Para qué |
| --- | --- | --- |
| Operar | Resumen | Inicio operativo construido solo con datos reales: frescura del pipeline, qué necesita atención, conteos canónicos de rutas, actividad reciente, feed vigente. Sin base de datos muestra un estado claro de «base no disponible» y el resto de la consola igual arranca. Nunca elige una región ni un trabajo. |
| Operar | Fases | Espacios de trabajo por fase (1 Nodos · 2 Semántica · 3 Rutas · 4 Nombres · 5 GTFS). La fase 4 incluye **Evidencia** y **Matching**. |
| Operar | Operaciones GTFS | Validar un ZIP GTFS (chequeo de estructura, validador de MobilityData a pedido), publicar localmente, despliegue en AWS tras confirmación explícita. Ver [docs/GTFS_VALIDATION.md](docs/GTFS_VALIDATION.md). |
| Operar | Revisar GTFS actual | Revisar un feed existente. |
| Revisión | Revisión | Vista de solo lectura de cada cola que espera a una persona, con enlaces a donde se decide. |
| Revisión | Paradas sintéticas | Paradas que el pipeline generó para cubrir huecos: verificar o rechazar cada una, sobre un mapa. Las rechazadas quedan fuera de todo export GTFS. |
| Revisión | Otras propuestas | Propuestas de ruta fuera de la revisión de reingreso: ver la evidencia y rechazar con motivo. Nada se aprueba automáticamente. |
| Auditoría | Auditoría de la región de ejemplo | Inspección de solo lectura de los sentidos de ruta, paradas y cohortes de la región de ejemplo, con las métricas canónicas. |
| Integración | Páginas de Geo API | Vistas de Geo API, producción y fusión. |
| Sistema | Asistencia de IA | Muestra un contexto de trabajo revisable y abre una terminal local interactiva de Claude Code o Codex solo después de revisar el alcance; no autentica ni amplía permisos. |
| Sistema | Configuración | Apariencia e idioma; si el autopilot firma con una clave propia; en modo local, la configuración vigente (los secretos solo como configurado / no configurado); en modo remoto, usuarios y roles. |

**Revisión humana.** Cada decisión en esas pantallas lleva el nombre de quien revisa y se escribe en la misma transacción que su fila de auditoría en `console.phase_decisions`. Nada se decide al abrir una página ni de forma automática. Ver [docs/REVIEW.md](docs/REVIEW.md).

**Fase 4 · Evidencia y Matching.** Explora registros de evidencia (relaciones de OSM, horarios, declaraciones de operadores), agrega uno a mano, pide rutas candidatas puntuadas por un matcher determinista y explicado, y etiqueta cada candidata (correcta / parcial / no es esta ruta). Las etiquetas son decisiones humanas con su fila de auditoría. No hay un modelo entrenado, así que no se ofrece predicción por modelo. Las dos tablas vienen de la migración `V4_8` (unidad de esquema `phase4.evidence_matching`). Ver [docs/PHASE4_EVIDENCE_MATCHING.md](docs/PHASE4_EVIDENCE_MATCHING.md).

</details>

<details>
<summary><b>Acceso a la base de datos y esquema</b></summary>

Abrir una página nunca cambia la base de datos. Las páginas verifican el esquema que necesitan y, si falta, lo dicen. Las migraciones se aplican solo a pedido:

```bash
python -m datamind_console.db.schema_cli status
```
```bash
python -m datamind_console.db.schema_cli plan <unidad>
```
```bash
python -m datamind_console.db.schema_cli apply <unidad> --yes
```

`apply` ejecuta el SQL de migración contra la base configurada. `plan` muestra qué haría cada archivo, incluidos los cambios de datos. El código lee con los helpers de `datamind_console/db/db.py` (`query_all`, `read_only_connection`, `transaction`, …). Ver [docs/DB_ACCESS.md](docs/DB_ACCESS.md).

Para explorar la consola contra una base real sin ningún riesgo de escritura, dale a la sesión solo lectura por defecto: `options=-c default_transaction_read_only=on` en el DSN (codificado en URL), o `PGOPTIONS="-c default_transaction_read_only=on"`.

</details>

<details>
<summary><b>Servidor MCP (solo lectura)</b></summary>

`tools/mcp_server.py` expone la base de datos a clientes MCP. Cada sesión es de solo lectura con un límite de 30 s por consulta. `query_db` acepta una sola sentencia `SELECT` / `WITH` / `EXPLAIN` / `SHOW` / `VALUES` / `TABLE` y rechaza funciones con efectos (`dblink*`, `pg_terminate_backend`, `lo_*`, `set_config`, `pg_sleep`, …). Devuelve como máximo 500 filas.

</details>

<details>
<summary><b>Adapta el pipeline a tu región</b></summary>

La muestra vive en una región inventada (`sample_region`, "Sample City"). Para tu región reemplaza esta configuración:

| Qué | Dónde |
|---|---|
| Regiones activas, caja operativa, sesgos de geocodificación | `workspace/config/supported_provinces.json` |
| Cajas de extracción por sector, anclas, catálogo de POIs y etiquetas OSM | `phase1_nodes/catalogs/*.json` |
| Grupos de búsqueda geográfica | `datamind_console/common/nominatim_group_bias.json` |
| Jurisdicciones y reglas de calidad de la puesta en tierra de paradas | `datamind_console/phases/phase3_routes/stop_grounding/catalogs/` |
| Operadores y plantillas de nombres | `phase4_naming/catalogs/` |
| Servicios externos (Overpass, Valhalla, OpenSearch) | `.env` (`OVERPASS_URL`, `VALHALLA_URL`, `OPENSEARCH_URL`) |

Overpass y Valhalla se pueden levantar con `docker-compose.yml`. Edítalo para que apunte a un extracto OSM de tu región en `./data/overpass/`.

</details>

<details>
<summary><b>Tests</b></summary>

Corre las suites **de a una carpeta**, porque varios paquetes `tests/` comparten nombre. Por ejemplo:

```bash
python -B -m pytest -p no:cacheprovider datamind_console --ignore=datamind_console/phases/phase3_routes/tests
```
```bash
python -B -m pytest -p no:cacheprovider tests
```

Las otras carpetas son `datamind_console/phases/phase3_routes/tests`, `tools/tests`, `phase1_nodes`, `phase2_semantics`, `phase3_routes/diagnostics`, `local_runner/tests` y `phase3_routes/services/route_constructor/tests` (desde ese directorio). Ninguna necesita base de datos, AWS ni una API externa. Ver [docs/TEST_CLASSIFICATION.md](docs/TEST_CLASSIFICATION.md).

- Los tests que **necesitan una base con datos** se saltan solos. Actívalos con `DB_DSN=... DATAMIND_RUN_DB_TESTS=1` (suponen datos reales de una región, no la muestra). Los que escriben (papelera, revisión humana, evidencia de la fase 4) solo corren con `DATAMIND_DISPOSABLE_TEST_DSN` apuntando a una base cuyo nombre contenga `test`, `scratch` o `disposable`.
- Los tests escritos contra los archivos de datos de la región original (catálogo de lugares, catálogos de la fase 3, catálogo de bboxes) se reportan como **omitidos** mientras esos archivos no estén; ver `conftest.py`. Con los archivos de tu región, corren.

</details>

<details>
<summary><b>Seguridad</b></summary>

- Sin credenciales incrustadas: la conexión sale siempre de `DB_DSN` / `.env`. Si falta, el código falla con un mensaje claro (`datamind_core/dsn.py`).
- En modo local, expón la consola solo en `127.0.0.1`. Para acceso remoto usa el modo con login y sesiones.
- El asistente de IA solo abre una terminal local con un flag explícito (`ML_DATAMIND_LOCAL_CLI_LAUNCH_ENABLED`) y después de que revises el alcance.
- La API de la fase 4 exige `X-API-Key` (`PHASE4_API_KEY`) en los endpoints que escriben y limita CORS a la consola local.
- Secretos: `python tools/secret_scan.py --tracked` revisa cada archivo versionado y muestra huellas, nunca valores. `tools/git-hooks/pre-push` rechaza un push cuyos commits nuevos agreguen un secreto; instálalo con `cp tools/git-hooks/pre-push .git/hooks/pre-push`. Ver [docs/SECURITY_HYGIENE.md](docs/SECURITY_HYGIENE.md).
- El autopilot firma las decisiones del operador con `HADES_OPERATOR_SIGNING_SECRET`. Crea la tuya con `python -m datamind_console.orchestrator.signing_key --write .env`. Sin ella, la consola avisa que se está usando la clave pública de desarrollo.
- Antes de publicar tu fork, corre `gitleaks detect` y revisa que no subas `.env`, dumps ni datos.

</details>

---

## Qué incluye y qué no

| ✅ Incluye | 🚫 No incluye (a propósito) |
|---|---|
| El código de las cinco fases, la consola y HADES | Datos reales, ni catálogos de una región concreta |
| El esquema completo de la base (`db/schema.sql`) | Dumps de la base, salidas de corridas y logs |
| Una muestra sintética diminuta (3 líneas, 26 paradas, 6 lugares de una ciudad inventada) | Modelos entrenados (reentrena los modelos LightGBM con `ai_training/` sobre tu propia base) |
| Configuración de ejemplo y el logo de la consola (`datamind_console/ui/assets/`) | Teselas de Valhalla y extractos OSM |

> [!WARNING]
> **Limitaciones que debes conocer**
> - El pipeline se desarrolló para **una región concreta** (Quito, Ecuador). La configuración ya es de muestra, pero algunas heurísticas de nombres de lugares (por ejemplo en `geography_guardrails.py` y `geography_input_resolver.py`) y varios comentarios y tests siguen mencionando esa región. Adáptalos a la tuya.
> - `db/schema.sql` es un volcado saneado del esquema real. Los archivos de `*/migrations` y `*/sql` se conservan como **historial** de cambios, pero por sí solos no reconstruyen la base completa. Usa `scripts/init_db.py`.
> - Las fases 3 y 5 necesitan servicios reales (Valhalla, OTP) para correr de punta a punta. Sin ellos puedes explorar la consola y la muestra, pero no construir rutas nuevas.
> - Los exports GTFS del proyecto original todavía no pasan el validador de MobilityData, así que aquí no se presenta ningún feed de DataMind como validado. La consola muestra esa puerta en lugar de esconderla.

---

## Documentación

Los documentos de `docs/` están en inglés.

| Tema | Leer |
|---|---|
| Qué hace de verdad cada capacidad hoy (activa, heurística, experimental, heredada) | [docs/CURRENT_CAPABILITIES.md](docs/CURRENT_CAPABILITIES.md) |
| Qué significa cada número | [docs/METRICS.md](docs/METRICS.md) · [docs/TERMINOLOGY.md](docs/TERMINOLOGY.md) |
| Revisión humana y rastro de auditoría | [docs/REVIEW.md](docs/REVIEW.md) |
| Validación y publicación de GTFS | [docs/GTFS_VALIDATION.md](docs/GTFS_VALIDATION.md) |
| Evidencia y matching de la fase 4 | [docs/PHASE4_EVIDENCE_MATCHING.md](docs/PHASE4_EVIDENCE_MATCHING.md) |
| Orquestación (HADES) | [docs/ORCHESTRATION.md](docs/ORCHESTRATION.md) · [docs/hades_pipeline_autopilot.md](docs/hades_pipeline_autopilot.md) |
| Acceso a la base y seguridad | [docs/DB_ACCESS.md](docs/DB_ACCESS.md) · [docs/SECURITY_HYGIENE.md](docs/SECURITY_HYGIENE.md) |
| Operación | [OPS.md](OPS.md) |
| Licencias | [LICENSE](LICENSE) · [docs/COMMERCIAL-LICENSING.md](docs/COMMERCIAL-LICENSING.md) · [docs/legal/VERSIONS-AND-LICENSES.md](docs/legal/VERSIONS-AND-LICENSES.md) |

## Licencia

ML DataMind es de **código disponible** (*source available*) bajo la [ML DataMind Sustainable Use License](LICENSE) (borrador, pendiente de revisión jurídica).

Eres libre de inspeccionar, modificar y usar ML DataMind con fines internos, de investigación, educativos y operativos.

Ofrecer ML DataMind como servicio alojado, producto white-label, producto de reventa o un servicio comercial competidor sustancialmente equivalente requiere una licencia comercial.

Las apps están permitidas: una app que consume GTFS u otros Outputs ya generados con ML DataMind es válida, y una app interna puede invocar ML DataMind en vivo para las personas de su propia organización. Si clientes o usuarios externos pueden invocar la funcionalidad de ML DataMind bajo demanda a través de tu app o servicio, directa o indirectamente, se necesita una licencia comercial.

Las versiones publicadas antes de esta licencia (hasta el commit `33d2b20`, versión 0.1.0) siguen disponibles bajo la licencia MIT. Ver [LICENSE-MIT](LICENSE-MIT) y [Versiones y licencias](docs/legal/VERSIONS-AND-LICENSES.md).

¿Te interesa el uso comercial o una alianza? [Escríbenos](docs/COMMERCIAL-LICENSING.md) o a josuearcos1307@gmail.com.

¿Quieres contribuir? Empieza por [CONTRIBUTING.md](CONTRIBUTING.md).

## Autores

Josué Arcos y Jhair Jiménez
