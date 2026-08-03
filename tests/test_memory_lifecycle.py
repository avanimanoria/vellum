import os
import unittest
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from app.db.connections import get_conn
from main import reinforce_used_memory, run_decay_job
from app.services.memory_compression import run_memory_compression_job

@unittest.skipUnless(
    os.getenv("VELLUM_TEST_DATABASE") == "1",
    "set VELLUM_TEST_DATABASE=1 to run PostgreSQL integration tests",
)
class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
            cur.execute("TRUNCATE beliefs CASCADE")
            cur.execute("TRUNCATE revision_log CASCADE")
            cur.execute("TRUNCATE background_jobs CASCADE")
            self.conn.commit()

        # Create demo user and session
        self.external_user_id = f"lifecycle-user-{uuid4()}"
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (external_user_id) VALUES (%s) RETURNING id",
                (self.external_user_id,),
            )
            self.user_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO sessions (user_id) VALUES (%s) RETURNING id",
                (self.user_id,),
            )
            self.session_id = cur.fetchone()[0]
            self.conn.commit()

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _insert_episode(self, content, strength=0.5, is_archived=False):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO episodes (
                    user_id, session_id, event_type, content, summary,
                    importance_score, novelty_score, recency_score, strength_score,
                    decay_factor, decay_score, access_count, quality_score, last_accessed_at,
                    is_archived, consolidated_into_belief
                ) VALUES (%s, %s, 'user_turn_signal', %s, %s, 0.5, 1.0, 1.0, %s, 1.0, 1.0, 0, 0.9, NOW(), %s, FALSE)
                RETURNING id
                """,
                (self.user_id, self.session_id, content, content, strength, is_archived),
            )
            val = cur.fetchone()[0]
        self.conn.commit()
        return val

    def _insert_belief(self, predicate, object_val, confidence=0.8, last_validated_hours_ago=0):
        validated_time = datetime.now(timezone.utc) - timedelta(hours=last_validated_hours_ago)
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO beliefs (
                    user_id, namespace, subject, predicate, object_value,
                    confidence, status, version, first_inferred_at, last_validated_at
                ) VALUES (%s, 'career', %s, %s, %s, %s, 'active', 1, NOW(), %s)
                RETURNING id
                """,
                (self.user_id, self.external_user_id, predicate, object_val, confidence, validated_time),
            )
            val = cur.fetchone()[0]
        self.conn.commit()
        return val

    def test_reinforcement_on_retrieval(self):
        # 1. Setup episode and belief
        episode_id = self._insert_episode("Researching databases.", strength=0.5)
        belief_id = self._insert_belief("prefers_role_family", "backend_and_applied_ai", confidence=0.6)

        # 2. Reinforce
        reinforce_used_memory({
            "episode_ids": [str(episode_id)],
            "belief_ids": [str(belief_id)]
        })
        self.conn.commit()

        # 3. Assert values increased
        with self.conn.cursor() as cur:
            cur.execute("SELECT strength_score, access_count FROM episodes WHERE id = %s", (episode_id,))
            ep = cur.fetchone()
            self.assertAlmostEqual(float(ep[0]), 0.65, places=4) # 0.50 + 0.15
            self.assertEqual(ep[1], 1)

            cur.execute("SELECT confidence FROM beliefs WHERE id = %s", (belief_id,))
            b = cur.fetchone()
            self.assertAlmostEqual(float(b[0]), 0.65, places=4) # 0.60 + 0.05

    def test_decay_job_updates_scores_and_observability(self):
        # 1. Setup memory nodes
        episode_id = self._insert_episode("Temporary task details.", strength=0.8)
        # Belief that is old enough to decay (validated 2 hours ago)
        belief_id_old = self._insert_belief("prefers_role_family", "backend", confidence=0.8, last_validated_hours_ago=2)
        # Belief that is fresh (validated 0 hours ago, should NOT decay)
        belief_id_fresh = self._insert_belief("prefers_role_family", "frontend", confidence=0.8, last_validated_hours_ago=0)

        # 2. Execute decay job
        run_decay_job(self.external_user_id)
        self.conn.commit()

        # 3. Assert decay applied correctly
        with self.conn.cursor() as cur:
            cur.execute("SELECT strength_score, recency_score FROM episodes WHERE id = %s", (episode_id,))
            ep = cur.fetchone()
            self.assertAlmostEqual(float(ep[0]), 0.72, places=4) # 0.8 * 0.9
            self.assertAlmostEqual(float(ep[1]), 0.90, places=4) # 1.0 * 0.9 (since recency starts at 1.0)

            cur.execute("SELECT confidence FROM beliefs WHERE id = %s", (belief_id_old,))
            b_old = cur.fetchone()
            self.assertAlmostEqual(float(b_old[0]), 0.78, places=4) # 0.80 - 0.02

            cur.execute("SELECT confidence FROM beliefs WHERE id = %s", (belief_id_fresh,))
            b_fresh = cur.fetchone()
            self.assertAlmostEqual(float(b_fresh[0]), 0.80, places=4) # Undecayed

            # 4. Check observability background_jobs log
            cur.execute("SELECT job_type, status, payload FROM background_jobs WHERE user_id = %s", (self.user_id,))
            job = cur.fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job[0], "decay")
            self.assertEqual(job[1], "completed")
            payload = job[2]
            self.assertEqual(payload["decayed_episodes"], 1)
            self.assertEqual(payload["decayed_beliefs"], 1)

    def test_compression_consolidates_weak_memories(self):
        # 1. Insert 3 weak/archived episodes
        self._insert_episode("Weak note 1.", strength=0.10)
        self._insert_episode("Weak note 2.", strength=0.12)
        self._insert_episode("Weak note 3.", strength=0.15)

        # 2. Run compression job
        count = run_memory_compression_job(self.external_user_id)
        self.assertEqual(count, 3)
        self.conn.commit()

        # 3. Assert compression outcomes
        with self.conn.cursor() as cur:
            # Source episodes should now be archived and marked as consolidated
            cur.execute("SELECT COUNT(*) FROM episodes WHERE user_id = %s AND consolidated_into_belief = TRUE AND is_archived = TRUE", (self.user_id,))
            self.assertEqual(cur.fetchone()[0], 3)

            # A new summary episode should exist
            cur.execute("SELECT content, event_type FROM episodes WHERE user_id = %s AND event_type = 'consolidated_summary'", (self.user_id,))
            summary_ep = cur.fetchone()
            self.assertIsNotNone(summary_ep)
            self.assertIn("Consolidated memory summary", summary_ep[0])

            # Check compression job log
            cur.execute("SELECT job_type, status, payload FROM background_jobs WHERE user_id = %s AND job_type = 'compression'", (self.user_id,))
            job = cur.fetchone()
            self.assertIsNotNone(job)
            self.assertEqual(job[1], "completed")
            self.assertEqual(job[2]["compressed_count"], 3)
