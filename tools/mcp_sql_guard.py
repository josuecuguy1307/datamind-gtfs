"""Validation for the MCP server's free-form ``query_db`` tool.

The DataMind MCP server is a developer lens: Claude Code (or any MCP client)
can send arbitrary SQL. A read-only *transaction* alone is not a sufficient
guarantee:

* ``SELECT 1; COMMIT; DELETE FROM t``: in a multi-statement simple query,
  statements after an explicit ``COMMIT`` run in a new transaction that is
  read-write.
* ``COPY (SELECT 1) TO PROGRAM '...'`` runs a shell command and is permitted
  in read-only transactions for superusers. The local role is a superuser.
* ``dblink_exec(...)`` opens a separate connection that is not read-only.

So this module rejects those shapes *before* execution:

* exactly one statement;
* an allow-listed leading keyword;
* no side-effecting functions.

The server also opens sessions with ``default_transaction_read_only=on`` and
a statement timeout. Nothing here widens permissions; it only narrows them.
"""
from __future__ import annotations

import re

ALLOWED_FIRST_KEYWORDS = frozenset({"SELECT", "WITH", "VALUES", "TABLE", "SHOW", "EXPLAIN"})
ROW_RETURNING_KEYWORDS = frozenset({"SELECT", "WITH", "VALUES", "TABLE"})
MAX_ROWS = 500

# Functions with side effects outside the read-only transaction (other
# connections, files, server processes, configuration, locks, sleeping).
_FORBIDDEN_FUNCTION_PATTERNS = (
    r"dblink\w*",
    r"pg_terminate_backend",
    r"pg_cancel_backend",
    r"pg_reload_conf",
    r"pg_rotate_logfile",
    r"pg_read_file",
    r"pg_read_binary_file",
    r"pg_ls_\w+",
    r"pg_stat_file",
    r"pg_file_\w+",
    r"lo_\w+",
    r"set_config",
    r"pg_sleep\w*",
    r"pg_advisory\w*",
    r"pg_try_advisory\w*",
    r"pg_notify",
    r"pg_logical_\w+",
    r"pg_create_\w+",
    r"pg_drop_replication_slot",
    r"pg_replication_\w+",
    r"pg_switch_wal",
    r"pg_promote",
    r"pg_backup_\w+",
    r"pg_wal_replay_\w+",
    r"pg_import_system_collations",
)
_FORBIDDEN_FUNCTIONS = re.compile(
    r"(?<![\w$])(?:" + "|".join(_FORBIDDEN_FUNCTION_PATTERNS) + r")\s*\(",
    re.IGNORECASE,
)
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_HAS_LIMIT = re.compile(r"\blimit\s+(?:\d+|all)\b|\bfetch\s+(?:first|next)\b", re.IGNORECASE)


class UnsafeSQLError(ValueError):
    """Raised when a query is not a single, plain read."""


def scrub(sql: str) -> str:
    """Return *sql* with comments and literal contents neutralised.

    String and dollar-quoted literals become ``''`` and comments become a
    space. Quoted identifiers keep their text (lower-cased, without quotes),
    so ``"dblink_exec"(...)`` is still detected.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j
            out.append(" ")
            continue
        if sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            out.append(" ")
            i = j
            continue
        if ch in "eE" and i + 1 < n and sql[i + 1] == "'" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            j = i + 2
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append("''")
            i = j + 1
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append("''")
            i = j + 1
            continue
        if ch == '"':
            j = sql.find('"', i + 1)
            j = n if j == -1 else j
            out.append(sql[i + 1:j].lower())
            i = j + 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                j = n if j == -1 else j
                out.append("''")
                i = j + len(tag)
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def validate_read_query(sql: str) -> tuple[str, bool]:
    """Validate *sql* for ``query_db``.

    Returns ``(clean_sql, add_limit)``, where *clean_sql* has its trailing
    semicolons removed and *add_limit* says whether to append
    ``LIMIT {MAX_ROWS}``. Raises :class:`UnsafeSQLError` otherwise.
    """
    if not sql or not sql.strip():
        raise UnsafeSQLError("Empty query.")
    clean = sql.strip()
    while clean.endswith(";"):
        clean = clean[:-1].rstrip()
    scrubbed = scrub(clean).strip()
    if not scrubbed:
        raise UnsafeSQLError("Empty query.")
    if ";" in scrubbed:
        raise UnsafeSQLError("Only one SQL statement per query is allowed.")
    first = scrubbed.split(None, 1)[0].upper().lstrip("(")
    if first not in ALLOWED_FIRST_KEYWORDS:
        raise UnsafeSQLError(
            f"Only read queries are allowed (SELECT, WITH, VALUES, TABLE, SHOW, EXPLAIN); got {first!r}."
        )
    forbidden = _FORBIDDEN_FUNCTIONS.search(scrubbed)
    if forbidden:
        name = forbidden.group(0).rstrip("( ").strip()
        raise UnsafeSQLError(f"Function {name!r} has side effects outside a read-only transaction and is not allowed.")
    add_limit = first in ROW_RETURNING_KEYWORDS and not _HAS_LIMIT.search(scrubbed)
    return clean, add_limit
