"""PostgreSQL-backed asynchronous embedding backfill and refresh jobs."""

from __future__ import annotations

import json
from typing import Any

from app.db.connections import get_conn
from app.services.embeddings import (
    EMBEDDING_MODEL,
    belief_embedding_text,
    embedding_literal,
    episode_embedding_text,
    get_embedding_provider,
    source_hash,
)

EPISODE_JOB = "embed_episode"
BELIEF_JOB = "embed_belief"


def enqueue_pending_embedding_jobs() -> int:
    """Queue missing/stale embeddings without duplicate in-flight jobs."""
    queued = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT e.id
                FROM episodes e
                LEFT JOIN episode_embeddings ee ON ee.episode_id = e.id
                WHERE e.is_archived = FALSE
                  AND (ee.episode_id IS NULL OR ee.source_hash IS NULL OR ee.generated_at < e.created_at)
                LIMIT 100
                """
            )
            episode_ids = [row[0] for row in cur.fetchall()]
            cur.execute(
                """
                SELECT b.id
                FROM beliefs b
                LEFT JOIN belief_embeddings be ON be.belief_id = b.id
                WHERE b.status = 'active'
                  AND (be.belief_id IS NULL OR be.source_hash IS NULL OR be.generated_at < b.last_validated_at)
                LIMIT 100
                """
            )
            belief_ids = [row[0] for row in cur.fetchall()]
            for job_type, entity_id in [(EPISODE_JOB, value) for value in episode_ids] + [(BELIEF_JOB, value) for value in belief_ids]:
                cur.execute(
                    """
                    INSERT INTO background_jobs (job_type, status, payload, scheduled_at)
                    SELECT %s, 'queued', jsonb_build_object('entity_id', %s::text), NOW()
                    WHERE NOT EXISTS (
                        SELECT 1 FROM background_jobs
                        WHERE job_type = %s
                          AND (
                              status IN ('queued', 'running')
                              OR (status = 'failed' AND created_at > NOW() - INTERVAL '1 hour')
                          )
                          AND payload->>'entity_id' = %s::text
                    )
                    """,
                    (job_type, entity_id, job_type, entity_id),
                )
                queued += cur.rowcount
    return queued


def _claim_embedding_job():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM background_jobs
                    WHERE job_type IN (%s, %s)
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
                RETURNING jobs.id, jobs.job_type, jobs.payload
                """,
                (EPISODE_JOB, BELIEF_JOB),
            )
            return cur.fetchone()


def _load_entity(job_type: str, entity_id):
    with get_conn() as conn:
        with conn.cursor() as cur:
            if job_type == EPISODE_JOB:
                cur.execute(
                    """
                    SELECT id, event_type, content, summary, entities, tags
                    FROM episodes WHERE id = %s AND is_archived = FALSE
                    """, (entity_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return "episode", row[0], episode_embedding_text({"event_type": row[1], "content": row[2], "summary": row[3], "entities": row[4], "tags": row[5]})
            cur.execute(
                """
                SELECT id, namespace, subject, predicate, object_value
                FROM beliefs WHERE id = %s AND status = 'active'
                """, (entity_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return "belief", row[0], belief_embedding_text({"namespace": row[1], "subject": row[2], "predicate": row[3], "object_value": row[4]})


def _store_embedding(kind: str, entity_id, text: str, vector: list[float]) -> None:
    table = "episode_embeddings" if kind == "episode" else "belief_embeddings"
    id_column = "episode_id" if kind == "episode" else "belief_id"
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {table} ({id_column}, embedding, embedding_model, source_hash, generated_at, updated_at)
                VALUES (%s, %s::vector, %s, %s, NOW(), NOW())
                ON CONFLICT ({id_column}) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    embedding_model = EXCLUDED.embedding_model,
                    source_hash = EXCLUDED.source_hash,
                    generated_at = NOW(),
                    updated_at = NOW()
                """,
                (entity_id, embedding_literal(vector), EMBEDDING_MODEL, source_hash(text)),
            )


def _finish_job(job_id, *, error: str | None = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE background_jobs
                SET status = %s, completed_at = NOW(), error_message = %s
                WHERE id = %s
                """,
                ("failed" if error else "completed", error[:2000] if error else None, job_id),
            )


def run_due_embedding_jobs(max_jobs: int = 10) -> int:
    """Embed up to ``max_jobs`` memories; provider outages mark jobs failed safely."""
    enqueue_pending_embedding_jobs()
    processed = 0
    for _ in range(max_jobs):
        job = _claim_embedding_job()
        if not job:
            break
        job_id, job_type, payload = job
        try:
            entity = _load_entity(job_type, payload["entity_id"])
            if entity:
                kind, entity_id, text = entity
                vector = get_embedding_provider().embed([text])[0]
                _store_embedding(kind, entity_id, text, vector)
            _finish_job(job_id)
            processed += 1
        except Exception as exc:
            _finish_job(job_id, error=str(exc))
    return processed
