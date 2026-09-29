# Route Constructor — Assumptions, Rules & Conventions

## Core Assumptions

### About the transit system
1. All 39 routes are **one-way sequences** (ida only). The vuelta (return) is a separate direction_id
2. Each route belongs to exactly ONE cooperative
3. "Interno" in a route name means it operates only within a local zone — it does NOT mean circular
4. CALSIG routes are LINEAR feeders along the Av. de los Volcanes — there is only one road
5. Bus routes follow actual roads — they don't cut across blocks or fields
6. A bus route does NOT zigzag between parallel streets kilometers apart

### About the pipeline
7. The seed catalog is the source of truth for route NAMES and STRUCTURE
8. The reviewed sequences JSON is the source of truth for stop ORDER
9. Valhalla faithfully follows the waypoint order — garbage in = garbage out
10. Step 30 is the canonical geometry builder — do not create standalone scripts
11. The coherence validator runs BEFORE geometry generation, not after
12. Retrocompatibility: the original 19 routes must never regress

### About Valhalla
13. Costing `bus` allows bus-only roads and U-turns
14. `through` waypoints = Valhalla passes through without stopping; `break` = generates a leg
15. If Valhalla fails with all stops, fallback to terminus-only (first + last as `break`)
16. Circular routes: append first stop at end as `break` to close the loop

## Critical Rules

### Never do this
- ❌ Never put Autopista stops (Orquídeas, Jardín del Valle, Puentes) on Puengasí routes
- ❌ Never mix ida (outbound) and vuelta (return) stops in the same sequence
- ❌ Never have Velasco Ibarra after Puente 8 — that's a 6km backtrack to Marín
- ❌ Never assume a CALSIG route is circular just because it's "internal"
- ❌ Never hardcode stop names or coordinates in pipeline code
- ❌ Never create standalone Valhalla scripts outside the pipeline
- ❌ Never reorder stops inside Step 30 — they come pre-ordered from the reviewed JSON

### Always do this
- ✅ Always run the coherence validator before Valhalla
- ✅ Always check terminus positions (should be first and last, not mid-sequence)
- ✅ Always dedup stops with same name < 100m apart
- ✅ Always verify the road order table for Autopista stops
- ✅ Always keep synthetic terminus stops (first and last)
- ✅ Always save Valhalla debug responses for troubleshooting

## File Naming Conventions

```
valle_v5_4_*             — Version 5.4 catalog family
*_FINAL_REVIEWED.json    — Human-reviewed sequences
*_COHERENT_*.json        — After coherence validator
*_WITH_GEOMETRIES.json   — Has Valhalla geometries
*_GEOM_v{N}.json         — Geometry iteration number
PROMPT_{letter}_{name}.md — Claude Code prompt
MCP_{NN}_{name}.md       — MCP reference document
```

## Version History

| Version | Routes | Key change |
|---------|--------|------------|
| v5.0 | 19 | Initial catalog |
| v5.2 | 19 | Text-based waypoints, 29/39 usable |
| v5.3 | 39 | Added coordinate waypoints (some regressions) |
| v5.4 | 39 | Hybrid best-of v5.2+v5.3 per route |
| v5.4-overrides-v1.0 | 39 | First sequence overrides (24 routes) |
| v5.4-overrides-v1.3 | 39 | Zigzag fixes, Marín reordering, dedup |
| v5.4-fixed-coherent-v16 | 39 | Coherence validator applied, geometries produced |
| v5.4-fixed-coherent-v17 | 39 | 38 pair swaps, backtrack removals, final clean |

## Glossary

| Term | Meaning |
|------|---------|
| **pf (path_fraction)** | Position along route, 0.0 = start, 1.0 = end |
| **on_route_score** | Confidence that a stop belongs to this route (0-1) |
| **corridor_km** | Expected one-way route length from the catalog |
| **geometry_km** | Actual length of Valhalla-produced geometry |
| **break point** | Valhalla waypoint where routing starts/ends a leg |
| **through point** | Valhalla waypoint the route passes through without breaking |
| **synthetic stop** | Stop created manually (not from DB), has `synthetic` in stop_id |
| **anchor** | Verified geographic reference point in the seed catalog |
| **terminus** | Start or end point of a route |
| **loop_origin** | Start/end point of a circular route (same location) |
| **HADES** | Automation rule for triggering Claude Code patches on failures |
