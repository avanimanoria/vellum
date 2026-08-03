import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from app.db.connections import get_conn
from app.services.hybrid_retrieval import retrieve_hybrid_memory_pack
from app.services.evaluations import get_observability_metrics, run_eval_benchmark

@unittest.skipUnless(
    os.getenv("VELLUM_TEST_DATABASE") == "1",
    "set VELLUM_TEST_DATABASE=1 to run PostgreSQL integration tests",
)
class ExplainabilityAndEvaluationsTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
            cur.execute("TRUNCATE beliefs CASCADE")
            cur.execute("TRUNCATE revision_log CASCADE")
            cur.execute("TRUNCATE background_jobs CASCADE")
            self.conn.commit()

        # Create demo user and session
        self.external_user_id = f"eval-user-{uuid4()}"
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

    @patch("app.services.embeddings.OpenAIEmbeddingProvider.embed")
    def test_explainable_retrieval_flow(self, mock_embed):
        mock_embed.return_value = [[0.0] * 1536]
        # 1. Insert a mock episode with high importance and strength
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO episodes (
                    user_id, session_id, event_type, content, summary,
                    importance_score, novelty_score, recency_score, strength_score,
                    decay_factor, decay_score, access_count, quality_score, last_accessed_at,
                    is_archived, consolidated_into_belief
                ) VALUES (%s, %s, 'user_turn_signal', %s, %s, 0.90, 1.0, 1.0, 0.85, 1.0, 1.0, 0, 0.9, NOW(), FALSE, FALSE)
                RETURNING id
                """,
                (self.user_id, self.session_id, "Highly salient system design preference.", "Highly salient system design preference."),
            )
            episode_id = cur.fetchone()[0]
            
            # Since retrieval requires embeddings, let's insert a dummy vector in episode_embeddings
            # Using 1536 dimensional vector (filled with 0.0)
            dummy_vector = "[" + ",".join(["0.0"] * 1536) + "]"
            cur.execute(
                "INSERT INTO episode_embeddings (episode_id, embedding) VALUES (%s, %s::vector)",
                (episode_id, dummy_vector)
            )
            self.conn.commit()

        # 2. Retrieve via hybrid pack (it uses a dummy provider in testing)
        res = retrieve_hybrid_memory_pack(
            self.conn,
            external_user_id=self.external_user_id,
            query="highly salient design details"
        )
        
        # 3. Assert explanations are present
        explanations = res.get("explanations", [])
        self.assertEqual(len(explanations), 1)
        exp = explanations[0]
        self.assertEqual(exp["item_id"], str(episode_id))
        self.assertEqual(exp["item_type"], "episode")
        
        # Reasons should contain high salience, reinforced recent, and semantic match
        reasons = exp["selection_reasons"]
        self.assertIn("high_salience", reasons)
        self.assertIn("reinforced_recent", reasons)
        self.assertIsNotNone(exp["human_explanation"])

    def test_automated_evaluation_run(self):
        # 1. Run the benchmark harness
        results = run_eval_benchmark(self.external_user_id)
        
        # 2. Assert benchmark outcomes
        self.assertEqual(results["overall_score"], 1.0)
        
        bench = results["benchmark_results"]
        self.assertTrue(bench["chitchat_ignored"])
        self.assertTrue(bench["preference_stored"])
        self.assertTrue(bench["belief_created"])
        self.assertTrue(bench["contradiction_superseded"])

        # 3. Check observability metrics
        metrics_res = get_observability_metrics(self.conn, self.external_user_id)
        metrics = metrics_res["metrics"]
        self.assertEqual(metrics["active_beliefs"], 1) # Frontend UI preference is active
        self.assertEqual(metrics["deprecated_beliefs"], 1) # Backend preference was superseded/deprecated
        self.assertEqual(metrics["belief_consistency_ratio"], 1.0) # Active/(Active+Conflicted)
