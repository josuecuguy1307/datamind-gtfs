from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional


TARGET_PREFIXES = {
    "codex_": "codex",
    "claude_": "claude",
}


class RunnerError(Exception):
    """Raised for predictable runner failures with safe messages."""


@dataclass(frozen=True)
class TargetConfig:
    command: list[str]
    input_mode: str


@dataclass(frozen=True)
class RunnerPaths:
    inbox: Path
    processing: Path
    done: Path
    failed: Path
    results: Path
    logs: Path


@dataclass(frozen=True)
class ClipboardConfig:
    enabled: bool
    prompt_filename_prefix: str
    max_chars: int
    persist_clipboard_prompt: bool
    require_target_explicit_for_clipboard: bool
    persist_destination: str


@dataclass(frozen=True)
class WatcherConfig:
    enabled: bool
    poll_interval_seconds: int
    max_concurrent_per_target: int
    watch_dir: Path
    auto_infer_target: bool
    default_target: Optional[str]
    ignored_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class RunnerConfig:
    timeout_seconds: int
    capture_stderr_separately: bool
    infer_target_from_filename: bool
    default_target: Optional[str]
    log_filename: str
    working_directory: Path
    paths: RunnerPaths
    targets: Dict[str, TargetConfig]
    clipboard: ClipboardConfig
    watcher: WatcherConfig


@dataclass(frozen=True)
class RunResult:
    timestamp: str
    prompt_source: str
    prompt_file: str
    prompt_file_final: str
    prompt_char_count: int
    target: str
    command_used: str
    status: str
    duration_ms: int
    output_file: str
    stderr_file: Optional[str]
    error_summary: Optional[str]
    return_code: Optional[int]
    log_file: str


