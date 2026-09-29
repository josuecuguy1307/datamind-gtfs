from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from .runner_core import RunnerError, run_clipboard_prompt, run_prompt_file
except Exception:  # pragma: no cover
    from runner_core import RunnerError, run_clipboard_prompt, run_prompt_file  # type: ignore


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[1] / "config" / "runner_config.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a prompt file or clipboard prompt through a configured coding assistant CLI target."
    )
    parser.add_argument(
        "--file",
        required=False,
        help="Prompt file path (expected inside local_runner/inbox/).",
    )
    parser.add_argument(
        "--clipboard",
        action="store_true",
        help="Read prompt from clipboard instead of --file.",
    )
    parser.add_argument(
        "--target",
        required=False,
        help="Target assistant: codex, claude, or auto (default target from config).",
    )
    parser.add_argument(
        "--no-persist-prompt",
        action="store_true",
        help="Clipboard mode only: do not persist clipboard prompt artifact to inbox/processing.",
    )
    parser.add_argument(
        "--print-output",
        action="store_true",
        help="Print saved stdout artifact to console after run.",
    )
    parser.add_argument(
        "--config",
        required=False,
        default=str(_default_config_path()),
        help="Runner config path. Default: local_runner/config/runner_config.yaml",
    )

    args = parser.parse_args(argv)

    if args.clipboard and args.file:
        parser.error("Use either --clipboard or --file, not both.")

    if not args.clipboard and not args.file:
        parser.error("--file is required unless --clipboard is used.")

    target = str(args.target).strip() if args.target else None

    try:
        if args.clipboard:
            result = run_clipboard_prompt(
                target=target,
                config_path=Path(args.config),
                persist_prompt=(False if args.no_persist_prompt else None),
            )
        else:
            result = run_prompt_file(
                file_path=Path(args.file),
                target=target,
                config_path=Path(args.config),
            )
    except RunnerError as err:
        print(f"[runner] ERROR: {err}", file=sys.stderr)
        return 2
    except Exception as err:  # pragma: no cover
        print(f"[runner] UNEXPECTED ERROR: {err}", file=sys.stderr)
        return 3

    print(
        "[runner] "
        f"source={result.prompt_source} "
        f"status={result.status} "
        f"target={result.target} "
        f"chars={result.prompt_char_count} "
        f"output={result.output_file} "
        f"stderr={result.stderr_file or '-'} "
        f"prompt={result.prompt_file} "
        f"prompt_final={result.prompt_file_final} "
        f"log={result.log_file} "
        f"duration_ms={result.duration_ms}"
    )

    if args.print_output:
        try:
            text = Path(result.output_file).read_text(encoding="utf-8")
        except Exception as e:
            print(f"[runner] WARN: unable to read output file for printing: {e}", file=sys.stderr)
        else:
            print("\n[runner] --- output start ---")
            print(text, end="" if text.endswith("\n") else "\n")
            print("[runner] --- output end ---")

    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
