"""Recurring PostgreSQL-backed maintenance for working memory."""

from __future__ import annotations

from app.db.connections import get_conn
from app.db.working_memory import expire_stale_working_memory

EXPIRY_JOB_TYPE = "expire_working_memory"


def ensure_working_memory_expiry_job() -> None:
    """Ensure exactly one due/running expiry job exists."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO background_jobs (job_type, status, payload, scheduled_at)
                SELECT %s, 'queued', '{"scope":"working_memory"}'::jsonb, NOW()
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM background_jobs
                    WHERE job_type = %s
                      AND status IN ('queued', 'running')
                )
                """,
                (EXPIRY_JOB_TYPE, EXPIRY_JOB_TYPE),
            )


def run_due_working_memory_jobs() -> int:
    """Claim and run one expiry job, then enqueue its next interval.

    ``FOR UPDATE SKIP LOCKED`` makes this safe when multiple API workers start
    the scheduler. The database remains the coordination authority.
    """
    ensure_working_memory_expiry_job()

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM background_jobs
                    WHERE job_type = %s
                      AND status = 'queued'
                      AND COALESCE(scheduled_at, NOW()) <= NOW()
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE background_jobs jobs
                SET status = 'running', started_at = NOW()
                FROM candidate
                WHERE jobs.id = candidate.id
                RETURNING jobs.id
                """,
                (EXPIRY_JOB_TYPE,),
            )
            row = cur.fetchone()

        if not row:
            return 0

        job_id = row[0]
        try:
            expired_count = expire_stale_working_memory(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'completed', completed_at = NOW()
                    WHERE id = %s
                    """,
                    (job_id,),
                )
                cur.execute(
                    """
                    INSERT INTO background_jobs (job_type, status, payload, scheduled_at)
                    VALUES (%s, 'queued', '{"scope":"working_memory"}'::jsonb,
                            NOW() + INTERVAL '5 minutes')
                    """,
                    (EXPIRY_JOB_TYPE,),
                )
            return expired_count
        except Exception as exc:
            # The expiry query may have put this transaction into an aborted
            # state; rollback before recording the failure in background_jobs.
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE background_jobs
                    SET status = 'failed', completed_at = NOW(), error_message = %s
                    WHERE id = %s
                    """,
                    (str(exc)[:2000], job_id),
                )
            raise
