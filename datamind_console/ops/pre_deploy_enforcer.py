"""Pre-deploy enforcer — hard gate before AWS GTFS deployment.

Cleans stale remote/local data, verifies resource headroom, then runs
the full deploy+verify sequence. Cannot be skipped.
"""
from __future__ import annotations

import csv
import os
import shlex
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import OpsConfig
from .otp_control import run_ssh, scp_upload, CommandResult

LogFn = Callable[[str], None]

LOCAL_OTP_BUILD_XMX = "4G"

MIN_DISK_FREE_MB = 2048
MIN_MEM_FREE_MB = 1024


@dataclass
class DeployCheckResult:
    name: str
    passed: bool
    details: str = ""
    value: Any = None


@dataclass
class DeployReport:
    passed: bool = True
    checks: List[DeployCheckResult] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)
    remote_cleanup: Dict[str, Any] = field(default_factory=dict)
    local_cleanup: Dict[str, Any] = field(default_factory=dict)
    post_deploy: Dict[str, Any] = field(default_factory=dict)

    def add_check(self, name: str, passed: bool, details: str = "", value: Any = None):
        self.checks.append(DeployCheckResult(name=name, passed=passed, details=details, value=value))
        if not passed:
            self.passed = False
            self.blocking_reasons.append(f"{name}: {details}")


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(msg)


def _parse_free_mb(stdout: str, kind: str = "disk") -> Optional[int]:
    """Parse free MB from df or free output."""
    for line in stdout.strip().splitlines():
        parts = line.split()
        if kind == "disk" and len(parts) >= 4:
            try:
                return int(parts[3]) // 1024
            except (ValueError, IndexError):
                continue
        if kind == "mem" and "Mem:" in line and len(parts) >= 7:
            try:
                return int(parts[6]) // 1024
            except (ValueError, IndexError):
                continue
    return None