def load_runner_config(config_path: Path) -> RunnerConfig:
    cfg_path = Path(config_path).expanduser().resolve()
    if not cfg_path.exists():
        raise RunnerError(f"Config file not found: {cfg_path}")

    raw = _load_yaml_or_json(cfg_path)
    if not isinstance(raw, dict):
        raise RunnerError("Runner config must be an object/map.")

    runner = dict(raw.get("runner") or {})
    timeout_seconds = int(runner.get("timeout_seconds", 1200))
    capture_stderr_separately = bool(runner.get("capture_stderr_separately", True))
    infer_target_from_filename = bool(runner.get("infer_target_from_filename", True))
    default_target = str(runner.get("default_target") or "").strip() or None
    log_filename = str(runner.get("log_filename") or "runs.jsonl").strip() or "runs.jsonl"

    repo_root = cfg_path.parents[2] if len(cfg_path.parents) >= 3 else Path.cwd().resolve()
    working_directory = _resolve_path(repo_root, str(runner.get("working_directory") or "."))
    if not working_directory.is_dir():
        raise RunnerError(f"runner.working_directory is not a directory: {working_directory}")
    p = dict(raw.get("paths") or {})
    paths = RunnerPaths(
        inbox=_resolve_path(repo_root, str(p.get("inbox") or "local_runner/inbox")),
        processing=_resolve_path(repo_root, str(p.get("processing") or "local_runner/processing")),
        done=_resolve_path(repo_root, str(p.get("done") or "local_runner/done")),
        failed=_resolve_path(repo_root, str(p.get("failed") or "local_runner/failed")),
        results=_resolve_path(repo_root, str(p.get("results") or "local_runner/results")),
        logs=_resolve_path(repo_root, str(p.get("logs") or "local_runner/logs")),
    )

    targets_raw = dict(raw.get("targets") or {})
    targets: Dict[str, TargetConfig] = {}
    for target_name, target_value in targets_raw.items():
        item = dict(target_value or {})
        command = item.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
            raise RunnerError(f"Target `{target_name}` must define `command` as a non-empty string list.")

        input_mode = str(item.get("input_mode") or "stdin").strip().lower()
        if input_mode not in {"stdin", "temp_file", "arg", "interactive_not_supported"}:
            raise RunnerError(
                f"Target `{target_name}` has unsupported input_mode `{input_mode}`. "
                "Use one of: stdin, temp_file, arg, interactive_not_supported."
            )

        targets[str(target_name).strip().lower()] = TargetConfig(
            command=[str(x) for x in command],
            input_mode=input_mode,
        )

    if not targets:
        raise RunnerError("Runner config must define at least one target in `targets`.")

    if default_target and default_target.lower() not in targets:
        raise RunnerError(f"default_target `{default_target}` is not configured in targets.")

    clipboard_raw = dict(raw.get("clipboard") or {})
    clipboard = ClipboardConfig(
        enabled=bool(clipboard_raw.get("clipboard_enabled", True)),
        prompt_filename_prefix=str(clipboard_raw.get("clipboard_prompt_filename_prefix") or "clipboard_prompt").strip()
        or "clipboard_prompt",
        max_chars=max(1, int(clipboard_raw.get("clipboard_max_chars", 200000))),
        persist_clipboard_prompt=bool(clipboard_raw.get("persist_clipboard_prompt", True)),
        require_target_explicit_for_clipboard=bool(
            clipboard_raw.get("require_target_explicit_for_clipboard", False)
        ),
        persist_destination=str(clipboard_raw.get("clipboard_persist_destination") or "inbox").strip().lower(),
    )

    if clipboard.persist_destination not in {"inbox", "processing"}:
        raise RunnerError("clipboard_persist_destination must be either `inbox` or `processing`.")

    watcher_raw = dict(raw.get("watcher") or {})
    watcher_default_target = str(watcher_raw.get("default_target") or "").strip().lower() or None
    if watcher_default_target and watcher_default_target not in targets:
        raise RunnerError(f"watcher.default_target `{watcher_default_target}` is not configured in targets.")

    watch_dir_raw = str(watcher_raw.get("watch_dir") or "local_runner/inbox")
    watcher = WatcherConfig(
        enabled=bool(watcher_raw.get("enabled", False)),
        poll_interval_seconds=max(1, int(watcher_raw.get("poll_interval_seconds", 5))),
        max_concurrent_per_target=max(1, int(watcher_raw.get("max_concurrent_per_target", 1))),
        watch_dir=_resolve_path(repo_root, watch_dir_raw),
        auto_infer_target=bool(watcher_raw.get("auto_infer_target", True)),
        default_target=watcher_default_target or default_target,
        ignored_prefixes=tuple(
            str(x).strip() for x in list(watcher_raw.get("ignored_prefixes") or ["_", "."]) if str(x).strip()
        ),
    )

    return RunnerConfig(
        timeout_seconds=max(1, timeout_seconds),
        capture_stderr_separately=capture_stderr_separately,
        infer_target_from_filename=infer_target_from_filename,
        default_target=(default_target.lower() if default_target else None),
        log_filename=log_filename,
        working_directory=working_directory,
        paths=paths,
        targets=targets,
        clipboard=clipboard,
        watcher=watcher,
    )


def run_prompt_file(
    *,
    file_path: Path,
    target: Optional[str],
    config_path: Path,
    cancel_event: Any | None = None,
) -> RunResult:
    cfg = load_runner_config(config_path)
    _ensure_runner_dirs(cfg.paths)

    source_prompt = Path(file_path).expanduser().resolve()
    if not source_prompt.exists():
        raise RunnerError(f"Prompt file not found: {source_prompt}")
    if not source_prompt.is_file():
        raise RunnerError(f"Prompt path is not a file: {source_prompt}")
    if not _is_within(source_prompt, cfg.paths.inbox):
        raise RunnerError(
            f"Prompt file must be inside inbox for safe lifecycle handling: {cfg.paths.inbox}"
        )

    effective_target = _resolve_target_file(
        source_prompt=source_prompt,
        explicit_target=target,
        cfg=cfg,
    )
    if effective_target not in cfg.targets:
        raise RunnerError(
            f"Unknown target `{effective_target}`. Available: {', '.join(sorted(cfg.targets.keys()))}"
        )

    processing_prompt = _move_to_dir_unique(source_prompt, cfg.paths.processing)
    prompt_text = processing_prompt.read_text(encoding="utf-8")

    return _dispatch_prompt(
        cfg=cfg,
        target=effective_target,
        prompt_text=prompt_text,
        prompt_source="file",
        prompt_file_original=source_prompt,
        prompt_file_processing=processing_prompt,
        prompt_char_count=len(prompt_text),
        base_hint=source_prompt.stem,
        clipboard_char_count=None,
        cancel_event=cancel_event,
    )


