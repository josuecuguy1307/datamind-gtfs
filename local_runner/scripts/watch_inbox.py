from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from .runner_core import RunnerError, infer_target_from_filename, load_runner_config, run_prompt_file
except Exception:  # pragma: no cover
    from runner_core import RunnerError, infer_target_from_filename, load_runner_config, run_prompt_file  # type: ignore


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "runner_config.yaml"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n")


def _safe_move(source: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = target_dir / source.name
    if candidate.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        candidate = target_dir / f"{source.stem}_{stamp}{source.suffix}"
    os.replace(source, candidate)
    return candidate


def _is_eligible_filename(name: str, ignored_prefixes: tuple[str, ...]) -> bool:
    n = str(name or "")
    if not n:
        return False
    lower = n.lower()
    if lower.endswith(".lock"):
        return False
    if not (lower.endswith(".txt") or lower.endswith(".md")):
        return False
    for prefix in ignored_prefixes:
        if n.startswith(prefix):
            return False
    return True


def _resolve_target(
    *,
    file_name: str,
    explicit_target: Optional[str],
    auto_infer_target: bool,
    default_target: Optional[str],
) -> Optional[str]:
    if explicit_target:
        return str(explicit_target).strip().lower()
    if auto_infer_target:
        inferred = infer_target_from_filename(file_name)
        if inferred:
            return inferred
    if default_target:
        return str(default_target).strip().lower()
    return None


def _collect_candidates(watch_dir: Path, ignored_prefixes: tuple[str, ...]) -> List[Path]:
    paths: List[Path] = []
    for item in sorted(watch_dir.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_file():
            continue
        if not _is_eligible_filename(item.name, ignored_prefixes):
            continue
        paths.append(item)
    return paths


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch local_runner inbox and dispatch prompt files automatically (opt-in)."
    )
    parser.add_argument(
        "--config",
        required=False,
        default=str(_default_config_path()),
        help="Runner config path. Default: local_runner/config/runner_config.yaml",
    )
    parser.add_argument(
        "--target",
        required=False,
        help="Force target for all files (codex|claude). If omitted uses watcher auto-infer/default config.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="For tests/debug: stop after N cycles (0 means run forever).",
    )
    args = parser.parse_args(argv)

    cfg_path = Path(args.config).expanduser().resolve()
    try:
        cfg = load_runner_config(cfg_path)
    except Exception as e:
        print(f"[watcher] ERROR loading config: {e}", file=sys.stderr)
        return 2

    watcher_cfg = cfg.watcher
    if not watcher_cfg.enabled:
        print("[watcher] watcher.enabled=false in config; refusing to start.", file=sys.stderr)
        return 2

    watch_dir = watcher_cfg.watch_dir.resolve()
    if not watch_dir.exists():
        print(f"[watcher] ERROR watch_dir does not exist: {watch_dir}", file=sys.stderr)
        return 2
    if not _within(watch_dir, cfg.paths.inbox):
        print(
            f"[watcher] ERROR watch_dir must be inside inbox for safety. watch_dir={watch_dir} inbox={cfg.paths.inbox}",
            file=sys.stderr,
        )
        return 2

    lock_path = watch_dir / ".watch_inbox.lock"
    if lock_path.exists():
        print(f"[watcher] ERROR lock already present: {lock_path}", file=sys.stderr)
        return 2

    stop_requested = {"value": False}

    def _request_stop(sig_num: int, _frame: Any) -> None:
        stop_requested["value"] = True
        print(f"[watcher] signal={sig_num} received; will stop after current dispatch.", file=sys.stderr)

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    lock_payload = {
        "started_at": _utc_now_iso(),
        "pid": os.getpid(),
        "watch_dir": str(watch_dir),
        "config_path": str(cfg_path),
    }
    lock_path.write_text(json.dumps(lock_payload, ensure_ascii=True, indent=2), encoding="utf-8")

    cycle_count = 0
    try:
        while True:
            cycle_count += 1
            watch_cycle_id = f"watch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
            candidates = _collect_candidates(watch_dir, watcher_cfg.ignored_prefixes)

            files_detected = len(candidates)
            files_dispatched = 0
            per_target_counts: Dict[str, int] = {}

            for prompt_path in candidates:
                if stop_requested["value"]:
                    break

                target = _resolve_target(
                    file_name=prompt_path.name,
                    explicit_target=(str(args.target).strip().lower() if args.target else None),
                    auto_infer_target=watcher_cfg.auto_infer_target,
                    default_target=watcher_cfg.default_target,
                )
                if not target:
                    failed_path = _safe_move(prompt_path, cfg.paths.failed)
                    row = {
                        "timestamp": _utc_now_iso(),
                        "prompt_source": "watcher",
                        "prompt_file": str(prompt_path),
                        "prompt_file_final": str(failed_path),
                        "prompt_char_count": 0,
                        "target": "<none>",
                        "command_used": "<not_dispatched>",
                        "status": "failed",
                        "duration_ms": 0,
                        "output_file": "",
                        "stderr_file": None,
                        "error_summary": "Target could not be inferred and no default target configured.",
                        "return_code": None,
                        "watch_cycle_id": watch_cycle_id,
                        "files_detected": files_detected,
                        "files_dispatched": files_dispatched,
                    }
                    _append_jsonl(cfg.paths.logs / cfg.log_filename, row)
                    continue

                allowed = int(per_target_counts.get(target, 0))
                if allowed >= int(watcher_cfg.max_concurrent_per_target):
                    continue

                if target not in cfg.targets:
                    failed_path = _safe_move(prompt_path, cfg.paths.failed)
                    row = {
                        "timestamp": _utc_now_iso(),
                        "prompt_source": "watcher",
                        "prompt_file": str(prompt_path),
                        "prompt_file_final": str(failed_path),
                        "prompt_char_count": 0,
                        "target": target,
                        "command_used": "<not_dispatched>",
                        "status": "failed",
                        "duration_ms": 0,
                        "output_file": "",
                        "stderr_file": None,
                        "error_summary": f"Unknown target `{target}`.",
                        "return_code": None,
                        "watch_cycle_id": watch_cycle_id,
                        "files_detected": files_detected,
                        "files_dispatched": files_dispatched,
                    }
                    _append_jsonl(cfg.paths.logs / cfg.log_filename, row)
                    continue

                try:
                    result = run_prompt_file(
                        file_path=prompt_path,
                        target=target,
                        config_path=cfg_path,
                    )
                    files_dispatched += 1
                    per_target_counts[target] = int(per_target_counts.get(target, 0)) + 1
                    row = {
                        "timestamp": _utc_now_iso(),
                        "prompt_source": "watcher",
                        "prompt_file": result.prompt_file,
                        "prompt_file_final": result.prompt_file_final,
                        "prompt_char_count": result.prompt_char_count,
                        "target": result.target,
                        "command_used": result.command_used,
                        "status": result.status,
                        "duration_ms": result.duration_ms,
                        "output_file": result.output_file,
                        "stderr_file": result.stderr_file,
                        "error_summary": result.error_summary,
                        "return_code": result.return_code,
                        "watch_cycle_id": watch_cycle_id,
                        "files_detected": files_detected,
                        "files_dispatched": files_dispatched,
                    }
                    _append_jsonl(cfg.paths.logs / cfg.log_filename, row)
                except RunnerError as e:
                    # run_prompt_file may fail before moving file; move to failed if still in inbox.
                    final_path = prompt_path
                    if prompt_path.exists() and _within(prompt_path, cfg.paths.inbox):
                        final_path = _safe_move(prompt_path, cfg.paths.failed)
                    row = {
                        "timestamp": _utc_now_iso(),
                        "prompt_source": "watcher",
                        "prompt_file": str(prompt_path),
                        "prompt_file_final": str(final_path),
                        "prompt_char_count": 0,
                        "target": target,
                        "command_used": "<runner_error>",
                        "status": "failed",
                        "duration_ms": 0,
                        "output_file": "",
                        "stderr_file": None,
                        "error_summary": str(e),
                        "return_code": None,
                        "watch_cycle_id": watch_cycle_id,
                        "files_detected": files_detected,
                        "files_dispatched": files_dispatched,
                    }
                    _append_jsonl(cfg.paths.logs / cfg.log_filename, row)

            if stop_requested["value"]:
                break
            if int(args.max_cycles) > 0 and cycle_count >= int(args.max_cycles):
                break
            time.sleep(float(watcher_cfg.poll_interval_seconds))
    finally:
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception:
            pass

    print(f"[watcher] stopped after cycles={cycle_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
