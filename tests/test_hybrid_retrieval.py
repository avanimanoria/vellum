import unittest
from datetime import datetime, timezone

from app.services.embeddings import belief_embedding_text, episode_embedding_text, source_hash
from app.services.hybrid_retrieval import score_belief, score_episode


class HybridScoringTests(unittest.TestCase):
    def test_episode_semantic_match_outranks_structured_only_candidate(self):
        now = datetime.now(timezone.utc)
        semantic = score_episode({
            "semantic_similarity": .95, "importance_score": .45, "strength_score": .45,
            "recency_score": .50, "access_count": 1, "created_at": now, "is_conflicted": False,
        })
        structured = score_episode({
            "semantic_similarity": .10, "importance_score": .95, "strength_score": .95,
            "recency_score": .95, "access_count": 10, "created_at": now, "is_conflicted": False,
        })
        self.assertGreater(semantic["hybrid_score"], structured["hybrid_score"])

    def test_conflicted_belief_is_penalized(self):
        now = datetime.now(timezone.utc)
        base = {
            "semantic_similarity": .90, "confidence": .90,
            "last_validated_at": now, "evidence_count": 3,
        }
        active = score_belief({**base, "is_conflicted": False})
        conflicted = score_belief({**base, "is_conflicted": True})
        self.assertGreater(active["hybrid_score"], conflicted["hybrid_score"])
        self.assertEqual(conflicted["conflict_penalty"], .50)

    def test_embedding_text_is_canonical_and_hashable(self):
        episode = episode_embedding_text({"event_type": "project", "summary": "Built hybrid retrieval", "entities": ["Vellum"], "tags": ["postgres"]})
        belief = belief_embedding_text({"namespace": "career", "subject": "user", "predicate": "prefers", "object_value": "backend"})
        self.assertIn("Built hybrid retrieval", episode)
        self.assertIn("Predicate: prefers", belief)
        self.assertEqual(source_hash(episode), source_hash(episode))


if __name__ == "__main__":
    unittest.main()