def run_clipboard_prompt(
    *,
    target: Optional[str],
    config_path: Path,
    persist_prompt: Optional[bool] = None,
    clipboard_getter: Optional[Callable[[], str]] = None,
    cancel_event: Any | None = None,
) -> RunResult:
    cfg = load_runner_config(config_path)
    _ensure_runner_dirs(cfg.paths)

    if not cfg.clipboard.enabled:
        raise RunnerError("Clipboard mode is disabled in config (`clipboard_enabled=false`).")

    effective_target = _resolve_target_clipboard(explicit_target=target, cfg=cfg)
    if effective_target not in cfg.targets:
        raise RunnerError(
            f"Unknown target `{effective_target}`. Available: {', '.join(sorted(cfg.targets.keys()))}"
        )

    text = _read_clipboard_text(clipboard_getter=clipboard_getter)
    if not str(text or "").strip():
        raise RunnerError("Clipboard is empty. Copy a prompt and run again.")

    char_count = len(text)
    if char_count > cfg.clipboard.max_chars:
        raise RunnerError(
            f"Clipboard content exceeds configured clipboard_max_chars={cfg.clipboard.max_chars}. "
            f"Current size: {char_count} chars."
        )

    should_persist = cfg.clipboard.persist_clipboard_prompt if persist_prompt is None else bool(persist_prompt)

    prompt_file_original: Optional[Path] = None
    prompt_file_processing: Optional[Path] = None

    if should_persist:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        prefix = _safe_stem(cfg.clipboard.prompt_filename_prefix)
        filename = f"{stamp}_{effective_target}_{prefix}.md"

        if cfg.clipboard.persist_destination == "processing":
            prompt_file_processing = cfg.paths.processing / filename
            prompt_file_processing.write_text(text, encoding="utf-8")
            prompt_file_original = prompt_file_processing
        else:
            prompt_file_original = cfg.paths.inbox / filename
            prompt_file_original.write_text(text, encoding="utf-8")
            prompt_file_processing = _move_to_dir_unique(prompt_file_original, cfg.paths.processing)

    return _dispatch_prompt(
        cfg=cfg,
        target=effective_target,
        prompt_text=text,
        prompt_source="clipboard",
        prompt_file_original=prompt_file_original,
        prompt_file_processing=prompt_file_processing,
        prompt_char_count=char_count,
        base_hint=(prompt_file_original.stem if prompt_file_original else cfg.clipboard.prompt_filename_prefix),
        clipboard_char_count=char_count,
        cancel_event=cancel_event,
    )


def infer_target_from_filename(filename: str) -> Optional[str]:
    name = str(filename or "").strip().lower()
    for prefix, target in TARGET_PREFIXES.items():
        if name.startswith(prefix):
            return target
    return None


