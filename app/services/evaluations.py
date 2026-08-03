from __future__ import annotations

from typing import Dict, Any
from uuid import uuid4

from app.db.connections import get_conn


def get_observability_metrics(conn, external_user_id: str) -> Dict[str, Any]:
    """Retrieve high-level observability and consistency metrics for a user."""
    with conn.cursor() as cur:
        # Get user_id
        cur.execute("SELECT id FROM users WHERE external_user_id = %s", (external_user_id,))
        user_row = cur.fetchone()
        if not user_row:
            return {"error": "User not found"}
        user_id = user_row[0]

        # Counts
        cur.execute("SELECT COUNT(*)::int FROM episodes WHERE user_id = %s AND is_archived = FALSE", (user_id,))
        active_episodes = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*)::int FROM episodes WHERE user_id = %s AND is_archived = TRUE", (user_id,))
        archived_episodes = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*)::int FROM beliefs WHERE user_id = %s AND status = 'active'", (user_id,))
        active_beliefs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*)::int FROM beliefs WHERE user_id = %s AND status = 'conflicted'", (user_id,))
        conflicted_beliefs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*)::int FROM beliefs WHERE user_id = %s AND status = 'deprecated'", (user_id,))
        deprecated_beliefs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*)::int FROM background_jobs WHERE user_id = %s", (user_id,))
        background_jobs_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*)::int FROM gate_decision_logs WHERE user_id = %s", (user_id,))
        gate_decisions_count = cur.fetchone()[0]

        # Consistency metric
        total_beliefs = active_beliefs + conflicted_beliefs
        consistency_ratio = 1.0 if total_beliefs == 0 else active_beliefs / total_beliefs

        return {
            "user_id": external_user_id,
            "metrics": {
                "active_episodes": active_episodes,
                "archived_episodes": archived_episodes,
                "active_beliefs": active_beliefs,
                "conflicted_beliefs": conflicted_beliefs,
                "deprecated_beliefs": deprecated_beliefs,
                "background_jobs_run": background_jobs_count,
                "gate_decisions_logged": gate_decisions_count,
                "belief_consistency_ratio": float(consistency_ratio)
            }
        }


def run_eval_benchmark(external_user_id: str) -> Dict[str, Any]:
    """Execute a scripted 3-step conversation benchmark to evaluate memory gate, belief logic, and contradictions."""
    from main import store_turn_internal, create_episode_from_turn, consolidate_user_episodes
    conn = get_conn()
    try:
        # Create unique session for evaluation
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE external_user_id = %s", (external_user_id,))
            user_row = cur.fetchone()
            if not user_row:
                cur.execute("INSERT INTO users (external_user_id) VALUES (%s) RETURNING id", (external_user_id,))
                user_id = cur.fetchone()[0]
            else:
                user_id = user_row[0]
                # Clean up existing user data to start fresh benchmark
                cur.execute("DELETE FROM beliefs WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM revision_log WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM gate_decision_logs WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM episodes WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM conversation_turns WHERE user_id = %s", (user_id,))
            
            cur.execute("INSERT INTO sessions (user_id, title) VALUES (%s, 'Evaluation Benchmark') RETURNING id", (user_id,))
            session_id = cur.fetchone()[0]
            conn.commit()

        # Step 1: Chitchat greeting ("hi there, how's it going?")
        turn1_id = store_turn_internal(
            external_user_id=external_user_id,
            session_id=session_id,
            role="user",
            content="hi there, how's it going?",
            token_count=6,
            salience_score=0.2
        )[0]
        create_episode_from_turn(external_user_id, session_id, turn1_id)
        conn.commit()

        # Verify chitchat is correctly ignored
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM episodes WHERE user_id = %s", (user_id,))
            episodes_after_t1 = cur.fetchone()[0]
            chitchat_ignored = (episodes_after_t1 == 0)

        # Step 2: Preference expression ("I want to focus on backend engineering")
        turn2_id = store_turn_internal(
            external_user_id=external_user_id,
            session_id=session_id,
            role="user",
            content="I want to focus on backend engineering in my new role.",
            token_count=11,
            salience_score=0.9
        )[0]
        create_episode_from_turn(external_user_id, session_id, turn2_id)
        conn.commit()

        # Verify preference stored and belief created
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM episodes WHERE user_id = %s", (user_id,))
            episodes_after_t2 = cur.fetchone()[0]
            preference_stored = (episodes_after_t2 >= 1)

            cur.execute(
                "SELECT object_value, status FROM beliefs WHERE user_id = %s AND namespace = 'career'",
                (user_id,)
            )
            belief_row = cur.fetchone()
            belief_created = (
                belief_row is not None
                and belief_row[0] == "backend_and_applied_ai"
                and belief_row[1] == "active"
            )

        # Step 3: Contradiction expression ("Actually, I want to switch and focus on frontend UI")
        turn3_id = store_turn_internal(
            external_user_id=external_user_id,
            session_id=session_id,
            role="user",
            content="Actually, I want to switch and focus on frontend UI today.",
            token_count=11,
            salience_score=0.9
        )[0]
        create_episode_from_turn(external_user_id, session_id, turn3_id)
        conn.commit()

        # Verify contradiction handled (superseded)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT object_value, status FROM beliefs WHERE user_id = %s AND namespace = 'career' ORDER BY created_at DESC",
                (user_id,)
            )
            beliefs = cur.fetchall()
            contradiction_superseded = False
            if len(beliefs) >= 2:
                # The latest one is active, the old one deprecated
                latest = beliefs[0]
                oldest = beliefs[1]
                contradiction_superseded = (
                    latest[0] == "frontend_and_ui"
                    and latest[1] == "active"
                    and oldest[0] == "backend_and_applied_ai"
                    and oldest[1] == "deprecated"
                )

        # Calculate score
        passes = sum([chitchat_ignored, preference_stored, belief_created, contradiction_superseded])
        total = 4
        score = passes / total

        return {
            "user_id": external_user_id,
            "session_id": str(session_id),
            "benchmark_results": {
                "chitchat_ignored": chitchat_ignored,
                "preference_stored": preference_stored,
                "belief_created": belief_created,
                "contradiction_superseded": contradiction_superseded
            },
            "overall_score": float(score)
        }
    finally:
        conn.close()
