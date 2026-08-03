import os
import unittest
from uuid import uuid4

from app.db.connections import get_conn
from app.services.belief_revision import detect_belief_conflict, revise_belief
from main import consolidate_user_episodes

@unittest.skipUnless(
    os.getenv("VELLUM_TEST_DATABASE") == "1",
    "set VELLUM_TEST_DATABASE=1 to run PostgreSQL integration tests",
)
class BeliefRevisionTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
            cur.execute("TRUNCATE beliefs CASCADE")
            cur.execute("TRUNCATE revision_log CASCADE")
            self.conn.commit()

        # Create demo user and session
        self.external_user_id = f"belief-rev-user-{uuid4()}"
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

    def _insert_belief(self, predicate, object_value, confidence=0.8, status="active"):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO beliefs (
                    user_id, namespace, subject, predicate, object_value,
                    confidence, status, version
                ) VALUES (%s, 'career', %s, %s, %s, %s, %s, 1)
                RETURNING id
                """,
                (self.user_id, self.external_user_id, predicate, object_value, confidence, status),
            )
            val = cur.fetchone()[0]
        self.conn.commit()
        return val

    def _insert_episode(self, event_type, summary, importance=0.8, strength=0.8):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO episodes (
                    user_id, session_id, event_type, content, summary,
                    importance_score, strength_score, consolidated_into_belief
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, FALSE)
                RETURNING id
                """,
                (self.user_id, self.session_id, event_type, summary, summary, importance, strength),
            )
            val = cur.fetchone()[0]
        self.conn.commit()
        return val

    def test_belief_reinforcement(self):
        # 1. Create a belief
        belief_id = self._insert_belief("prefers_role_family", "backend_and_applied_ai", confidence=0.8)
        
        # 2. Insert a reinforcing episode (matching summary text for preferences)
        episode_id = self._insert_episode("career_preference", "User prefers backend and applied AI", importance=0.9, strength=0.9)

        # 3. Consolidate
        processed = consolidate_user_episodes(self.external_user_id)
        self.assertEqual(processed, 1)

        # 4. Commit to start a new transaction snapshot
        self.conn.commit()

        # 5. Verify belief confidence is increased and logged
        with self.conn.cursor() as cur:
            cur.execute("SELECT confidence, status FROM beliefs WHERE id = %s", (belief_id,))
            confidence, status = cur.fetchone()
            self.assertGreater(confidence, 0.8)
            self.assertEqual(status, "active")

            cur.execute(
                "SELECT action, old_belief_id FROM revision_log WHERE old_belief_id = %s",
                (belief_id,),
            )
            log = cur.fetchone()
            self.assertIsNotNone(log)
            self.assertEqual(log[0], "reinforce")

    def test_belief_supersede(self):
        # 1. Create a belief
        belief_id = self._insert_belief("prefers_role_family", "backend_and_applied_ai", confidence=0.7)

        # 2. Insert contradicting episode with high importance
        episode_id = self._insert_episode("career_preference", "User prefers frontend now", importance=0.95, strength=0.95)

        # 3. Consolidate
        processed = consolidate_user_episodes(self.external_user_id)
        self.assertEqual(processed, 1)

        # 4. Commit to start a new transaction snapshot
        self.conn.commit()

        # 5. Verify old belief is deprecated and new one is created
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, superseded_by FROM beliefs WHERE id = %s", (belief_id,))
            status, superseded_by = cur.fetchone()
            self.assertEqual(status, "deprecated")
            self.assertIsNotNone(superseded_by)

            cur.execute(
                "SELECT object_value, status, confidence FROM beliefs WHERE id = %s",
                (superseded_by,),
            )
            new_val, new_status, new_conf = cur.fetchone()
            self.assertEqual(new_val, "frontend_and_ui")
            self.assertEqual(new_status, "active")

            # Check revision log
            cur.execute(
                "SELECT action, old_belief_id, new_belief_id FROM revision_log WHERE action = 'supersede'"
            )
            log = cur.fetchone()
            self.assertIsNotNone(log)
            self.assertEqual(log[1], belief_id)
            self.assertEqual(log[2], superseded_by)

    def test_belief_conflict(self):
        # 1. Create a belief with high confidence
        belief_id = self._insert_belief("prefers_role_family", "backend_and_applied_ai", confidence=0.95)

        # 2. Insert contradicting episode with low importance/confidence (e.g. 0.40)
        episode_id = self._insert_episode("career_preference", "User is doing system design today", importance=0.3, strength=0.3)
        # Note: "system design" maps to predicate="learning_focus", object_value="system_design"
        # Since learning_focus is a different predicate from prefers_role_family, they are irrelevant and won't conflict.
        # Let's insert a contradicting prefers_role_family episode but with low importance.
        # "User prefers backend" -> wait, backend is same.
        # Let's manually trigger revise_belief with relationship = "contradict" and low weight to test the conflict action.
        
        belief_dict = {
            "id": belief_id,
            "confidence": 0.95,
            "version": 1,
            "object_value": "backend_and_applied_ai",
            "status": "active"
        }
        proposed = {
            "namespace": "career",
            "subject": self.external_user_id,
            "predicate": "prefers_role_family",
            "object_value": "frontend",
            "confidence": 0.45
        }
        
        # Call revise_belief directly to verify low-confidence contradict maps to conflict status
        revise_belief(
            self.conn,
            user_id=self.user_id,
            belief=belief_dict,
            episode_id=episode_id,
            proposed=proposed,
            relationship="contradict",
            evidence_weight=0.45
        )
        self.conn.commit()

        # Verify old belief is marked conflicted
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM beliefs WHERE id = %s", (belief_id,))
            status = cur.fetchone()[0]
            self.assertEqual(status, "conflicted")

            cur.execute("SELECT action FROM revision_log WHERE old_belief_id = %s", (belief_id,))
            action = cur.fetchone()[0]
            self.assertEqual(action, "conflict")

    def test_belief_negation(self):
        # 1. Create a belief
        belief_id = self._insert_belief("prefers_role_family", "backend_and_applied_ai", confidence=0.8)
        episode_id = self._insert_episode("career_preference", "Negated", importance=0.8, strength=0.8)

        # Proposed negation dict
        proposed = {
            "namespace": "career",
            "subject": self.external_user_id,
            "predicate": "prefers_role_family",
            "object_value": "__negated__",
            "confidence": 0.8,
            "metadata": {"negated": True}
        }

        # Check negation relationship
        rel = detect_belief_conflict({"namespace": "career", "subject": self.external_user_id, "predicate": "prefers_role_family", "object_value": "backend_and_applied_ai"}, proposed)
        self.assertEqual(rel, "negate")

        # Execute negation revision
        revise_belief(
            self.conn,
            user_id=self.user_id,
            belief={"id": belief_id, "confidence": 0.8, "version": 1},
            episode_id=episode_id,
            proposed=proposed,
            relationship="negate"
        )
        self.conn.commit()

        # Check status is deprecated
        with self.conn.cursor() as cur:
            cur.execute("SELECT status FROM beliefs WHERE id = %s", (belief_id,))
            status = cur.fetchone()[0]
            self.assertEqual(status, "deprecated")
