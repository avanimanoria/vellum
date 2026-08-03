import os
import unittest
import json
from uuid import uuid4

from app.db.connections import get_conn
from app.services.memory_gate import evaluate_and_log_turn
from main import create_episode_from_turn, consolidate_user_episodes

@unittest.skipUnless(
    os.getenv("VELLUM_TEST_DATABASE") == "1",
    "set VELLUM_TEST_DATABASE=1 to run PostgreSQL integration tests",
)
class MemoryGateTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
            cur.execute("TRUNCATE beliefs CASCADE")
            cur.execute("TRUNCATE revision_log CASCADE")
            cur.execute("TRUNCATE gate_decision_logs CASCADE")
            self.conn.commit()

        # Create demo user and session
        self.external_user_id = f"gate-user-{uuid4()}"
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

    def _insert_turn(self, content, salience=0.5):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversation_turns (
                    session_id, user_id, role, content, token_count,
                    turn_index, salience_score, decay_score, last_accessed_at
                ) VALUES (%s, %s, 'user', %s, 10, 1, %s, 1.0, NOW())
                RETURNING id
                """,
                (self.session_id, self.user_id, content, salience),
            )
            val = cur.fetchone()[0]
        self.conn.commit()
        return val

    def test_chitchat_ignored(self):
        # Insert a greeting turn (chitchat)
        turn_id = self._insert_turn("hello", salience=0.2)
        
        # Execute turn creation
        create_episode_from_turn(self.external_user_id, self.session_id, turn_id)
        
        # Start a new transaction snapshot
        self.conn.commit()

        with self.conn.cursor() as cur:
            # Verify no episode was created
            cur.execute("SELECT COUNT(*) FROM episodes WHERE user_id = %s", (self.user_id,))
            episode_count = cur.fetchone()[0]
            self.assertEqual(episode_count, 0)

            # Verify decision is logged as ignore
            cur.execute("SELECT decisions FROM gate_decision_logs WHERE user_id = %s", (self.user_id,))
            log = cur.fetchone()
            self.assertIsNotNone(log)
            decisions = log[0]
            self.assertIn("ignore", decisions)

    def test_store_episode_and_semantic_candidate(self):
        # Insert an informative turn
        turn_id = self._insert_turn("I am researching memory gates today for my project.", salience=0.7)

        # Execute
        create_episode_from_turn(self.external_user_id, self.session_id, turn_id)
        self.conn.commit()

        with self.conn.cursor() as cur:
            # Verify episode was created
            cur.execute("SELECT content, importance_score, novelty_score FROM episodes WHERE user_id = %s", (self.user_id,))
            ep_row = cur.fetchone()
            self.assertIsNotNone(ep_row)
            self.assertEqual(ep_row[0], "I am researching memory gates today for my project.")
            self.assertEqual(float(ep_row[1]), 0.7)
            self.assertEqual(float(ep_row[2]), 1.0) # No prior episodes, so novelty is 1.0

            # Verify decision log has correct categories
            cur.execute("SELECT decisions FROM gate_decision_logs WHERE user_id = %s", (self.user_id,))
            decisions = cur.fetchone()[0]
            self.assertIn("store_episode", decisions)
            self.assertIn("semantic_candidate", decisions)

    def test_important_preference_triggers_belief_revision(self):
        # Insert preference turn
        turn_id = self._insert_turn("I prefer backend and applied AI engineering.", salience=0.9)

        # Execute
        create_episode_from_turn(self.external_user_id, self.session_id, turn_id)
        self.conn.commit()

        with self.conn.cursor() as cur:
            # Verify episode was created
            cur.execute("SELECT COUNT(*) FROM episodes WHERE user_id = %s", (self.user_id,))
            self.assertEqual(cur.fetchone()[0], 1)

            # Verify belief was automatically created/updated
            cur.execute("SELECT predicate, object_value, status FROM beliefs WHERE user_id = %s", (self.user_id,))
            belief = cur.fetchone()
            self.assertIsNotNone(belief)
            self.assertEqual(belief[0], "prefers_role_family")
            self.assertEqual(belief[1], "backend_and_applied_ai")
            self.assertEqual(belief[2], "active")

            # Verify decision log contains trigger_belief_revision
            cur.execute("SELECT decisions FROM gate_decision_logs WHERE user_id = %s", (self.user_id,))
            decisions = cur.fetchone()[0]
            self.assertIn("trigger_belief_revision", decisions)

    def test_redundancy_lowers_novelty(self):
        # Insert first turn
        turn1_id = self._insert_turn("Working on machine learning models.", salience=0.6)
        create_episode_from_turn(self.external_user_id, self.session_id, turn1_id)
        self.conn.commit()

        # Insert highly redundant turn
        turn2_id = self._insert_turn("Working on machine learning models.", salience=0.6)
        create_episode_from_turn(self.external_user_id, self.session_id, turn2_id)
        self.conn.commit()

        with self.conn.cursor() as cur:
            # Verify only first turn got stored as an episode (second one is redundant and has novelty = 0.0)
            # Wait, does the second one get stored as an episode?
            # Let's check: decisions for second turn:
            # since content is identical, Jaccard overlap = 1.0, so novelty = 0.0.
            # Importance = 0.3 (no career keywords, salience 0.6 is not > 0.7).
            # Salience = 0.6, so store_episode will still be triggered since salience >= 0.5.
            # Let's verify that the second episode has novelty_score = 0.0
            cur.execute("SELECT novelty_score FROM episodes WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (self.user_id,))
            novelty = cur.fetchone()[0]
            self.assertEqual(float(novelty), 0.0)
