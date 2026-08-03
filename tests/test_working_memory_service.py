import unittest

from app.routers.prompts import build_wm_block
from app.services.working_memory import infer_working_memory_update


class WorkingMemoryServiceTests(unittest.TestCase):
    def test_explicit_goal_creates_compact_goal_item(self):
        patch = infer_working_memory_update("Implement a working memory layer")

        self.assertEqual(patch["active_goal"], "Implement a working memory layer")
        self.assertEqual(patch["execution_state"], "in_progress")
        self.assertTrue(patch["refresh_ttl"])
        self.assertEqual(patch["upsert_items"][0]["kind"], "next_action")
        self.assertNotIn("latest_user_message", str(patch))

    def test_chitchat_does_not_refresh_or_create_state(self):
        patch = infer_working_memory_update("Thanks, that helps.")

        self.assertFalse(patch["refresh_ttl"])
        self.assertEqual(patch["upsert_items"], [])

    def test_question_is_tracked_without_overwriting_goal(self):
        current = {"active_goal": "Implement the working memory layer"}
        patch = infer_working_memory_update("How should expiry work?", current)

        self.assertNotIn("active_goal", patch)
        self.assertEqual(patch["execution_state"], "in_progress")
        self.assertEqual(patch["upsert_items"][0]["kind"], "open_question")

    def test_prompt_block_is_compact_and_uses_items(self):
        block = build_wm_block({
            "active_goal": "Implement working memory",
            "execution_state": "in_progress",
            "items": [
                {"kind": "next_action", "content": "Write the migration"},
                {"kind": "constraint", "content": "Do not embed session state"},
            ],
        })

        self.assertIn("Goal: Implement working memory", block)
        self.assertIn("[next_action] Write the migration", block)
        self.assertNotIn("state_slots", block)


if __name__ == "__main__":
    unittest.main()
