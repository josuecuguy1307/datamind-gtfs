# Current capabilities: what DataMind actually runs

Internal source of truth for UI copy, documentation and any public claim.

## Status vocabulary

| Status | Meaning |
|---|---|
| **ACTIVE** | Runs in the current production path and its output is used |
| **ACTIVE HYBRID** | Runs, but its output is blended with, or mostly replaced by, rules or a fallback |
| **HEURISTIC** | Deterministic rules or hand-set weights (possibly *called* a "model" in code or docs) |
| **EXPERIMENTAL** | Code exists for trials; not wired into the production path |
| **LEGACY** | Superseded path kept for compatibility |
| **PRESENT BUT UNUSED** | Code or artifact exists, but production never uses it, or it cannot run |
| **DOCUMENTED BUT NOT VERIFIED** | Claimed in docs or skills; not confirmed in code or data |

## Pipeline algorithms

| Capability | Status | What actually happens | Evidence |
|---|---|---|---|
| Overpass extraction | ACTIVE | `.ql` templates per canton or bbox against a self-hosted Overpass; runs stored in `node_raw.overpass_runs` | `phase1_nodes/datamind/services/openmaps_extractor/src/overpass/*`, `extract_job.py` |
| PostGIS DBSCAN (stop clustering) | ACTIVE | `ST_ClusterDBSCAN(ST_Transform(geom,3857), eps 35 m, minpoints 3)`. **Noise points are kept** as singleton clusters (no noise removal) | `src/pipeline/cluster_job.py:14,63` |
| Stop vs not-a-stop typing | HEURISTIC | Production `node_type` is `CASE WHEN tag_kind='poi' THEN 'POI' ELSE 'STOP'`, the OSM tag. **No model decides it.** | `src/pipeline/promote_job.py:118-121` |
| Phase 1 baseline heuristic `baseline_v0` | ACTIVE | `confidence = 0.30 +0.25 name +0.15 ref +0.10 operator +0.10 station −0.25 POI keys`; it is the fallback when LightGBM is unavailable. **94% of stored predictions** (485,758 of 514,735) | `src/features/node_features.py`; `node_work.node_features.model_version` |
| Phase 1 STOP/POI LightGBM v3 | ACTIVE HYBRID | Called in `features_job` with a heuristic fallback; produced 28,977 predictions (5.6%). Used only to choose each cluster's representative (`resolve`) and for the promote threshold (conf ≥ 0.3). Labels are OSM `tag_kind`, and the `confidence_v0` feature partly derives from the same tags, so the AUC 0.9575 measures agreement with OSM tagging, not ground truth | `src/models/stop_poi/lgbm.py`, `train_stop_classifier.py`, `stop_poi_lgbm_v3_meta.json` |
| `models/registry.py` routing (`ACTIVE_STOP_POI_MODEL="baseline"`) | LEGACY | Not on the path `features_job` uses | `src/models/registry.py:18-19` |
| Phase 1 GPBoost STOP/POI | EXPERIMENTAL | Training script and meta only | `train_stop_classifier_gpboost.py` |
| Phase 1 node-set ranking | HEURISTIC | `rank_v0_heuristic` (counts and ratios). The LightGBM set ranker is not called | `src/pipeline/rank_sets_job.py:39` |
| Multilingual embeddings | ACTIVE | `paraphrase-multilingual-MiniLM-L12-v2`, 384-d in pgvector. **1,666,259 of 2,966,858 aliases embedded (56%)** | `phase2_semantics/src/pipeline/embeddings/*`; `geo_prod.place_alias_embeddings` |
| Hybrid semantic search | ACTIVE | `0.42·cosine + 0.33·trigram + 0.20·ts_rank + 0.05·exact`, lexical fallback; serves the Geo API geocode and autocomplete endpoints | `phase2_semantics/src/pipeline/search/semantic_search.py`; `Phase2Client.semantic_search_*` |
| OpenSearch index | LEGACY | Step 50 is now a Postgres refresh | `phase2_semantics/scripts/run_all.py` |
| Phase 2 name ranker v1 | HEURISTIC | Never trained: registry `method=fallback_default`, `n_pos=0`, `n_neg=0`; runs the hand-set default weights | `geo_work.model_registry`; `src/pipeline/naming/name_ranker.py` |
| Phase 2 place-type model v1 | HEURISTIC (degenerate) | Difference of means; the STOP weights are all 0 | `geo_work.model_registry` |
| Contextual stop names | HEURISTIC | Cascade: intersection → landmark → sector → extended sector → orphan | `phase2_semantics/.../contextual_name_generator.py` |
| Valhalla routing | ACTIVE (needs the service) | `/route` (costing `bus`), `/locate`, `/sources_to_targets`, `/optimized_route`; `/trace_*` only in a quality-gate fixer and Runtime Lab | `route_constructor/src/constructor_v2/clients/*`, `geometry/valhalla_client.py` |
| Stop grounding corridor ∩ stops | ACTIVE | Progressive buffers 50/100/150 m (≤12 km), 50/100/200 m (≤25 km), else 50/100/200/400 m; configured in the **untracked** `stop_grounding/catalogs/stop_grounding_config.json` | `stop_grounding/corridor_stop_intersector.py` |
| Phase 3 on-route classifier | ACTIVE HYBRID | Ensemble: **0.8 heuristic + 0.2 LightGBM**; the model has **2 trees** and was trained on 22 routes. The model file is gitignored. The "AUC 0.98" report is an older 16-feature run, not this artifact | `stop_grounding/on_route_classifier.py`, `models/on_route_lgbm{.txt,_meta.json}` |
| OR-Tools sequencing | ACTIVE | Constructor V2: Valhalla matrix → fixed-end routing (12 s) → adjacent-swap refiner; replaces the baseline only if confidence improves by > 0.5 | `constructor_v2/pipeline.py:83-190`, `ordering/*` |
| CP-SAT path | EXPERIMENTAL (opt-in) | Runs only with `solver_mode="cpsat"` or `"both"`; the default is `"ortools"` | `constructor_v2/pipeline.py:42,123` |
| Constructor V2 validators | ACTIVE | Detour (strong ≤ 2.2, acceptable ≤ 3.2), repeats ≤ 12%, regression ≤ 3%, corridor switches ≤ 3, duplicates 60 m | `constructor_v2/constants.py`, `validation/*` |
| Route dedup | ACTIVE | Weights: ref .35 / terminus .25 / operator .15 / geometry .15 / stops .10; matched ≥ 0.70, ambiguous ≥ 0.40 | `route_constructor/src/dedup/route_matcher.py` |
| HADES stop-coverage enforcer | ACTIVE | Zone inferred from length; per-zone gap thresholds; tier ladder (borrow → Overpass POI → deep-research query → synthetic, recorded but not inserted) | `hades/enforcers/stop_coverage_enforcer.py` |
| HADES geometry enforcer and policy engine | ACTIVE | Flags implausible shapes; the policy engine *decides* and does not apply | `hades/enforcers/geometry_enforcer.py`, `policy_engine.py` |
| Deep-research gap validator ("DR Type 2") | ACTIVE (human-run) | Prompt export → **a person** pastes answers → parse → distance + Overpass + public Nominatim. `final = 0.5·responder self-confidence + 0.5·validation`; ≥ 0.6 accept | `hades/enforcers/dr_*.py` |
| DR batching | ACTIVE | scikit-learn KMeans over gap coordinates | `hades/enforcers/dr_stop_coverage_clusterer.py` |
| GREEK ship transaction | ACTIVE | β lock → α orphan cleanup → γ refill validation → δ atomic swap into `route_prod`; via the Re-Entry Review UI (preview → confirm / override with reason) or `scripts/bulk_ship_greek.py` | `hades/enforcers/re_entry_swap_service.py`, `stop_quality/stop_treater.py` |
| Phase 4 matching | HEURISTIC | Weighted: stop overlap .55, token overlap .15, ref .10, relation overlap .10, alias .05, name length .05 | `phase4_naming/phase4_semantics/matching/scoring.py` |
| Phase 4 naming confidence | HEURISTIC + research | `0.30 + 0.10 × filled_fields`, overwritten by deep-research values when higher; values are discrete | `phase4_semantics/scripts/05_bridge_osm_to_catalog.py` |
| Phase 4 LightGBM LambdaRank | PRESENT BUT UNUSED | `phase4_models/lgbm/v1/model.txt` is **0 bytes**; meta `train_rows: 0`, ndcg 0 | `phase4_naming/phase4_models/lgbm/v1/` |
| Direction / headsign | HEURISTIC | Direction slots 0/1 per service route; trips built from them | `012_route_direction_gate.sql`, `phase5_gtfs/compiler/build_trips.py` |
| Runtime Lab (ETA-like runtimes) | HEURISTIC | Valhalla leg times + dwell model + variance model, parameterized by deep-research data; not ML | `phase5_gtfs/runtime_lab/*` |
| GTFS compile | ACTIVE | `calendar`, `feed_info`, `routes`, `shapes`, `stops`, `trips`, `stop_times` → `gtfs_work` | `phase5_gtfs/compiler/*` |
| GTFS validation | See below | Layered; the MobilityData validator is not part of the automatic path | |
| Direction-aware 7-rule stop dedup | DOCUMENTED BUT NOT VERIFIED | Skill document; the route-pair variant is a deferred stub | `pipeline/phase_4_5/pair_detection/seven_rule_route_pair.py` |

