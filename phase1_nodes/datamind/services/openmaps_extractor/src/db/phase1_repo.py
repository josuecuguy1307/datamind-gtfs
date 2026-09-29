from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence, Union
from uuid import UUID

from phase1_nodes.datamind.services.openmaps_extractor.src.db.repo import (
    fetchone,
    exec_sql,
)

from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    T_NODE_SETS,
    T_SELECTION_LOG,
    T_BANDIT_STATE,
)


JsonDict = Dict[str, Any]
UUIDLike = Union[UUID, str]


# ============================================================
# Helpers
# ============================================================

def _to_uuid_list(vals: Sequence[UUIDLike] | None) -> List[UUID]:
    if not vals:
        return []
    out: List[UUID] = []
    for v in vals:
        if isinstance(v, UUID):
            out.append(v)
        else:
            out.append(UUID(str(v)))
    return out


def _parse_pg_uuid_array(val: Any) -> List[UUID]:
    """
    psycopg2 may return uuid[] as:
      - list[UUID] or list[str] (good)
      - OR a Postgres array literal string like "{uuid1,uuid2}"
    Normalize to List[UUID].
    """
    if val is None:
        return []

    if isinstance(val, (list, tuple)):
        return _to_uuid_list(val)

    if isinstance(val, str):
        s = val.strip()
        if s.startswith("{") and s.endswith("}"):
            s = s[1:-1].strip()

        if not s:
            return []

        parts = [p.strip().strip('"') for p in s.split(",") if p.strip()]
        return [UUID(p) for p in parts]

    # last resort
    return [UUID(str(val))]


# ============================================================
# Node-set persistence
# ============================================================

def create_node_set(
    conn,
    source_run_ids: List[UUIDLike],
    action_ids: List[str],
    params_used: JsonDict,
) -> UUID:
    run_ids = [str(x) for x in _to_uuid_list(source_run_ids)]  # <- strings

    row = fetchone(
        conn,
        f"""
        INSERT INTO {T_NODE_SETS} (source_run_ids, action_ids, params_used)
        VALUES (%s::uuid[], %s::text[], %s::jsonb)
        RETURNING node_set_id
        """,
        (run_ids, action_ids, json.dumps(params_used)),
    )
    return row["node_set_id"]


def update_node_set_params_used(
    conn,
    node_set_id: UUIDLike,
    params_used: JsonDict,
) -> None:
    exec_sql(
        conn,
        f"""
        UPDATE {T_NODE_SETS}
        SET params_used = %s::jsonb
        WHERE node_set_id = %s
        """,
        (json.dumps(params_used), str(node_set_id)),
    )


def get_node_set(conn, node_set_id: UUIDLike):
    return fetchone(
        conn,
        f"SELECT * FROM {T_NODE_SETS} WHERE node_set_id = %s",
        (str(node_set_id),),
    )


def get_node_set_run_ids(conn, node_set_id: UUIDLike) -> List[UUID]:
    row = fetchone(
        conn,
        f"""
        SELECT source_run_ids
        FROM {T_NODE_SETS}
        WHERE node_set_id = %s
        """,
        (str(node_set_id),),
    )
    if not row:
        return []
    return _parse_pg_uuid_array(row.get("source_run_ids"))


def update_node_set_rank(
    conn,
    node_set_id: UUIDLike,
    rank_score: float,
    rank_model_ver: str,
) -> None:
    exec_sql(
        conn,
        f"""
        UPDATE {T_NODE_SETS}
        SET rank_score = %s,
            rank_model_ver = %s
        WHERE node_set_id = %s
        """,
        (rank_score, rank_model_ver, str(node_set_id)),
    )


def update_node_set_params(
    conn,
    node_set_id: UUIDLike,
    params_used: JsonDict,
) -> None:
    exec_sql(
        conn,
        f"""
        UPDATE {T_NODE_SETS}
        SET params_used = %s::jsonb
        WHERE node_set_id = %s
        """,
        (json.dumps(params_used), str(node_set_id)),
    )


# ============================================================
# Selection logging (reward signal, human-in-the-loop)
# ============================================================

def log_selection(
    conn,
    *,
    decision_id: str,
    node_set_id: UUIDLike,
    chosen: bool,
    reward: float = 0.0,
    notes: Optional[str] = None,
) -> None:
    """Log a human accept/reject decision for a node_set (reward signal)."""
    exec_sql(
        conn,
        f"""
        INSERT INTO {T_SELECTION_LOG}
          (phase, object_type, chosen_set_id, rejected_set_ids,
           bandit_action_ids, params, metrics, context)
        VALUES (
          1, 'node_set',
          %s::uuid,
          ARRAY[]::uuid[],
          ARRAY[]::text[],
          %s::jsonb, %s::jsonb, %s::jsonb
        )
        """,
        (
            str(node_set_id),
            json.dumps({"decision_id": decision_id, "chosen": chosen}),
            json.dumps({"reward": reward}),
            json.dumps({"notes": notes or ""}),
        ),
    )



# ============================================================
# Bandit persistence (STATE-BASED, schema-aligned)
# ============================================================

def load_bandit_state(conn, key: str) -> Optional[JsonDict]:
    row = fetchone(
        conn,
        f"SELECT state FROM {T_BANDIT_STATE} WHERE key = %s",
        (key,),
    )
    return row["state"] if row else None


def save_bandit_state(conn, key: str, state: JsonDict) -> None:
    exec_sql(
        conn,
        f"""
        INSERT INTO {T_BANDIT_STATE} (key, state)
        VALUES (%s, %s::jsonb)
        ON CONFLICT (key)
        DO UPDATE SET
            state = EXCLUDED.state,
            updated_at = now()
        """,
        (key, json.dumps(state)),
    )