def _dispatch_prompt(
    *,
    cfg: RunnerConfig,
    target: str,
    prompt_text: str,
    prompt_source: str,
    prompt_file_original: Optional[Path],
    prompt_file_processing: Optional[Path],
    prompt_char_count: int,
    base_hint: str,
    clipboard_char_count: Optional[int],
    cancel_event: Any | None = None,
) -> RunResult:
    target_cfg = cfg.targets[target]

    started_at = datetime.now(timezone.utc)
    stamp = started_at.strftime("%Y%m%dT%H%M%S%fZ")
    base_name = f"{stamp}_{target}_{_safe_stem(base_hint)}"

    command: list[str] = list(target_cfg.command)
    temp_prompt_path: Optional[Path] = None

    status = "failed"
    error_summary: Optional[str] = None
    return_code: Optional[int] = None
    stdout_text = ""
    stderr_text = ""

    started_ns = time.perf_counter_ns()

    try:
        if target_cfg.input_mode == "interactive_not_supported":
            status = "failed"
            error_summary = (
                f"Target `{target}` is configured as interactive_not_supported. "
                "Update target command/input_mode for non-interactive automation."
            )
        else:
            input_text: Optional[str] = None
            if target_cfg.input_mode == "stdin":
                input_text = prompt_text
            elif target_cfg.input_mode == "temp_file":
                temp_prompt_path = _write_temp_prompt(prompt_text=prompt_text, work_dir=cfg.paths.processing)
                command = _inject_or_append(command, placeholder="{prompt_file}", value=str(temp_prompt_path))
            elif target_cfg.input_mode == "arg":
                command = _inject_or_append(command, placeholder="{prompt}", value=prompt_text)
            else:
                raise RunnerError(f"Unsupported input_mode `{target_cfg.input_mode}` for target `{target}`.")

            status, return_code, stdout_text, stderr_text, error_summary = _run_command_with_controls(
                command=command,
                input_text=input_text,
                timeout_seconds=cfg.timeout_seconds,
                working_directory=cfg.working_directory,
                cancel_event=cancel_event,
            )
            if status == "failed" and not str(error_summary or "").strip():
                snippet = (stderr_text or stdout_text or "").strip().replace("\n", " ")
                snippet = snippet[:220] if snippet else ""
                if snippet:
                    error_summary = f"Command returned non-zero exit code {return_code}. Output: {snippet}"
                else:
                    error_summary = f"Command returned non-zero exit code {return_code}."

    except RunnerError as err:
        status = "failed"
        error_summary = str(err)
    except Exception as err:
        status = "failed"
        error_summary = str(err)
    finally:
        duration_ms = int((time.perf_counter_ns() - started_ns) / 1_000_000)
        if temp_prompt_path and temp_prompt_path.exists():
            try:
                temp_prompt_path.unlink()
            except Exception:
                pass

    output_file, stderr_file = _write_result_files(
        results_dir=cfg.paths.results,
        base_name=base_name,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        capture_stderr_separately=cfg.capture_stderr_separately,
    )

    prompt_final: Optional[Path] = None
    if prompt_file_processing and prompt_file_processing.exists():
        final_prompt_dir = cfg.paths.done if status == "success" else cfg.paths.failed
        prompt_final = _move_to_dir_unique(prompt_file_processing, final_prompt_dir)

    ended_at = datetime.now(timezone.utc)

    command_used = _sanitize_command_for_log(
        command=command,
        prompt_text=prompt_text,
        temp_prompt_path=temp_prompt_path,
    )

    prompt_file_value = str(prompt_file_original) if prompt_file_original else "<clipboard:memory>"
    prompt_file_final_value = str(prompt_final) if prompt_final else prompt_file_value

    record = {
        "timestamp": ended_at.isoformat(),
        "prompt_source": prompt_source,
        "prompt_file": prompt_file_value,
        "prompt_file_final": prompt_file_final_value,
        "prompt_char_count": int(prompt_char_count),
        "target": target,
        "command_used": command_used,
        "status": status,
        "duration_ms": duration_ms,
        "output_file": str(output_file),
        "stderr_file": (str(stderr_file) if stderr_file else None),
        "error_summary": error_summary,
        "return_code": return_code,
        "clipboard_char_count": clipboard_char_count,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
    }

    log_file = cfg.paths.logs / cfg.log_filename
    _append_jsonl(log_file, record)

    return RunResult(
        timestamp=record["timestamp"],
        prompt_source=record["prompt_source"],
        prompt_file=record["prompt_file"],
        prompt_file_final=record["prompt_file_final"],
        prompt_char_count=int(record["prompt_char_count"]),
        target=record["target"],
        command_used=record["command_used"],
        status=record["status"],
        duration_ms=int(record["duration_ms"]),
        output_file=record["output_file"],
        stderr_file=record["stderr_file"],
        error_summary=record["error_summary"],
        return_code=record["return_code"],
        log_file=str(log_file),
    )