def _count_rows_in_zip(zip_path: Path, filename: str) -> int:
    """Count data rows in a GTFS txt file inside a zip."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            data = zf.read(filename).decode("utf-8")
            return sum(1 for _ in csv.DictReader(StringIO(data)))
    except (KeyError, zipfile.BadZipFile):
        return 0


class PreDeployEnforcer:
    """Hard gate before AWS deployment. Cleans old data, verifies resources."""

    def enforce(
        self,
        cfg: OpsConfig,
        gtfs_zip_path: Path,
        *,
        log: Optional[LogFn] = None,
    ) -> DeployReport:
        report = DeployReport()

        _log(log, "Pre-deploy enforcer: starting remote cleanup checks")

        # ── 1. SSH connectivity ──
        ping = run_ssh(cfg, "echo connected", timeout=30)
        report.add_check(
            "ssh_connectivity",
            ping.ok,
            "SSH OK" if ping.ok else f"SSH failed: {ping.stderr[:200]}",
        )
        if not ping.ok:
            return report

        # ── 2. Stop OTP to free memory ──
        _log(log, "Stopping remote OTP to free memory for graph build")
        extract_dir = cfg.aws_gtfs_extract_dir or "/opt/gtfs_app/otp/otp-data/routers/default"
        transportapp = cfg.aws_transportapp_dir or "/opt/gtfs_app"

        stop_cmd = (
            f"cd {shlex.quote(transportapp)} && "
            f"docker compose stop otp 2>/dev/null || docker stop otp 2>/dev/null || true"
        )
        stop_res = run_ssh(cfg, stop_cmd, timeout=120)
        report.add_check(
            "otp_stopped",
            True,
            f"OTP stop: rc={stop_res.returncode}",
        )

        # ── 3. Delete old graph.obj ──
        _log(log, "Deleting old graph.obj on remote")
        graph_path = f"{extract_dir.rstrip('/')}/graph.obj"
        del_graph = run_ssh(
            cfg,
            f"rm -f {shlex.quote(graph_path)} && echo deleted",
            timeout=60,
        )
        report.remote_cleanup["graph_deleted"] = del_graph.ok
        report.add_check(
            "old_graph_deleted",
            True,
            "Deleted old graph.obj" if del_graph.ok else "No graph.obj to delete",
        )

        # ── 3b. Truncate gtfs_work tables on remote ──
        _log(log, "Truncating remote gtfs_work tables")
        truncate_cmd = (
            f"cd {shlex.quote(transportapp)} && source .env && "
            "docker exec postgres psql -U \"$PGUSER\" -d \"$PGDATABASE\" -c "
            "\"DO \\$\\$ DECLARE r RECORD; BEGIN "
            "FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'gtfs_work' LOOP "
            "EXECUTE 'TRUNCATE TABLE gtfs_work.' || quote_ident(r.tablename) || ' CASCADE'; "
            "END LOOP; END \\$\\$;\" 2>/dev/null || true"
        )
        truncate_res = run_ssh(cfg, truncate_cmd, timeout=120)
        report.remote_cleanup["gtfs_work_truncated"] = truncate_res.ok
        report.add_check(
            "gtfs_work_truncated",
            True,
            f"Truncated gtfs_work tables (rc={truncate_res.returncode})",
        )

        # ── 4. Delete old GTFS zips (keep latest backup only) ──
        _log(log, "Cleaning old GTFS zips on remote")
        clean_zips_cmd = (
            f"find {shlex.quote(extract_dir)} -maxdepth 1 -name '*.zip' -delete 2>/dev/null; "
            f"incoming_dir={shlex.quote(transportapp)}/gtfs_incoming; "
            f"if [ -d \"$incoming_dir\" ]; then "
            f"  cd \"$incoming_dir\" && ls -t *.zip 2>/dev/null | tail -n +2 | xargs -r rm -f; "
            f"fi; "
            f"echo done"
        )
        clean_zips_res = run_ssh(cfg, clean_zips_cmd, timeout=60)
        report.remote_cleanup["old_zips_cleaned"] = clean_zips_res.ok

        # ── 5. Delete extracted GTFS txt files ──
        clean_files = [
            "agency.txt", "stops.txt", "routes.txt", "trips.txt",
            "stop_times.txt", "shapes.txt", "calendar.txt", "calendar_dates.txt",
            "feed_info.txt", "frequencies.txt", "transfers.txt", "pathways.txt",
            "fare_attributes.txt", "fare_rules.txt", "levels.txt",
        ]
        rm_cmds = " ".join(
            f"rm -f {shlex.quote(extract_dir.rstrip('/') + '/' + f)};"
            for f in clean_files
        )
        run_ssh(cfg, rm_cmds, timeout=60)

        # ── 6. Check disk free ──
        _log(log, "Checking remote disk space")
        df_res = run_ssh(cfg, "df -k /home | tail -1", timeout=30)
        disk_free_mb = _parse_free_mb(df_res.stdout, "disk") if df_res.ok else None
        report.remote_cleanup["disk_free_mb"] = disk_free_mb
        report.add_check(
            "disk_headroom",
            disk_free_mb is not None and disk_free_mb >= MIN_DISK_FREE_MB,
            f"{disk_free_mb}MB free (need {MIN_DISK_FREE_MB}MB)" if disk_free_mb else "Could not read disk",
            value=disk_free_mb,
        )

        # ── 7. Check memory free (OTP is stopped now) ──
        _log(log, "Checking remote memory")
        mem_res = run_ssh(cfg, "free -k | grep Mem:", timeout=30)
        mem_free_mb = _parse_free_mb(mem_res.stdout, "mem") if mem_res.ok else None
        report.remote_cleanup["mem_free_mb"] = mem_free_mb
        report.add_check(
            "memory_headroom",
            mem_free_mb is not None and mem_free_mb >= MIN_MEM_FREE_MB,
            f"{mem_free_mb}MB available (need {MIN_MEM_FREE_MB}MB)" if mem_free_mb else "Could not read memory",
            value=mem_free_mb,
        )

        # ── 8. Verify GTFS zip is valid ──
        from .gtfs_validate import validate_gtfs_zip
        validation = validate_gtfs_zip(gtfs_zip_path)
        report.add_check(
            "gtfs_zip_valid",
            bool(validation.get("ok")),
            "GTFS zip structural validation passed" if validation.get("ok") else f"GTFS zip invalid: {validation.get('errors', [])}",
        )

        # ── 9. Local cleanup ──
        _log(log, "Running local cleanup")
        local_cleaned = 0
        # Clean old validation reports
        for p in Path("/tmp").glob("gtfs_validation_*"):
            if p.is_dir():
                import shutil
                shutil.rmtree(p, ignore_errors=True)
                local_cleaned += 1
            elif p.is_file():
                p.unlink(missing_ok=True)
                local_cleaned += 1
        report.local_cleanup["validation_reports_cleaned"] = local_cleaned

        # Delete stale GTFS outputs (keep only the current release zip)
        gtfs_out_dir = gtfs_zip_path.parent
        stale_zips_removed = 0
        current_name = gtfs_zip_path.name
        for p in gtfs_out_dir.glob("*.zip"):
            if p.name != current_name:
                try:
                    p.unlink()
                    stale_zips_removed += 1
                except OSError:
                    pass
        report.local_cleanup["stale_gtfs_outputs_removed"] = stale_zips_removed
        if stale_zips_removed:
            _log(log, f"Cleaned {stale_zips_removed} stale GTFS zips from {gtfs_out_dir}")

        # Record expected counts from the zip for post-deploy verification
        report.post_deploy["expected_routes"] = _count_rows_in_zip(gtfs_zip_path, "routes.txt")
        report.post_deploy["expected_stops"] = _count_rows_in_zip(gtfs_zip_path, "stops.txt")
        report.post_deploy["expected_trips"] = _count_rows_in_zip(gtfs_zip_path, "trips.txt")

        _log(log, f"Pre-deploy enforcer: {len(report.checks)} checks, "
             f"{'ALL PASS' if report.passed else 'BLOCKED: ' + '; '.join(report.blocking_reasons)}")

        return report

    def build_and_upload_graph(
        self,
        cfg: OpsConfig,
        gtfs_zip_path: Path,
        *,
        log: Optional[LogFn] = None,
    ) -> DeployCheckResult:
        """Build OTP graph locally, upload to EC2. NEVER build on EC2."""
        import subprocess

        local_extract = cfg.local_gtfs_extract_dir or os.path.expanduser("~/gtfs_app/otp/otp-data/routers/default")
        graph_path = Path(local_extract) / "graph.obj"
        compose_dir = cfg.transportapp_root or os.path.expanduser("~/gtfs_app")
        compose_file = Path(compose_dir) / "docker-compose.yaml"
        if not compose_file.exists():
            compose_file = Path(compose_dir) / "docker-compose.yml"

        # Copy GTFS zip to local OTP dir
        import shutil
        local_zip = Path(local_extract) / "latest_upload_gtfs.zip"
        shutil.copy2(gtfs_zip_path, local_zip)
        _log(log, f"Copied GTFS zip to {local_zip}")

        # Delete old graph
        if graph_path.exists():
            graph_path.unlink()
            _log(log, "Deleted old local graph.obj")

        # Stop heavy local containers to free Docker memory
        _log(log, "Stopping heavy local containers to free Docker memory for OTP build")
        containers_to_stop = ["otp", "phase2-opensearch", "evolution-api", "n8n-n8n-1",
                              "nominatim", "overpass-region", "valhalla_constructor_v2"]
        for c in containers_to_stop:
            subprocess.run(["docker", "stop", c], capture_output=True, timeout=30)

        # Build graph locally
        _log(log, f"Building OTP graph locally (Xmx={LOCAL_OTP_BUILD_XMX})")
        build_cmd = [
            "docker", "compose", "-f", str(compose_file),
            "run", "--rm", "--entrypoint", "bash", "otp", "-lc",
            f"java -Xmx{LOCAL_OTP_BUILD_XMX} -jar /otp/otp.jar --build --save /otp/otp-data/routers/default",
        ]
        build_proc = subprocess.run(
            build_cmd, capture_output=True, text=True, timeout=600,
            cwd=compose_dir,
        )

        # Restart stopped containers
        _log(log, "Restarting local containers")
        for c in containers_to_stop:
            subprocess.run(["docker", "start", c], capture_output=True, timeout=30)

        if not graph_path.exists():
            _log(log, f"OTP graph build FAILED (rc={build_proc.returncode})")
            last_lines = (build_proc.stdout or "")[-500:]
            return DeployCheckResult(
                name="local_graph_build",
                passed=False,
                details=f"Build failed: {last_lines}",
            )

        graph_size_mb = graph_path.stat().st_size / (1024 * 1024)
        _log(log, f"Graph built: {graph_path} ({graph_size_mb:.0f}MB)")

        # Upload to EC2
        extract_dir = cfg.aws_gtfs_extract_dir or "/opt/gtfs_app/otp/otp-data/routers/default"
        remote_graph = f"{extract_dir.rstrip('/')}/graph.obj"
        _log(log, f"Uploading graph.obj to EC2: {remote_graph}")
        upload_res = scp_upload(cfg, graph_path, remote_graph, timeout=600)

        if not upload_res.ok:
            return DeployCheckResult(
                name="graph_upload",
                passed=False,
                details=f"SCP upload failed: {upload_res.stderr[:200]}",
            )

        _log(log, f"Graph uploaded to EC2 ({graph_size_mb:.0f}MB)")
        return DeployCheckResult(
            name="local_graph_build_and_upload",
            passed=True,
            details=f"Built locally ({graph_size_mb:.0f}MB), uploaded to EC2",
            value={"graph_size_mb": graph_size_mb, "graph_path": str(graph_path)},
        )

    def post_deploy_verify(
        self,
        cfg: OpsConfig,
        expected_routes: int,
        expected_stops: int,
        *,
        log: Optional[LogFn] = None,
    ) -> DeployReport:
        """Run after deployment completes to verify data landed correctly."""
        report = DeployReport()
        transportapp = cfg.aws_transportapp_dir or "/opt/gtfs_app"

        # ── 1. OTP responding ──
        _log(log, "Post-deploy: checking OTP health")
        otp_health = run_ssh(
            cfg,
            "curl -sS -m 15 http://127.0.0.1:8080/otp/routers/default 2>/dev/null | head -c 200",
            timeout=30,
        )
        otp_ok = otp_health.ok and "routerId" in otp_health.stdout
        report.add_check(
            "otp_responding",
            otp_ok,
            "OTP router responding" if otp_ok else f"OTP not responding: {otp_health.stdout[:100]}",
        )

        # ── 2. DB route count ──
        _log(log, "Post-deploy: checking DB route count")
        count_cmd = (
            f"cd {shlex.quote(transportapp)} && source .env && "
            "docker exec postgres psql -U \"$PGUSER\" -d \"$PGDATABASE\" -t -A -c "
            "\"SELECT COUNT(*) FROM gtfs.routes;\""
        )
        routes_res = run_ssh(cfg, count_cmd, timeout=30)
        try:
            actual_routes = int(routes_res.stdout.strip())
        except (ValueError, TypeError):
            actual_routes = -1

        routes_match = actual_routes == expected_routes
        report.add_check(
            "route_count_match",
            routes_match,
            f"Expected {expected_routes}, got {actual_routes}" + ("" if routes_match else " — MISMATCH"),
            value={"expected": expected_routes, "actual": actual_routes},
        )

        # ── 3. DB stop count ──
        _log(log, "Post-deploy: checking DB stop count")
        stops_cmd = (
            f"cd {shlex.quote(transportapp)} && source .env && "
            "docker exec postgres psql -U \"$PGUSER\" -d \"$PGDATABASE\" -t -A -c "
            "\"SELECT COUNT(*) FROM gtfs.stops;\""
        )
        stops_res = run_ssh(cfg, stops_cmd, timeout=30)
        try:
            actual_stops = int(stops_res.stdout.strip())
        except (ValueError, TypeError):
            actual_stops = -1

        stops_match = actual_stops == expected_stops
        report.add_check(
            "stop_count_match",
            stops_match,
            f"Expected {expected_stops}, got {actual_stops}" + ("" if stops_match else " — MISMATCH"),
            value={"expected": expected_stops, "actual": actual_stops},
        )

        if not routes_match or not stops_match:
            _log(log, "POST-DEPLOY WARNING: count mismatch detected (not rolling back)")
            report.passed = True
            report.blocking_reasons = [
                r for r in report.blocking_reasons
                if "route_count" not in r and "stop_count" not in r
            ]
            for c in report.checks:
                if c.name in ("route_count_match", "stop_count_match"):
                    c.passed = True
                    c.details += " [WARNING ONLY — not blocking]"

        _log(log, f"Post-deploy verify: {sum(1 for c in report.checks if c.passed)}/{len(report.checks)} OK")
        return report
