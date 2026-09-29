"""Database connection resolution with NO default values.

Nothing in this project ships with a default user, password or server: the connection
ALWAYS comes from your own configuration (`.env`, see `.env.example`).
If it is missing, the code stops with a message that says which variable to set.
"""
from __future__ import annotations

import os
from typing import Optional


class MissingConfigError(RuntimeError):
    """A required configuration variable is missing."""


def need_dsn(value: Optional[str] = None, *, name: str = "DB_DSN") -> str:
    """Return `value` if given; otherwise DB_DSN / DATABASE_URL; otherwise raise a clear error.

    Example DSN: ``postgresql://USER:PASSWORD@127.0.0.1:5432/datamind_ml``
    """
    dsn = (value or os.getenv(name) or os.getenv("DATABASE_URL") or "").strip()
    if not dsn or dsn.upper().startswith("TU_") or "USER:PASSWORD@" in dsn:
        raise MissingConfigError(
            f"Missing database connection: set {name} (or DATABASE_URL) in your .env, "
            "e.g. postgresql://USER:PASSWORD@127.0.0.1:5432/datamind_ml. "
            "Copy .env.example to .env and fill in your own values (see README)."
        )
    return dsn
