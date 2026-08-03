from __future__ import annotations

import json
from uuid import UUID
from app.db.connections import get_conn
from app.services.graph_memory import upsert_entity, add_edge

def run_memory_compression_job(external_user_id: str) -> int:
    """Consolidate multiple weak/archived episodes into a single summary episode."""
    job_id = None
    compressed_count = 0
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Fetch user ID
                cur.execute("SELECT id FROM users WHERE external_user_id = %s", (external_user_id,))
                user_row = cur.fetchone()
                if not user_row:
                    return 0
                user_id = user_row[0]

                # 2. Log job start
                cur.execute(
                    """
                    INSERT INTO background_jobs (user_id, job_type, status, started_at)
                    VALUES (%s, 'compression', 'running', NOW())
                    RETURNING id;
                    """,
                    (user_id,)
                )
                job_id = cur.fetchone()[0]
                conn.commit()

            with conn.cursor() as cur:
                # 3. Retrieve weak or archived episodes that are not already consolidated/summarized
                cur.execute(
                    """
                    SELECT id, content, COALESCE(summary, content), importance_score
                    FROM episodes
                    WHERE user_id = %s
                      AND consolidated_into_belief = FALSE
                      AND (strength_score < 0.25 OR is_archived = TRUE)
                      AND event_type != 'consolidated_summary'
                    ORDER BY created_at ASC;
                    """,
                    (user_id,)
                )
                ep_rows = cur.fetchall()
                
                if len(ep_rows) >= 3:
                    ep_ids = [r[0] for r in ep_rows]
                    ep_contents = [r[2] for r in ep_rows]
                    avg_importance = sum(float(r[3]) for r in ep_rows) / len(ep_rows)
                    
                    summary_text = "Consolidated memory summary: " + "; ".join(ep_contents)
                    
                    # 4. Insert consolidated summary episode
                    cur.execute(
                        """
                        INSERT INTO episodes (
                            user_id,
                            event_type,
                            content,
                            summary,
                            importance_score,
                            novelty_score,
                            recency_score,
                            strength_score,
                            decay_factor,
                            decay_score,
                            access_count,
                            quality_score,
                            last_accessed_at,
                            consolidated_into_belief,
                            consolidated_belief_id
                        )
                        VALUES (
                            %s, 'consolidated_summary', %s, %s,
                            %s, 0.8, 1.0, 0.8, 1.0, 1.0, 0, 0.90, NOW(),
                            FALSE, NULL
                        )
                        RETURNING id;
                        """,
                        (user_id, summary_text, summary_text, avg_importance)
                    )
                    new_ep_id = cur.fetchone()[0]

                    # 5. Archive and mark source episodes as consolidated
                    cur.execute(
                        """
                        UPDATE episodes
                        SET is_archived = TRUE,
                            consolidated_into_belief = TRUE,
                            last_consolidated_at = NOW()
                        WHERE id = ANY(%s::uuid[]);
                        """,
                        (ep_ids,)
                    )

                    # 6. Auto-populate graph memory relations for the consolidated summary
                    lowered_c = summary_text.lower()
                    topics = {
                        "backend": "technology",
                        "applied ai": "technology",
                        "frontend": "technology",
                        "ui": "technology",
                        "system design": "domain",
                        "ml interview": "domain",
                        "machine learning interview": "domain",
                        "python": "technology",
                        "fastapi": "technology"
                    }
                    for kw, category in topics.items():
                        if kw in lowered_c:
                            ent_name = kw
                            if kw == "machine learning interview":
                                ent_name = "ml interview"
                            ent_id = upsert_entity(cur, ent_name, category)
                            add_edge(cur, new_ep_id, "episode", ent_id, "entity", "mentions", 1.0)

                    compressed_count = len(ep_rows)

                    # 7. Update job status to completed
                    cur.execute(
                        """
                        UPDATE background_jobs
                        SET status = 'completed',
                            completed_at = NOW(),
                            payload = %s::jsonb
                        WHERE id = %s;
                        """,
                        (
                            json.dumps({
                                "compressed_count": compressed_count,
                                "new_episode_id": str(new_ep_id)
                            }),
                            job_id
                        )
                    )
                else:
                    # Not enough weak/archived episodes to compress
                    cur.execute(
                        """
                        UPDATE background_jobs
                        SET status = 'completed',
                            completed_at = NOW(),
                            payload = %s::jsonb
                        WHERE id = %s;
                        """,
                        (json.dumps({"compressed_count": 0}), job_id)
                    )
                conn.commit()

    except Exception as e:
        if job_id:
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE background_jobs
                            SET status = 'failed',
                                completed_at = NOW(),
                                error_message = %s
                            WHERE id = %s;
                            """,
                            (str(e), job_id)
                        )
                        conn.commit()
            except Exception:
                pass
        raise RuntimeError(f"Compression job failed: {str(e)}")

    return compressed_count
