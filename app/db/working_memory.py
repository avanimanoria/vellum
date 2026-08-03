"""Database access for the short-lived, session-scoped working-memory layer."""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional
from uuid import UUID

WORKING_MEMORY_TTL_SQL = "INTERVAL '90 minutes'"


def _state_from_row(row: tuple, items: Optional[list[dict]] = None) -> Dict[str, Any]:
    return {
        "id": str(row[0]),
        "user_id": str(row[1]),
        "session_id": str(row[2]),
        "agent_key": row[3],
        "active_goal": row[4],
        "execution_state": row[5],
        "task_context": row[6] or {},
        "last_source_turn_id": str(row[7]) if row[7] else None,
        "status": row[8],
        "version": row[9],
        "last_refreshed_at": row[10].isoformat() if row[10] else None,
        "expires_at": row[11].isoformat() if row[11] else None,
        "created_at": row[12].isoformat() if row[12] else None,
        "updated_at": row[13].isoformat() if row[13] else None,
        "items": items or [],
    }


STATE_COLUMNS = """
    id, user_id, session_id, agent_key, active_goal, execution_state,
    task_context, last_source_turn_id, status, version, last_refreshed_at,
    expires_at, created_at, updated_at
"""


def get_working_memory(
    conn, *, user_id: UUID, session_id: UUID, agent_key: str = "primary"
) -> Optional[Dict[str, Any]]:
    """Return only active, non-expired state belonging to this user/session."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {STATE_COLUMNS}
            FROM working_memory_states
            WHERE user_id = %s AND session_id = %s AND agent_key = %s
              AND status = 'active' AND expires_at > NOW()
            """,
            (user_id, session_id, agent_key),
        )
        row = cur.fetchone()
        if not row:
            return None

        cur.execute(
            """
            SELECT id, kind, dedupe_key, content, priority, confidence, metadata,
                   source_turn_id, expires_at
            FROM working_memory_items
            WHERE working_memory_state_id = %s
              AND status = 'active' AND expires_at > NOW()
            ORDER BY priority DESC, updated_at DESC
            LIMIT 12
            """,
            (row[0],),
        )
        items = [
            {
                "id": str(item[0]), "kind": item[1], "dedupe_key": item[2],
                "content": item[3], "priority": item[4],
                "confidence": float(item[5]), "metadata": item[6] or {},
                "source_turn_id": str(item[7]) if item[7] else None,
                "expires_at": item[8].isoformat() if item[8] else None,
            }
            for item in cur.fetchall()
        ]
    return _state_from_row(row, items)


