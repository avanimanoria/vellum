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
class WorkingMemoryRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self.conn = get_conn()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
            cur.execute("TRUNCATE working_memory_states CASCADE")
            self.conn.commit()

        # Create demo user and session
        self.external_user_id = f"test-routes-user-{uuid4()}"
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

    def test_get_working_memory_empty(self):
        response = self.client.get(
            f"/users/{self.external_user_id}/sessions/{self.session_id}/working-memory"
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "none")
        self.assertIsNone(data["working_memory"])

    def test_clear_working_memory_none(self):
        response = self.client.delete(
            f"/users/{self.external_user_id}/sessions/{self.session_id}/working-memory"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "none")

    def test_expire_endpoint(self):
        response = self.client.post("/admin/expire-working-memory")
        self.assertEqual(response.status_code, 200)
        self.assertIn("expired_count", response.json())
