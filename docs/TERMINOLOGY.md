# Terminology

The console uses one word per concept, in English and Spanish. The source of
truth for on-screen copy is `datamind_console/i18n/en.py` and `es.py` (the
`term.*` and `status.*` keys); this page explains what the words mean.
Technical identifiers (schemas, columns, env vars, package names) keep their
existing names.

## Core nouns

| English | Español | Meaning |
|---|---|---|
| stop | parada | a boarding point: a Phase 1 node of type stop |
| place | lugar | a Phase 2 semantic entity (landmark, sector, locality) a stop can be named after |
| route direction | sentido de ruta | one row of `route_prod.routes`: one direction (0/1) of a line, with its own geometry and stop sequence |
| line | línea | a logical service route (`service_route_id`) grouping its directions; one GTFS `routes.txt` entry |
| stop sequence | secuencia de paradas | the ordered `stop_node_ids` of a route direction |
| shape | trazado | the geometry a route direction follows (GTFS `shapes.txt`) |
| stop reference | referencia de parada | one occurrence of a stop in a stop sequence; a stop on five directions is five references |
| candidate | candidato | a proposed route, stop or name that has not been approved |
| evidence | evidencia | the sources supporting a candidate |
| coverage gap | brecha de cobertura | a stretch where the pipeline expected stops and found none |
| synthetic stop | parada sintética | a stop the pipeline generated to fill a gap; it needs review before it counts as observed |
| desk research | investigación documental | evidence gathered from documents rather than field data or OSM |
| run | ejecución | one execution of a pipeline step |
| phase | fase | one of the five pipeline stages below |

The word **route** alone is avoided in metrics because it was used for both
rows (directions) and lines. See [METRICS.md](METRICS.md).

## Phases

| | English | Español |
|---|---|---|
| 1 | Nodes | Nodos |
| 2 | Semantics | Semántica |
| 3 | Routes | Rutas |
| 4 | Naming | Nombres |
| 5 | GTFS | GTFS |

## States

**Deploy state** (`route_prod.routes.deploy_status`)

| Value | English | Español |
|---|---|---|
| `active` | Deployed | Desplegado |
| `excluded_quality` | Excluded (quality) | Excluido (calidad) |
| `quarantined_no_stops` | Quarantined (no stops) | En cuarentena (sin paradas) |
| `non_sample_region` | Outside Sample Region deployment | Fuera del despliegue de Sample Region |

**Cleanliness** (`cleanliness_status`): `clean` = Clean / Limpio, `dirty` =
Needs work / Requiere trabajo, NULL = Not classified / Sin clasificar.

**Re-entry** (`status` of the re-entry queue): `pending` = Queued / En cola,
`v2_ready` = Proposal ready / Propuesta lista, `swapped` = Shipped / Enviado,
`failed` = Closed without shipping / Cerrado sin envío. A closed item whose
`last_error` starts with `no_improvement` kept the current version on purpose;
the Review page shows it as *No improvement (current kept)*, not as an error.

**Approval**: pending, in progress, ready, approved, rejected (Pendiente,
En progreso, Lista, Aprobada, Rechazada).

## Validation words

These are distinct and never interchangeable. See
[GTFS_VALIDATION.md](GTFS_VALIDATION.md).

| English | Español | Means |
|---|---|---|
| Structure check | Comprobación de estructura | the zip has the required files and columns; DataMind's own fast check |
| DataMind quality check | Control de calidad de DataMind | DataMind's rules on its own data (stops, sequences, coverage); not a GTFS conformance check |
| MobilityData validator | Validador de MobilityData | the canonical `gtfs-validator`; the only layer that may say *valid GTFS* |
| Publish-ready | Listo para publicar | structure check passed **and** the MobilityData validator reported 0 errors for this exact file |

"Validated" is never shown unless the MobilityData validator ran on that file
and reported no errors. Publishing stays a human action.

## Spanish style

Spanish copy keeps its accents (*Revisión*, *Configuración*, *Español*). A
test (`test_spanish_keeps_its_accents`) rejects the stripped forms the old
catalog produced.
