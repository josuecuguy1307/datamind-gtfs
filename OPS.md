# GTFS Ops (Local + AWS)

This dashboard adds a dedicated **GTFS Ops (Local + AWS)** screen in the left navigation.

## Required environment variables

Set these in `<repo>/.env`:

### Local publish
- `LOCAL_GTFS_DB_DSN` (recommended for the downstream app DB target)
- or `LOCAL_DB_DSN` (used only if it already points to the downstream app DB)
- fallback: auto-detect from `GTFS_APP_ROOT/.env` (`PGUSER/PGPASSWORD/PGDATABASE` + `PG_HOST_PORT`)
- `LOCAL_GTFS_IMPORT_CMD` (optional; if empty, widget uses built-in importer from extracted GTFS TXT files)
- `LOCAL_GTFS_EXTRACT_DIR` (optional; defaults to OTP router dir if found, else `GTFS_APP_ROOT/gtfs_data`)
- Optional: `LOCAL_DB_REFRESH_SQL` (default: empty/skip)

### AWS publish
- SSH auth (one strategy):
  - `AWS_SSH_CONFIG_NAME`, or
  - `AWS_SSH_HOST` + `AWS_SSH_USER` + `AWS_SSH_KEY_PATH`
- GTFS apply target:
  - `AWS_GTFS_EXTRACT_DIR`, or
  - `AWS_GTFS_APPLY_CMD`
  - if both are empty, the widget auto-detects common paths under `/opt/gtfs_app` and `/opt/gtfs_app`
- DB import/refresh strategy:
  - optional `AWS_DB_IMPORT_CMD` (if set, used before OTP build)
  - refresh: `AWS_DB_REFRESH_CMD`, or
  - `AWS_DB_DSN`
  - optional fallback: set `LOCAL_DB_REFRESH_SQL` and the widget can execute it remotely via SSH + `docker exec postgres psql`

### AWS backend push (separate button)
- `BACKEND_PUSH_LOCAL_DIR` (optional; defaults to `GTFS_APP_ROOT`)
- `AWS_BACKEND_REMOTE_DIR` (optional; defaults to `AWS_TRANSPORTAPP_DIR`)
- `AWS_BACKEND_DEPLOY_CMD` (optional; explicit remote deploy command after upload)
- `AWS_BACKEND_SERVICE_NAME` (default: `backend`, used for compose/container detection)
- `AWS_BACKEND_SYSTEMD_UNIT` (default: `backend`, used for systemd fallback)
- `AWS_BACKEND_DEPLOY_SERVICES` (default: `backend,otp`, used in compose deploy mode)

### OTP controls (optional but recommended)
- `OTP_LOCAL_MODE` / `OTP_AWS_MODE` (`compose`, `container`, `systemd`)
- `OTP_LOCAL_BUILD_CMD` / `OTP_AWS_BUILD_CMD`
- `OTP_LOCAL_BUILD_XMX` / `OTP_AWS_BUILD_XMX` (Java heap for graph build, e.g. `2G`, `3G`)
- `OTP_LOCAL_HEALTH_URL` / `OTP_AWS_HEALTH_URL`
- `AWS_ALLOW_REMOTE_OTP_BUILD` (default `false`; when `false`, AWS publish requires local `graph.obj` and skips remote build)

### Local API preflight (optional overrides)
- `LOCAL_BACKEND_BASE_URL` (default `http://127.0.0.1:3000`)
- `BACKEND_HOST_PORT` (used when `LOCAL_BACKEND_BASE_URL` is not set)

## Runtime behavior

1. Upload GTFS ZIP in the widget.
2. Click **Validate GTFS**.
3. Click **Run LOCAL publish**:
   - saves zip
   - validates
   - runs `LOCAL_GTFS_IMPORT_CMD`
   - runs DB refresh SQL
   - updates OTP (detect + build + restart)
4. Click **Run ALL APIs** (required by default before AWS actions):
   - verifies OTP router health endpoint
   - verifies OTP `/otp/routers/default/index/stops` returns valid JSON
   - verifies backend core + search + geo + line/station APIs already mounted in your local server
   - if one check fails, final run-all result is `false`
   - if all pass, status is `STATUS_AWESOME`