def apply_working_memory_patch(
    conn, *, user_id: UUID, session_id: UUID, source_turn_id: UUID,
    patch: Dict[str, Any], expected_version: int, agent_key: str = "primary"
) -> Optional[Dict[str, Any]]:
    """Apply a validated patch when the state snapshot is still current.

    Returns ``None`` on an optimistic-lock conflict. The caller must keep the
    assistant turn but discard the stale patch and return freshly read state.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO working_memory_states (user_id, session_id, agent_key, expires_at)
            VALUES (%s, %s, %s, NOW() + {WORKING_MEMORY_TTL_SQL})
            ON CONFLICT (session_id, agent_key) DO NOTHING
            RETURNING id
            """,
            (user_id, session_id, agent_key),
        )
        inserted = cur.fetchone() is not None
        cur.execute(
            f"""
            SELECT {STATE_COLUMNS}
            FROM working_memory_states
            WHERE user_id = %s AND session_id = %s AND agent_key = %s
            FOR UPDATE
            """,
            (user_id, session_id, agent_key),
        )
        before = cur.fetchone()
        if not before:
            raise ValueError("session does not belong to user")

        # A non-existent active snapshot is version 0. A row created by this
        # transaction is therefore valid; an existing newer row is not.
        is_reactivation = before[8] != "active"
        if not ((inserted and expected_version == 0) or (is_reactivation and expected_version == 0)):
            if before[9] != expected_version:
                return None

        before_snapshot = _state_from_row(before)
        refresh_ttl = patch.get("refresh_ttl", False) or is_reactivation
        active_goal = patch.get("active_goal", before[4])
        execution_state = patch.get("execution_state", before[5])
        task_context = patch.get("task_context", before[6] or {})
        status = "active"
        expires_sql = f"NOW() + {WORKING_MEMORY_TTL_SQL}" if refresh_ttl else "expires_at"
        cur.execute(
            f"""
            UPDATE working_memory_states
            SET active_goal = %s, execution_state = %s, task_context = %s::jsonb,
                last_source_turn_id = %s, status = %s,
                version = version + 1, updated_at = NOW(),
                last_refreshed_at = CASE WHEN %s THEN NOW() ELSE last_refreshed_at END,
                expires_at = {expires_sql}
            WHERE id = %s
            RETURNING {STATE_COLUMNS}
            """,
            (active_goal, execution_state, json.dumps(task_context), source_turn_id,
             status, refresh_ttl, before[0]),
        )
        after = cur.fetchone()

        for key in patch.get("resolve_item_keys", []):
            cur.execute(
                """
                UPDATE working_memory_items
                SET status = 'resolved', updated_at = NOW()
                WHERE working_memory_state_id = %s AND dedupe_key = %s AND status = 'active'
                """,
                (after[0], key),
            )

        for item in patch.get("upsert_items", []):
            cur.execute(
                f"""
                INSERT INTO working_memory_items (
                    working_memory_state_id, kind, dedupe_key, content, priority,
                    confidence, metadata, source_turn_id, expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, NOW() + {WORKING_MEMORY_TTL_SQL})
                ON CONFLICT (working_memory_state_id, kind, dedupe_key) DO UPDATE SET
                    content = EXCLUDED.content, priority = EXCLUDED.priority,
                    confidence = EXCLUDED.confidence, metadata = EXCLUDED.metadata,
                    source_turn_id = EXCLUDED.source_turn_id, status = 'active',
                    last_confirmed_at = NOW(), expires_at = EXCLUDED.expires_at,
                    updated_at = NOW()
                """,
                (after[0], item["kind"], item["dedupe_key"], item["content"],
                 item.get("priority", 50), item.get("confidence", 1.0),
                 json.dumps(item.get("metadata", {})), source_turn_id),
            )

        action = "created" if before[9] == 1 and before[4] is None else "updated"
        cur.execute(
            """
            INSERT INTO working_memory_revision_log (
                user_id, working_memory_state_id, source_turn_id, action, before_state, after_state
            ) VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb)
            """,
            (user_id, after[0], source_turn_id, action,
             json.dumps(before_snapshot), json.dumps(_state_from_row(after))),
        )

    # Re-read so callers receive active items after the patch.
    return get_working_memory(conn, user_id=user_id, session_id=session_id, agent_key=agent_key) or _state_from_row(after)


def resolve_working_memory(conn, *, user_id: UUID, session_id: UUID, agent_key: str = "primary") -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE working_memory_states
            SET status = 'archived', execution_state = 'done', version = version + 1,
                updated_at = NOW()
            WHERE user_id = %s AND session_id = %s AND agent_key = %s AND status = 'active'
            RETURNING id
            """,
            (user_id, session_id, agent_key),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                "UPDATE working_memory_items SET status = 'resolved', updated_at = NOW() WHERE working_memory_state_id = %s AND status = 'active'",
                (row[0],),
            )
    return row is not None


def expire_stale_working_memory(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH expired AS (
                UPDATE working_memory_states
                SET status = 'expired', version = version + 1, updated_at = NOW()
                WHERE status = 'active' AND expires_at <= NOW()
                RETURNING id, user_id, last_source_turn_id
            ), expired_items AS (
                UPDATE working_memory_items
                SET status = 'expired', updated_at = NOW()
                WHERE status = 'active' AND working_memory_state_id IN (SELECT id FROM expired)
            )
            INSERT INTO working_memory_revision_log (
                user_id, working_memory_state_id, source_turn_id, action, before_state, after_state
            )
            SELECT user_id, id, last_source_turn_id, 'expired', '{}'::jsonb, '{}'::jsonb FROM expired
            RETURNING working_memory_state_id
            """
        )
        return len(cur.fetchall())
