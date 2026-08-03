"""PostgreSQL integration tests.

Run explicitly against an isolated local test database/session:
    $env:VELLUM_TEST_DATABASE='1'; python -m unittest tests.test_working_memory_postgres -v
"""

import os
import unittest
from uuid import uuid4

import psycopg
from unittest.mock import patch

from app.db.connections import get_conn
from app.db.working_memory import (
    apply_working_memory_patch,
    expire_stale_working_memory,
    get_working_memory,
)
from app.services.hybrid_retrieval import retrieve_hybrid_memory_pack


class _FixedEmbeddingProvider:
    def embed(self, texts):
        return [[0.001] * 1536 for _ in texts]


@unittest.skipUnless(
    os.getenv("VELLUM_TEST_DATABASE") == "1",
    "set VELLUM_TEST_DATABASE=1 to run PostgreSQL integration tests",
)
class WorkingMemoryPostgresTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE working_memory_states CASCADE")
            self.conn.commit()
        self.user_id = self._create_user_and_session("owner")
        self.other_user_id = self._create_user("other")
        self.turn_id = self._create_turn()

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _create_user(self, suffix):
        external_user_id = f"wm-integration-{suffix}-{uuid4()}"
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (external_user_id) VALUES (%s) RETURNING id",
                (external_user_id,),
            )
            user_id = cur.fetchone()[0]
        if suffix == "owner":
            self.external_user_id = external_user_id
        return user_id

    def _create_user_and_session(self, suffix):
        user_id = self._create_user(suffix)
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO sessions (user_id) VALUES (%s) RETURNING id", (user_id,))
            self.session_id = cur.fetchone()[0]
        return user_id

    def _create_turn(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversation_turns (session_id, user_id, role, content, turn_index)
                VALUES (%s, %s, 'assistant', 'integration test', 1)
                RETURNING id
                """,
                (self.session_id, self.user_id),
            )
            return cur.fetchone()[0]

    def _patch(self, goal="Implement expiry"):
        return {
            "active_goal": goal,
            "execution_state": "in_progress",
            "upsert_items": [{
                "kind": "next_action",
                "dedupe_key": "implement-expiry",
                "content": "Implement expiry worker",
                "priority": 100,
            }],
            "resolve_item_keys": [],
            "refresh_ttl": True,
        }

    def test_state_item_and_audit_are_persisted(self):
        state = apply_working_memory_patch(
            self.conn, user_id=self.user_id, session_id=self.session_id,
            source_turn_id=self.turn_id, patch=self._patch(), expected_version=0,
        )

        self.assertEqual(state["active_goal"], "Implement expiry")
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["items"][0]["dedupe_key"], "implement-expiry")
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM working_memory_revision_log WHERE working_memory_state_id = %s",
                (state["id"],),
            )
            self.assertEqual(cur.fetchone()[0], "created")

    def test_stale_patch_does_not_overwrite_newer_state(self):
        state = apply_working_memory_patch(
            self.conn, user_id=self.user_id, session_id=self.session_id,
            source_turn_id=self.turn_id, patch=self._patch(), expected_version=0,
        )
        conflict = apply_working_memory_patch(
            self.conn, user_id=self.user_id, session_id=self.session_id,
            source_turn_id=self.turn_id, patch=self._patch("Stale overwrite"), expected_version=1,
        )

        self.assertIsNone(conflict)
        fresh = get_working_memory(self.conn, user_id=self.user_id, session_id=self.session_id)
        self.assertEqual(fresh["active_goal"], state["active_goal"])

    def test_expiry_removes_state_from_retrieval(self):
        state = apply_working_memory_patch(
            self.conn, user_id=self.user_id, session_id=self.session_id,
            source_turn_id=self.turn_id, patch=self._patch(), expected_version=0,
        )
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE working_memory_states SET expires_at = NOW() - INTERVAL '1 second' WHERE id = %s",
                (state["id"],),
            )

        self.assertEqual(expire_stale_working_memory(self.conn), 1)
        self.assertIsNone(get_working_memory(self.conn, user_id=self.user_id, session_id=self.session_id))

    def test_database_rejects_mismatched_session_owner(self):
        with self.assertRaises(psycopg.IntegrityError):
            apply_working_memory_patch(
                self.conn, user_id=self.other_user_id, session_id=self.session_id,
                source_turn_id=self.turn_id, patch=self._patch(), expected_version=0,
            )

    def test_hybrid_retrieval_returns_semantic_episode(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO episodes (user_id, session_id, source_turn_id, event_type, content, summary,
                                      importance_score, strength_score, recency_score)
                VALUES (%s, %s, %s, 'project', 'Hybrid retrieval implementation',
                        'Implemented semantic hybrid retrieval for Vellum', .8, .8, 1.0)
                RETURNING id
                """,
                (self.user_id, self.session_id, self.turn_id),
            )
            episode_id = cur.fetchone()[0]
            vector = "[" + ",".join(["0.001"] * 1536) + "]"
            cur.execute(
                "INSERT INTO episode_embeddings (episode_id, embedding, source_hash) VALUES (%s, %s::vector, 'test')",
                (episode_id, vector),
            )

        with patch("app.services.hybrid_retrieval.get_embedding_provider", return_value=_FixedEmbeddingProvider()):
            pack = retrieve_hybrid_memory_pack(
                self.conn,
                external_user_id=self.external_user_id,
                query="How did we implement semantic retrieval?",
                session_id=self.session_id,
            )

        episodes = [item for item in pack["items"] if item["item_type"] == "episode"]
        self.assertEqual(pack["retrieval_mode"], "hybrid")
        self.assertTrue(episodes)
        self.assertIn("semantic hybrid retrieval", episodes[0]["label_2"])
        self.assertGreater(episodes[0]["retrieval"]["semantic_score"], .99)

    def test_sql_get_memory_pack_with_vector(self):
        # Insert a test episode with a vector
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO episodes (user_id, session_id, source_turn_id, event_type, content, summary,
                                      importance_score, strength_score, recency_score)
                VALUES (%s, %s, %s, 'test_type', 'Semantic SQL test content',
                        'Semantic SQL test summary', .7, .7, 1.0)
                RETURNING id
                """,
                (self.user_id, self.session_id, self.turn_id),
            )
            episode_id = cur.fetchone()[0]
            vector_val = [0.002] * 1536
            vector_str = "[" + ",".join([str(x) for x in vector_val]) + "]"
            cur.execute(
                "INSERT INTO episode_embeddings (episode_id, embedding, source_hash) VALUES (%s, %s::vector, 'sql_test')",
                (episode_id, vector_str),
            )

        # Call get_memory_pack via SQL with the query vector
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT item_type, item_id, label_1, label_2, score_1, score_2
                FROM get_memory_pack(%s, %s::vector)
                WHERE item_type = 'episode' AND label_1 = 'test_type';
                """,
                (self.external_user_id, vector_str),
            )
            rows = cur.fetchall()

        self.assertTrue(rows)
        # Verify the returned score is present and valid
        hybrid_score = rows[0][4]
        self.assertGreater(hybrid_score, 0.7)


if __name__ == "__main__":
    unittest.main()