5. Click **Run AWS full deploy** (single AWS action, requires confirmation checkbox):
   - GTFS upload is optional if local bundle already has updated GTFS (`latest_upload_gtfs.zip` or GTFS txt files)
   - step 1/2 runtime deploy:
     - always packages local Docker context from `GTFS_APP_ROOT` (not backend-only folder)
     - hard-fails before upload if required runtime artifacts are missing (`scripts/otp-start.sh`, `otp/otp.jar`, `otp/otp-data/routers/default/graph.obj`)
     - uploads to EC2 and updates remote files (keeps `.prev` backups for replaced directories)
     - replaces remote `otp/otp-data/routers/default` with uploaded local bundle copy (not merge overlay)
     - deploys runtime via compose/container/systemd auto-detection (or `AWS_BACKEND_DEPLOY_CMD`)
   - step 2/2 GTFS+DB+OTP deploy:
     - uploads GTFS over SSH/SCP
     - applies GTFS on EC2 (configured or auto-detected dir)
     - imports GTFS into AWS transit DB (built-in mapped importer or `AWS_DB_IMPORT_CMD`)
     - uploads local prebuilt `graph.obj` when available **and fresher than the uploaded GTFS ZIP** (run LOCAL publish first)
     - by default, does **not** run long remote EC2 graph build unless `AWS_ALLOW_REMOTE_OTP_BUILD=true`
     - updates OTP (restart; build only when explicitly allowed and needed)
     - runs AWS DB refresh/FTS
6. Use **Check LOCAL status** / **Check AWS status** for health snapshots.
   - AWS DB status auto-falls back to SSH + `docker exec postgres psql` when `AWS_DB_DSN/AWS_DB_STATUS_CMD` are not set.
7. In **Dashboard** page, use **Run ALL APIs** for one-click local preflight from the home screen.

## OTP deterministic startup

Local/AWS compose now starts OTP through `gtfs_app/scripts/otp-start.sh`:

- auto-creates `/otp/otp-data/routers/default`
- validates that `/otp/otp.jar` is mounted
- fails early with clear logs if `graph.obj` is missing (instead of opaque restart-loop behavior)
- keeps bind mounts on:
  - `./gtfs_data -> /otp/gtfs_data`
  - `./osm_data -> /otp/osm_data`
  - `./otp/otp-data -> /otp/otp-data`

## Safe AWS deploy checklist

### Stop/remove scope

- Stop only app services first: `backend`, `otp`
- Keep data services running when possible: `postgres`, `redis`

### Replace scope

- Replace app/runtime content:
  - `BACKEND/`
  - `config/`, `init/`, `scripts/`
  - `otp/otp.jar`
  - `otp/otp-data/routers/default` (router files + `graph.obj`)
  - `gtfs_data/` and `osm_data/` only when intended

### Do not delete

- Do **not** delete Postgres/Redis named volumes (`pgdata`, `redisdata`)
- Do **not** run destructive compose commands with `-v` on shared data containers

### Post-deploy health checks

- EC2 local OTP:
  - `curl -fsS http://127.0.0.1:8080/otp/routers/default/index/stops`
- EC2 local backend:
  - `curl -fsS http://127.0.0.1:3000/health`
  - `curl -fsS 'http://127.0.0.1:3000/api/routes?fromLat=-0.21&fromLon=-78.50&toLat=-0.20&toLon=-78.49&profile=walk&forceFresh=true'`
- Public domain:
  - `https://<your-domain>/health`
  - `https://<your-domain>/api/routes?...`

## Session state keys

- `ops.gtfs.last_file_path`
- `ops.gtfs.last_checksum`
- `ops.local.last_run`
- `ops.aws.last_run`
- `ops.logs.local`
- `ops.logs.aws`
- `ops.status`

## GTFS Approval Gate + WhatsApp (Meta Cloud API)

GTFS exports are approval-gated in `gtfs.gtfs_artifacts`:

1. Phase5 package step creates a `pending_approval` artifact (with `approval_token`).
2. Dashboard `GTFS Ops -> GTFS Exports` shows pending items for approve/reject.
3. Download is blocked until status is `approved` or `downloaded`.
4. Desktop downloader (`scripts/gtfs_desktop_downloader.py`) pulls only approved artifacts and marks them downloaded.

### Required env flags

