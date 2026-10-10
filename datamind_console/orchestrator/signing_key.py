"""The key the autopilot signs operator decisions with (HMAC-SHA256).

Resolution order: an explicit argument, then ``HADES_OPERATOR_SIGNING_SECRET``,
then ``DATAMIND_OPERATOR_SIGNING_SECRET``. When none is set the autopilot keeps
working with a public development key, but it logs a warning and the console
Settings page says so: anyone who reads the code could forge those signatures.

Each installation configures its own key:

    python -m datamind_console.orchestrator.signing_key            # print a new key
    python -m datamind_console.orchestrator.signing_key --write .env

``--write`` adds the line to the given env file and refuses to replace a key
that is already there unless ``--force`` is passed. Changing the key only
affects new signatures; recorded decisions keep the signature they were
stored with. The key value is never logged or shown in the console.
"""
from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
from pathlib import Path
from typing import Optional

ENV_VARS = ("HADES_OPERATOR_SIGNING_SECRET", "DATAMIND_OPERATOR_SIGNING_SECRET")
PRIMARY_ENV_VAR = ENV_VARS[0]
DEVELOPMENT_KEY = "hades-autopilot-dev-secret"
DEVELOPMENT_SOURCE = "development_default"

LOGGER = logging.getLogger(__name__)
_warned = False


def resolve(explicit: Optional[str] = None) -> tuple[str, str]:
    """Return ``(key, source)``; ``source`` names where the key came from, never the key."""
    global _warned
    if explicit:
        return str(explicit), "argument"
    for name in ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    if not _warned:
        LOGGER.warning(
            "autopilot.signing_key: no %s set; operator decisions are signed with the public "
            "development key. Run `python -m datamind_console.orchestrator.signing_key --write .env`.",
            PRIMARY_ENV_VAR,
        )
        _warned = True
    return DEVELOPMENT_KEY, DEVELOPMENT_SOURCE


def source() -> str:
    """Where the key would come from right now (for status displays); never the key itself."""
    for name in ENV_VARS:
        if (os.getenv(name) or "").strip():
            return name
    return DEVELOPMENT_SOURCE


def is_development_key() -> bool:
    return source() == DEVELOPMENT_SOURCE


def generate() -> str:
    """A new random key: 48 bytes, URL-safe."""
    return secrets.token_urlsafe(48)


def write_to_env_file(path: Path, key: str, *, force: bool = False) -> str:
    """Add ``HADES_OPERATOR_SIGNING_SECRET=<key>`` to ``path``; return "added" or "replaced"."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{PRIMARY_ENV_VAR}="
    existing = [i for i, line in enumerate(lines) if line.strip().startswith(prefix)]
    if existing and lines[existing[0]].strip() != prefix and not force:
        raise FileExistsError(f"{path} already sets {PRIMARY_ENV_VAR}; pass --force to replace it")
    new_line = f"{prefix}{key}"
    if existing:
        lines[existing[0]] = new_line
        result = "replaced"
    else:
        lines.append(new_line)
        result = "added"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Create this installation's autopilot signing key.")
    parser.add_argument("--write", type=Path, metavar="ENV_FILE", help=f"add {PRIMARY_ENV_VAR} to this env file instead of printing it")
    parser.add_argument("--force", action="store_true", help="replace a key already present in ENV_FILE")
    args = parser.parse_args(argv)

    key = generate()
    if args.write is None:
        print(f"{PRIMARY_ENV_VAR}={key}")
        print(f"# Add the line above to your .env (or run with --write .env). Status now: {source()}.", file=sys.stderr)
        return 0
    try:
        result = write_to_env_file(args.write, key, force=args.force)
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{PRIMARY_ENV_VAR} {result} in {args.write} (value not shown). Restart the console to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
