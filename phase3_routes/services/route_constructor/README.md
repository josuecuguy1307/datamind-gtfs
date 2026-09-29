# Phase 3 — Route Constructor (Geometry)

This service builds **route shapes (LineStrings)** from evidence (usually an OSM relation) + canonical stops.

## Folder layout
- sql/ : route_raw / route_work / route_prod schemas + tables
- src/ : python modules (evidence parsing, sequence candidates, geometry candidates)
- scripts/ : CLI steps (fetch evidence → build candidates → approve)

## Setup
1) Create a venv and install deps:
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -U pip
   pip install -e .

2) Copy env:
   cp .env.example .env
   # edit DB_DSN, VALHALLA_URL if needed

3) Apply SQL (run from this folder):
   psql "$DB_DSN" -f sql/001_route_raw.sql
   psql "$DB_DSN" -f sql/010_route_work_core.sql
   psql "$DB_DSN" -f sql/011_route_work_learning.sql
   psql "$DB_DSN" -f sql/020_route_prod_core.sql
   psql "$DB_DSN" -f sql/027_geometry_stop_recovery.sql

## Run pipeline (MVP)
1) Fetch OSM relation:
   python scripts/10_fetch_relation.py new <osm_relation_id>

2) Extract stop prior + build sequences:
   python scripts/20_build_stop_sequences.py <route_id>

3) Build geometry candidates for one sequence:
   python scripts/30_build_geometry_candidates.py <route_id> <stop_sequence_candidate_id>

4) Recover nearby canonical stops from each geometry candidate:
   python scripts/32_geometry_stop_recovery.py <route_id> <geometry_set_id>

5) Optionally rank the geometry candidates after stop recovery:
   python scripts/35_rank_geometry_candidates.py <route_id> <geometry_set_id>

6) Approve best geometry in a set (auto best-by-score):
   python scripts/40_approve_geometry.py <route_id> <geometry_set_id>

Result is written into:
- route_prod.routes (geom = final shape)