- `WHATSAPP_ENABLED=false` (default OFF)
- `WHATSAPP_ADMIN_NUMBER=`
- `META_WA_PHONE_NUMBER_ID=`
- `META_WA_ACCESS_TOKEN=`
- `META_WA_VERIFY_TOKEN=`
- `PUBLIC_WEBHOOK_BASE_URL=`
- `DATAMIND_BASE_URL=http://127.0.0.1:8006`
- `DATAMIND_API_KEY=` (optional)
- `DOWNLOAD_DIR=~/Downloads/GTFS`

### Meta webhook behavior

- Verify URL: `GET /api/whatsapp/inbound` (`hub.verify_token` must match `META_WA_VERIFY_TOKEN`)
- Inbound URL: `POST /api/whatsapp/inbound`
- Authorized sender: must match `WHATSAPP_ADMIN_NUMBER`
- Approval command must match exactly:
  - `APPROVED <TOKEN>`
  - regex: `^APPROVED\\s+([A-Z0-9]{4,10})$`

### Cloudflare Tunnel quick setup (local webhook exposure)

1. Install/login `cloudflared`.
2. Run a tunnel to local backend (example backend on `8006`):
   - `cloudflared tunnel --url http://127.0.0.1:8006`
3. Copy generated public URL and set:
   - `PUBLIC_WEBHOOK_BASE_URL=https://<your-tunnel-host>`
4. In Meta WhatsApp app webhook config:
   - Callback URL: `https://<your-tunnel-host>/api/whatsapp/inbound`
   - Verify token: same value as `META_WA_VERIFY_TOKEN`
5. Subscribe to WhatsApp message events.

### launchd (macOS downloader)

1. Copy and edit `scripts/com.datamind.gtfsdownloader.plist`:
   - replace `REPLACE_USER`
   - confirm workspace path and `.venv/bin/python`
2. Load service:
   - `launchctl unload ~/Library/LaunchAgents/com.datamind.gtfsdownloader.plist 2>/dev/null || true`
   - `cp scripts/com.datamind.gtfsdownloader.plist ~/Library/LaunchAgents/com.datamind.gtfsdownloader.plist`
   - `launchctl load ~/Library/LaunchAgents/com.datamind.gtfsdownloader.plist`
3. Logs:
   - `tail -f ~/Library/Logs/gtfs-downloader.log`

## Geo API Production

You now have a dedicated dashboard page: **Geo API Production**.

### New env controls

Set these in `<repo>/.env`:

- `GEO_API_REQUIRE_AUTH=false`  
  If `true`, `/api/geo/geocode|autocomplete|reverse` require API key.
- `GEO_API_API_KEYS=`  
  Comma-separated API keys for Geo API auth.
- `GEO_API_RATE_LIMIT_PER_MIN=0`  
  Optional in-memory per-client rate limit. `0` disables limit.
- `GEO_API_PUBLIC_HEALTH=true`  
  If `false`, `/api/geo/health` also requires auth.

### Auth headers

When auth is enabled, send one of:

- `Authorization: Bearer <API_KEY>`
- `X-API-Key: <API_KEY>`

### Compatibility note

`gtfs_app/BACKEND/src/search/otpController.js` now supports auth headers for Geo API calls.
Set one of:

- `GEO_API_AUTH_HEADER=Bearer <API_KEY>`
- `GEO_API_API_KEY=<API_KEY>`

## Geo API Fusion (single API path)

You now also have a dedicated dashboard page: **Geo API Fusion**.

Purpose:
- validate that `gtfs_app` and DataMind Geo API are fused on one contract (`/api/geo/*`)
- run one-click checks for:
  - `/api/geo/health`
  - `/api/geo/geocode`
  - `/api/geo/autocomplete`
  - `/api/geo/reverse`
  - compatibility alias `/api/search/geocode`

Recommended backend env in `gtfs_app/.env`:
- `GEO_API_BASE_URL=http://127.0.0.1:3000` (or your API host)
- `GEO_API_GEOCODE_PATH=/api/geo/geocode`
- optional auth:
  - `GEO_API_AUTH_HEADER=Bearer <API_KEY>`
  - or `GEO_API_API_KEY=<API_KEY>`

Implementation note:
- `BACKEND/src/search/otpController.js` now uses a **single** geocoder path (`/api/geo/geocode`) with no legacy fallback chain.
- `BACKEND/src/routes/searchRoutes.js` maps `/api/search/geocode` and `/api/search/reversegeocode` to the same Geo API handlers, so behavior stays unified.