def _load_yaml_or_json(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")

    try:
        import yaml  # type: ignore

        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RunnerError(
            "Unable to parse config. Install PyYAML for full YAML support, or provide JSON-compatible YAML. "
            f"Parse error: {e}"
        )

    if not isinstance(data, dict):
        raise RunnerError("Runner config must decode to an object/map.")
    return data


def _resolve_path(repo_root: Path, value: str) -> Path:
    p = Path(value).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (repo_root / p).resolve()


def _ensure_runner_dirs(paths: RunnerPaths) -> None:
    for p in [paths.inbox, paths.processing, paths.done, paths.failed, paths.results, paths.logs]:
        p.mkdir(parents=True, exist_ok=True)


def _resolve_target_file(*, source_prompt: Path, explicit_target: Optional[str], cfg: RunnerConfig) -> str:
    if explicit_target:
        t = explicit_target.strip().lower()
        if t == "auto":
            if cfg.default_target:
                return cfg.default_target
            raise RunnerError("`--target auto` requested but default_target is not set in config.")
        if t:
            return t

    if cfg.infer_target_from_filename:
        inferred = infer_target_from_filename(source_prompt.name)
        if inferred:
            return inferred

    if cfg.default_target:
        return cfg.default_target

    raise RunnerError(
        "Target not provided and could not be inferred from filename. "
        "Use --target codex|claude|auto or filename prefix codex_/claude_."
    )


def _resolve_target_clipboard(*, explicit_target: Optional[str], cfg: RunnerConfig) -> str:
    if explicit_target:
        t = explicit_target.strip().lower()
        if t == "auto":
            if cfg.default_target:
                return cfg.default_target
            raise RunnerError("`--target auto` requested but default_target is not set in config.")
        if t:
            return t

    if cfg.clipboard.require_target_explicit_for_clipboard:
        raise RunnerError(
            "Clipboard mode requires explicit --target codex|claude|auto (per config)."
        )

    if cfg.default_target:
        return cfg.default_target

    raise RunnerError(
        "Clipboard target not provided and default_target is not configured. "
        "Use --target codex|claude|auto."
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _move_to_dir_unique(source: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = target_dir / source.name
    if candidate.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        candidate = target_dir / f"{source.stem}_{stamp}{source.suffix}"
    os.replace(source, candidate)
    return candidate


def _write_temp_prompt(*, prompt_text: str, work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="runner_prompt_", suffix=".txt", dir=str(work_dir))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(prompt_text)
    return Path(temp_name)


def _inject_or_append(command: list[str], *, placeholder: str, value: str) -> list[str]:
    used_placeholder = False
    out: list[str] = []
    for arg in command:
        if placeholder in arg:
            out.append(arg.replace(placeholder, value))
            used_placeholder = True
        else:
            out.append(arg)
    if not used_placeholder:
        out.append(value)
    return out


def _safe_stem(text: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9._-]+", "_", text or "prompt")
    clean = clean.strip("_")
    return clean[:80] or "prompt"


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _write_result_files(
    *,
    results_dir: Path,
    base_name: str,
    stdout_text: str,
    stderr_text: str,
    capture_stderr_separately: bool,
) -> tuple[Path, Optional[Path]]:
    results_dir.mkdir(parents=True, exist_ok=True)

    output_file = results_dir / f"{base_name}.out.txt"
    stderr_file: Optional[Path] = None

    if capture_stderr_separately:
        output_file.write_text(stdout_text or "", encoding="utf-8")
        if (stderr_text or "").strip():
            stderr_file = results_dir / f"{base_name}.err.txt"
            stderr_file.write_text(stderr_text, encoding="utf-8")
    else:
        merged = stdout_text or ""
        if (stderr_text or "").strip():
            if merged and not merged.endswith("\n"):
                merged += "\n"
            merged += "\n[stderr]\n"
            merged += stderr_text
        output_file.write_text(merged, encoding="utf-8")

    return output_file, stderr_file


def _sanitize_command_for_log(
    *,
    command: list[str],
    prompt_text: str,
    temp_prompt_path: Optional[Path],
) -> str:
    safe_parts: list[str] = []
    for part in command:
        if prompt_text and part == prompt_text:
            safe_parts.append("<PROMPT_TEXT>")
            continue
        if temp_prompt_path and part == str(temp_prompt_path):
            safe_parts.append("<PROMPT_FILE>")
            continue
        safe_parts.append(part)
    return " ".join(shlex.quote(p) for p in safe_parts)


def _run_command_with_controls(
    *,
    command: list[str],
    input_text: Optional[str],
    timeout_seconds: int,
    working_directory: Path,
    cancel_event: Any | None = None,
) -> tuple[str, Optional[int], str, str, Optional[str]]:
    started = time.perf_counter()
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        shell=False,
        cwd=str(working_directory),
    )

    stdout_text = ""
    stderr_text = ""
    wrote_input = False

    try:
        while True:
            if not wrote_input and input_text is not None and proc.stdin is not None:
                try:
                    proc.stdin.write(input_text)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                finally:
                    wrote_input = True

            if cancel_event is not None and bool(getattr(cancel_event, "is_set", lambda: False)()):
                _interrupt_process(proc)
                try:
                    out, err = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                stdout_text = out or ""
                stderr_text = err or ""
                return ("cancelled", int(proc.returncode) if proc.returncode is not None else None, stdout_text, stderr_text, "Command cancelled by operator.")

            rc = proc.poll()
            if rc is not None:
                out, err = proc.communicate()
                stdout_text = out or ""
                stderr_text = err or ""
                status = "success" if int(rc) == 0 else "failed"
                error_summary = None
                if status != "success" and not stderr_text.strip():
                    error_summary = f"Command returned non-zero exit code {int(rc)}."
                return (status, int(rc), stdout_text, stderr_text, error_summary)

            elapsed = time.perf_counter() - started
            if elapsed >= float(timeout_seconds):
                _interrupt_process(proc)
                try:
                    out, err = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    out, err = proc.communicate()
                stdout_text = out or ""
                stderr_text = err or ""
                return (
                    "timeout",
                    int(proc.returncode) if proc.returncode is not None else None,
                    stdout_text,
                    stderr_text,
                    f"Command timed out after {int(timeout_seconds)} seconds.",
                )

            time.sleep(0.2)
    except Exception:
        try:
            _interrupt_process(proc)
        except Exception:
            pass
        raise


def _interrupt_process(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
    except Exception:
        pass
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    try:
        proc.terminate()
    except Exception:
        pass
    deadline = time.time() + 1.0
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    try:
        proc.kill()
    except Exception:
        pass


def _append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _read_clipboard_text(*, clipboard_getter: Optional[Callable[[], str]] = None) -> str:
    if clipboard_getter is not None:
        try:
            return str(clipboard_getter() or "")
        except Exception as e:
            raise RunnerError(f"Clipboard read failed (custom getter): {e}")

    errors: list[str] = []

    try:
        import pyperclip  # type: ignore

        return str(pyperclip.paste() or "")
    except Exception as e:
        errors.append(f"pyperclip unavailable/failed: {e}")

    backends = [
        ["pbpaste"],
        ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
        ["wl-paste", "-n"],
    ]

    for cmd in backends:
        exe = cmd[0]
        if shutil.which(exe) is None:
            continue
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
                check=False,
            )
            if out.returncode == 0:
                return out.stdout or ""
            errors.append(f"{exe} exited {out.returncode}: {(out.stderr or '').strip()[:200]}")
        except Exception as e:
            errors.append(f"{exe} failed: {e}")

    if not errors:
        raise RunnerError(
            "Clipboard read failed: no supported backend found. Install pyperclip or a platform clipboard tool."
        )

    raise RunnerError(
        "Clipboard read failed. " + " | ".join(errors)
    )
