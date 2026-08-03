from fastapi import APIRouter, HTTPException
from uuid import UUID

from app.db.connections import get_conn
from app.db.working_memory import (
    expire_stale_working_memory,
    get_working_memory,
    resolve_working_memory,
)

router = APIRouter()


def _get_owned_user_id(conn, external_user_id: str, session_id: UUID):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT u.id
            FROM users u JOIN sessions s ON s.user_id = u.id
            WHERE u.external_user_id = %s AND s.id = %s
            """,
            (external_user_id, session_id),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User or session not found")
    return row[0]


@router.get("/users/{external_user_id}/sessions/{session_id}/working-memory")
def get_wm_endpoint(external_user_id: str, session_id: UUID):
    try:
        with get_conn() as conn:
            user_id = _get_owned_user_id(conn, external_user_id, session_id)
            wm = get_working_memory(conn, user_id=user_id, session_id=session_id)
        return {"status": "active" if wm else "none", "working_memory": wm}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete("/users/{external_user_id}/sessions/{session_id}/working-memory")
def clear_wm_endpoint(external_user_id: str, session_id: UUID):
    try:
        with get_conn() as conn:
            user_id = _get_owned_user_id(conn, external_user_id, session_id)
            resolved = resolve_working_memory(conn, user_id=user_id, session_id=session_id)
        return {"status": "archived" if resolved else "none"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/admin/expire-working-memory")
def expire_wm_endpoint():
    try:
        with get_conn() as conn:
            count = expire_stale_working_memory(conn)
        return {"expired_count": count}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
