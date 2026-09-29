# AI Bot Ops Runbook (DataMind)

## 1) Check local Postgres health
```bash
python datamind_console/scripts/check_local_postgres.py --json
```

Expected:
- `db_probe.ok=true`
- `brew_services` shows `postgresql@17 started`
- no `stale_lock_suspected=true` for active data dir

## 2) If stale lock is detected (`postmaster.pid`)
Only when Postgres service is stopped and the PID is not a postgres process:
```bash
brew services stop postgresql@17
rm -f /opt/homebrew/var/postgresql@17/postmaster.pid
brew services start postgresql@17
```

## 3) Verify AI Insights DB-backed path
Read-only strict:
```bash
python datamind_console/scripts/verify_ai_insights_db.py --expect-db datamind_ml --strict
```

Write/read/cleanup strict:
```bash
python datamind_console/scripts/verify_ai_insights_db.py --expect-db datamind_ml --allow-write --strict
```

Safety guard:
- `--allow-write` requires `--expect-db`
- script exits if active DB does not match expected DB

## 4) Bring Valhalla up (for Phase3 Step30)
```bash
cd phase3_routes/services/route_constructor/services/valhalla
docker compose up -d
curl http://127.0.0.1:8002/status
```

Notes:
- `/status` may return 404 on this image and still be usable (health check treats `<500` as reachable).

## 5) 2-minute go-live check
```bash
python datamind_console/scripts/ai_bot_golive_check.py --days 7 --strict --json
```

Interpretation:
- `status=ready_with_warnings`: hard checks passed, soft gaps remain (usually label quality/operator feedback scale).
- `hard_failed` non-empty: not ready.

## 6) Phase3 E2E caution
Running Step40 approves geometry into `route_prod.routes`.
Use only on a route intended for approval.