## GTFS validation layers

| Layer | Status | Checks | Where |
|---|---|---|---|
| Structure check | ACTIVE | ZIP integrity, required files and columns, UTF-8, NUL bytes, lat/lon ranges, calendar presence | `datamind_console/ops/gtfs_validate.py` (gates local publish) |
| DataMind quality check (DB) | ACTIVE | Non-empty tables, orphan `stop_id`/`trip_id` in `gtfs_work` | `phase5_gtfs/validator/gtfs_validate.py` |
| Pre-deploy enforcer | ACTIVE, **AWS path only** | Expected counts and consistency before AWS deploy; local publish does not run it | `datamind_console/ops/pre_deploy_enforcer.py`, `ops/aws_publish.py:201` |
| MobilityData GTFS validator | Explicit action only | `tools/gtfs-validator.jar` (untracked) is not called automatically. Phase 2 adds an explicit button; see `docs/GTFS_VALIDATION.md` | — |

## ML and "AI" components outside the pipeline

| Component | Status | Notes |
|---|---|---|
| AI bot decision engine (`ai_agent/`) | PRESENT BUT UNUSED | `AI_DECIDER` defaults to `heuristics`; `ai.ai_model_registry` is empty; `ai.ai_suggestions` has 0 rows |
| AI bot LogisticRegression / MLP | PRESENT BUT UNUSED | `ai_training/train_baseline.py`, `train_mlp.py`; nothing registered |
| `ai.ai_bot_run_logs` (1,070 rows) | ACTIVE (misnamed) | A pipeline step-run log (`step_05_discover`, `step_cluster`, …), not AI activity |
| `ai_insights` quality / sequence-risk scores | ACTIVE HYBRID → heuristic | `ml_adapter.predict` falls back because `phase1_quality_score_lgbm.txt` and `phase3_sequence_risk_lgbm.txt` do not exist |
| Pipeline autopilot (`orchestrator/pipeline_autopilot.py`, 15.6 k lines) | PRESENT, dormant, no UI | Automation levels AUTO_SAFE / AUTO_WITH_CHECKPOINT / APPROVAL_REQUIRED / MANUAL_ASSISTED. Last `console.orchestrator_sessions` row: 2026-03-03. **There is no "Operator Orchestrator" screen**: commit `1fb6a471`, "target view never wired" |

