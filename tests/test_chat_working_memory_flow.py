import os
import unittest
from uuid import uuid4
from fastapi.testclient import TestClient

from app.db.connections import get_conn
from main import app

@unittest.skipUnless(
    os.getenv("VELLUM_TEST_DATABASE") == "1",
    "set VELLUM_TEST_DATABASE=1 to run PostgreSQL integration tests",
)
class ChatWorkingMemoryFlowTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.conn = get_conn()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
            cur.execute("TRUNCATE working_memory_states CASCADE")
            self.conn.commit()

        # Create demo user and session
        self.external_user_id = f"chat-flow-user-{uuid4()}"
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
        self.conn.close()

    def test_chat_flow_initializes_and_updates_working_memory(self):
        # 1. Send first message initiating a goal
        payload = {
            "external_user_id": self.external_user_id,
            "session_id": str(self.session_id),
            "message": "please help me build a search engine",
            "salience_score": 0.9,
            "token_count": 10
        }
        response = self.client.post("/chat", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        
        # Verify assistant response fields
        self.assertIn("reply", data)
        self.assertEqual(data["used_memory"]["working_memory_write_status"], "applied")
        
        # Check that working memory goal was initialized
        wm = data["used_memory"]["working_memory"]
        self.assertEqual(wm["active_goal"], "please help me build a search engine")
        self.assertEqual(wm["execution_state"], "in_progress")
        self.assertTrue(len(wm["items"]) > 0)
        self.assertEqual(wm["items"][0]["kind"], "next_action")

        # 2. Get working memory endpoint to confirm persistence
        get_response = self.client.get(
            f"/users/{self.external_user_id}/sessions/{self.session_id}/working-memory"
        )
        self.assertEqual(get_response.status_code, 200)
        get_data = get_response.json()
        self.assertEqual(get_data["status"], "active")
        self.assertEqual(get_data["working_memory"]["active_goal"], "please help me build a search engine")

        # 3. Complete the goal with a second turn
        payload2 = {
            "external_user_id": self.external_user_id,
            "session_id": str(self.session_id),
            "message": "I am done building the search engine",
            "salience_score": 0.9,
            "token_count": 10
        }
        response2 = self.client.post("/chat", json=payload2)
        self.assertEqual(response2.status_code, 200)
        data2 = response2.json()

        # The state should be 'done' now
        wm2 = data2["used_memory"]["working_memory"]
        self.assertEqual(wm2["execution_state"], "done")
