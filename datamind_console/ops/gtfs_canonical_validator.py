"""MobilityData GTFS validator, run as an explicit operator action.

DataMind's own checks are a structure check of the ZIP
(``ops/gtfs_validate.py``) and a database quality check
(``phase5_gtfs/validator``). Neither is the canonical GTFS validator. The
canonical one (``tools/gtfs-validator.jar``, MobilityData 7.x) was present
but never called. This module runs it on demand. It never runs
automatically and never gates publishing by itself; the UI reports its
result as a separate validation layer.

Configuration (all optional):
  GTFS_VALIDATOR_JAR            path to the jar (default: tools/gtfs-validator.jar)
  GTFS_VALIDATOR_COUNTRY_CODE   ISO 3166-1 alpha-2 code for country rules, e.g. "ec"
  GTFS_VALIDATOR_TIMEOUT_S      default 600
  JAVA_HOME                     used when `java` is not on PATH
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from datamind_console.common.env import PROJECT_ROOT

DEFAULT_JAR = PROJECT_ROOT / "tools" / "gtfs-validator.jar"
SEVERITIES = ("ERROR", "WARNING", "INFO")


@dataclass
class ValidatorAvailability:
    jar: Optional[Path]
    java: Optional[str]

    @property
    def ready(self) -> bool:
        return bool(self.jar and self.java)

    @property
    def missing(self) -> list[str]:
        out = []
        if not self.jar:
            out.append("jar")
        if not self.java:
            out.append("java")
        return out


@dataclass
class ValidatorResult:
    ok: bool
    status: str  # "pass" | "fail" | "error"
    counts: dict[str, int] = field(default_factory=dict)
    notices: list[dict[str, Any]] = field(default_factory=list)
    output_dir: Optional[Path] = None
    html_report: Optional[Path] = None
    validator_version: Optional[str] = None
    stderr_tail: str = ""
    returncode: Optional[int] = None


def resolve_jar() -> Optional[Path]:
    configured = os.getenv("GTFS_VALIDATOR_JAR", "").strip()
    candidate = Path(configured).expanduser() if configured else DEFAULT_JAR
    return candidate if candidate.is_file() else None


def resolve_java() -> Optional[str]:
    found = shutil.which("java")
    if found:
        return found
    java_home = os.getenv("JAVA_HOME", "").strip()
    if java_home and (Path(java_home) / "bin" / "java").is_file():
        return str(Path(java_home) / "bin" / "java")
    return None


def availability() -> ValidatorAvailability:
    return ValidatorAvailability(jar=resolve_jar(), java=resolve_java())


def build_command(java: str, jar: Path, zip_path: Path, output_dir: Path, *, country_code: Optional[str] = None) -> list[str]:
    cmd = [java, "-jar", str(jar), "-i", str(zip_path), "-o", str(output_dir), "-svu", "-p"]
    code = (country_code or os.getenv("GTFS_VALIDATOR_COUNTRY_CODE", "")).strip().lower()
    if len(code) == 2 and code.isalpha():
        cmd += ["-c", code]
    return cmd


def parse_report(report: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]], Optional[str]]:
    """Counts by severity and the notice list (largest first) from report.json."""
    if not isinstance(report, dict) or not isinstance(report.get("notices"), list):
        raise ValueError("report must contain a notices array")
    summary = report.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("validatorVersion"), str) or not summary["validatorVersion"]:
        raise ValueError("report summary must identify validatorVersion")
    counts = {sev: 0 for sev in SEVERITIES}
    notices: list[dict[str, Any]] = []
    for notice in report["notices"]:
        if not isinstance(notice, dict):
            raise ValueError("notice must be an object")
        severity = notice.get("severity")
        total = notice.get("totalNotices")
        if (severity not in SEVERITIES or not isinstance(notice.get("code"), str)
                or not notice["code"] or type(total) is not int or total < 1):
            raise ValueError("notice requires code, known severity and positive integer totalNotices")
        counts[severity] += total
        notices.append({"code": notice["code"], "severity": severity, "total": total})
    notices.sort(key=lambda n: (SEVERITIES.index(n["severity"]), -n["total"]))
    return counts, notices, summary.get("validatorVersion")


def _system_errors(out_dir: Path) -> list[dict[str, Any]]:
    path = out_dir / "system_errors.json"
    if not path.is_file():
        raise ValueError("missing system_errors.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("notices"), list):
        raise ValueError("system_errors report must contain a notices array")
    errors = []
    for notice in data["notices"]:
        if not isinstance(notice, dict) or not isinstance(notice.get("code"), str):
            raise ValueError("invalid system error notice")
        total = notice.get("totalNotices", 1)
        if type(total) is not int or total < 1:
            raise ValueError("invalid system error totalNotices")
        errors.append({"code": notice["code"], "severity": "SYSTEM", "total": total})
    return errors


def default_output_dir(zip_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    base = Path(tempfile.gettempdir()) / "datamind_gtfs_validator"
    return base / f"{zip_path.stem}_{stamp}"


def run_validator(
    zip_path: Path,
    *,
    output_dir: Optional[Path] = None,
    timeout_s: Optional[int] = None,
    country_code: Optional[str] = None,
    runner=subprocess.run,
) -> ValidatorResult:
    """Run the MobilityData validator on *zip_path* (read-only on the input)."""
    avail = availability()
    if not avail.ready:
        return ValidatorResult(ok=False, status="error", stderr_tail=f"validator unavailable: missing {', '.join(avail.missing)}")
    if not Path(zip_path).exists():
        return ValidatorResult(ok=False, status="error", stderr_tail=f"input not found: {zip_path}")
    out_dir = output_dir or default_output_dir(zip_path)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if any((out_dir / name).exists() for name in ("report.json", "system_errors.json", "report.html")):
            raise ValueError("output directory already contains a report; use a fresh directory")
        timeout = int(timeout_s or os.getenv("GTFS_VALIDATOR_TIMEOUT_S", "600") or 600)
        if timeout < 1:
            raise ValueError("timeout must be positive")
    except (OSError, ValueError) as exc:
        return ValidatorResult(ok=False, status="error", output_dir=out_dir, stderr_tail=str(exc))
    cmd = build_command(avail.java, avail.jar, zip_path, out_dir, country_code=country_code)
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return ValidatorResult(ok=False, status="error", output_dir=out_dir, stderr_tail=f"timed out after {timeout}s")
    except OSError as exc:
        return ValidatorResult(ok=False, status="error", output_dir=out_dir, stderr_tail=str(exc))
    report_path = out_dir / "report.json"
    if not report_path.is_file():
        return ValidatorResult(
            ok=False, status="error", output_dir=out_dir, returncode=proc.returncode,
            stderr_tail=(proc.stderr or proc.stdout or "missing report.json")[-2000:],
        )
    try:
        counts, notices, version = parse_report(json.loads(report_path.read_text(encoding="utf-8")))
        system_errors = _system_errors(out_dir)
    except (OSError, ValueError, TypeError) as exc:
        return ValidatorResult(ok=False, status="error", output_dir=out_dir,
                               returncode=proc.returncode, stderr_tail=f"invalid validator report: {exc}")
    html = out_dir / "report.html"
    if proc.returncode != 0 or system_errors:
        # The validator could not read or fully process the feed: never
        # report "pass" for a run that did not validate (it writes an empty
        # report.json in that case).
        return ValidatorResult(
            ok=False, status="error", counts=counts, notices=notices + system_errors,
            output_dir=out_dir, html_report=html if html.is_file() else None,
            validator_version=version, stderr_tail=(proc.stderr or "")[-2000:], returncode=proc.returncode,
        )
    passed = counts.get("ERROR", 0) == 0
    return ValidatorResult(
        ok=passed,
        status="pass" if passed else "fail",
        counts=counts,
        notices=notices,
        output_dir=out_dir,
        html_report=html if html.is_file() else None,
        validator_version=version,
        stderr_tail=(proc.stderr or "")[-2000:],
        returncode=proc.returncode,
    )