## LLM, agents and MCP

See `docs/ORCHESTRATION.md` for the full separation.

| Capability | Status | Default |
|---|---|---|
| Constructor LLM advisor (OpenAI Responses API) | ACTIVE code, advisory only | Discovery calls it with `run_llm_advisory=False, llm_mode="mock"`; no DB writes |
| ChatGPT copilot / advisory API (`api_chatgpt/`) | ACTIVE code, advisory only | `DATAMIND_CHATGPT_MOCK_MODE` defaults to `true` |
| Phase 4 document extraction (`gpt-4o-mini`) | DOCUMENTED BUT NOT VERIFIED in the current flow | `phase4_semantics/ingest/docs/extract_structured.py` |
| Skills (`workspace/skills/`, 50 files) | ACTIVE as operator / agent procedures | How phases were actually run: a coding agent in a terminal, with operator approval |
| MCP server (`tools/mcp_server.py`, 18 tools) | ACTIVE developer tooling, read-only | Session-level read-only, single-statement guard. It is not product functionality and does not run engines |
| Local runner (`claude -p` / `codex exec`) | PRESENT, dormant | Last jobs 2026-03 |
| AI Assistance page | ACTIVE | Opens an interactive local CLI only with `ML_DATAMIND_LOCAL_CLI_LAUNCH_ENABLED=true`, LOCAL mode and a written scope |

## Human gates

| Gate | Status | Notes |
|---|---|---|
| Phase 1 `nodes_resolved` approval | Bypassed in practice | `scripts/bulk_approve_promote.py` approves all `work` rows; `run_phase1.py --auto-approve` |
| Phase 2 approval | Script | `phase2_semantics/scripts/30_approve.py` |
| Phase 3 Re-Entry Review | ACTIVE | Preview → confirm / override with required reason. Bulk alternative: `scripts/bulk_ship_greek.py` |
| Synthetic stop review | Pending | `node_prod.nodes.synthetic_review_state='pending'`: 2,423 rows |
| GTFS publish | ACTIVE | GTFS Ops: local publish gated by the structure check; AWS deploy needs a confirmation checkbox and runs the pre-deploy enforcer |
| Decision log `console.phase_decisions` | PRESENT BUT UNUSED → wired to Re-Entry Review in Phase 2 | 0 rows. The schema (phase 1–4, APPROVE/REJECT/EDIT/PUBLISH, reason required for REJECT/EDIT) was written for the per-phase approve tabs. Those tabs called `log_phase_decision` and were deleted in `e0fd494a` (2026-09-28); with 0 rows, they never logged a real decision. Review outcomes live in queue rows (`approval_queue.resolved_by` / `resolution_notes`, `refill_audit`, `cleanup_override_audit`). Phase 2 adds best-effort decision logging behind the Re-Entry Review actions; see `docs/REVIEW.md` |
