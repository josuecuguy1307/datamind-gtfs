# Constructor V2 Valhalla

This repository keeps the legacy Phase 3 Valhalla service unchanged on `http://127.0.0.1:8002`.

Constructor V2 gets a separate experimental Valhalla instance on `http://127.0.0.1:8003`.

## Files

- Compose: `services/valhalla/docker-compose.constructor_v2.yml`
- Config: `services/valhalla/valhalla_constructor_v2.json`
- Runtime state: `services/valhalla/constructor_v2_runtime/`

## What It Reuses

- Image: `valhalla-local`
- Tiles: `services/valhalla/tiles` mounted read-only
- Extracts: `services/valhalla/extracts` mounted read-only

The Constructor V2 instance keeps its own runtime artifacts under `constructor_v2_runtime/` so it does not share writable state with the existing Valhalla container.

## Start

```bash
docker compose -f phase3_routes/services/route_constructor/services/valhalla/docker-compose.constructor_v2.yml up -d
```

## Stop

```bash
docker compose -f phase3_routes/services/route_constructor/services/valhalla/docker-compose.constructor_v2.yml down
```

## Verify

Route:

```bash
curl -sS -X POST http://127.0.0.1:8003/route \
  -H 'Content-Type: application/json' \
  -d '{"locations":[{"lat":-0.227916,"lon":-78.506679,"type":"break"},{"lat":-0.285132,"lon":-78.472331,"type":"break"}],"costing":"bus","shape_format":"geojson"}'
```

Optimized route:

```bash
curl -sS -X POST http://127.0.0.1:8003/optimized_route \
  -H 'Content-Type: application/json' \
  -d '{"locations":[{"lat":-0.227916,"lon":-78.506679,"type":"break"},{"lat":-0.2333,"lon":-78.5045,"type":"break"},{"lat":-0.2313,"lon":-78.5040,"type":"break"},{"lat":-0.285132,"lon":-78.472331,"type":"break"}],"costing":"bus","shape_format":"geojson"}'
```

Matrix:

```bash
curl -sS -X POST http://127.0.0.1:8003/sources_to_targets \
  -H 'Content-Type: application/json' \
  -d '{"sources":[{"lat":-0.227916,"lon":-78.506679},{"lat":-0.2313,"lon":-78.5040}],"targets":[{"lat":-0.2333,"lon":-78.5045},{"lat":-0.285132,"lon":-78.472331}],"costing":"bus"}'
```

Locate:

```bash
curl -sS -X POST http://127.0.0.1:8003/locate \
  -H 'Content-Type: application/json' \
  -d '{"locations":[{"lat":-0.227916,"lon":-78.506679}],"costing":"bus"}'
```

## Constructor V2 Targeting

Constructor V2 now prefers `CONSTRUCTOR_V2_VALHALLA_URL`, and the experimental default is `http://127.0.0.1:8003`.

For the dedicated instance:

```bash
export CONSTRUCTOR_V2_VALHALLA_URL=http://127.0.0.1:8003
```

If you need to force the legacy behavior for comparison:

```bash
export CONSTRUCTOR_V2_VALHALLA_URL=http://127.0.0.1:8002
```
