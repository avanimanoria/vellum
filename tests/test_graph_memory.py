import os
import unittest
from uuid import uuid4

from app.db.connections import get_conn
from app.services.graph_memory import retrieve_graph_memory, upsert_entity, add_edge
from main import create_episode_from_turn, consolidate_user_episodes

@unittest.skipUnless(
    os.getenv("VELLUM_TEST_DATABASE") == "1",
    "set VELLUM_TEST_DATABASE=1 to run PostgreSQL integration tests",
)
class GraphMemoryTests(unittest.TestCase):
    def setUp(self):
        self.conn = get_conn()
        with self.conn.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
            cur.execute("TRUNCATE beliefs CASCADE")
            cur.execute("TRUNCATE revision_log CASCADE")
            cur.execute("TRUNCATE entities CASCADE")
            cur.execute("TRUNCATE memory_edges CASCADE")
            self.conn.commit()

        # Create demo user and session
        self.external_user_id = f"graph-user-{uuid4()}"
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

    def _insert_turn(self, content, salience=0.8):
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

    def test_graph_population_on_episode_store(self):
        # Store a turn that contains matching keywords
        turn_id = self._insert_turn("I am learning system design and backend preferences today.")
        create_episode_from_turn(self.external_user_id, self.session_id, turn_id)
        self.conn.commit()

        with self.conn.cursor() as cur:
            # Verify entities were created
            cur.execute("SELECT name, category FROM entities ORDER BY name")
            entities = cur.fetchall()
            self.assertEqual(len(entities), 2)
            self.assertEqual(entities[0], ("backend", "technology"))
            self.assertEqual(entities[1], ("system design", "domain"))

            # Verify edges were created
            cur.execute(
                """
                SELECT source_type, target_type, edge_type
                FROM memory_edges
                """
            )
            edges = cur.fetchall()
            self.assertEqual(len(edges), 2)
            for src_t, tgt_t, edge_t in edges:
                self.assertEqual(src_t, "episode")
                self.assertEqual(tgt_t, "entity")
                self.assertEqual(edge_t, "mentions")

    def test_graph_population_on_consolidation(self):
        # 1. Manually insert an active belief
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO beliefs (
                    user_id, namespace, subject, predicate, object_value,
                    confidence, status, version
                ) VALUES (%s, 'career', %s, 'prefers_role_family', 'backend_and_applied_ai', 0.8, 'active', 1)
                RETURNING id
                """,
                (self.user_id, self.external_user_id),
            )
            belief_id = cur.fetchone()[0]
            self.conn.commit()

        # 2. Insert a reinforcing episode turn
        turn_id = self._insert_turn("User prefers backend engineering today.")
        create_episode_from_turn(self.external_user_id, self.session_id, turn_id)
        self.conn.commit()

        # 3. Consolidate (this may return 0 because memory gate consolidates inline)
        processed = consolidate_user_episodes(self.external_user_id)
        self.assertIn(processed, [0, 1])
        self.conn.commit()

        with self.conn.cursor() as cur:
            # Verify that a support/evidence edge was built between the belief and the episode in the graph
            cur.execute(
                """
                SELECT edge_type
                FROM memory_edges
                WHERE source_id = %s AND source_type = 'belief'
                """,
                (belief_id,)
            )
            edge = cur.fetchone()
            self.assertIsNotNone(edge)
            self.assertEqual(edge[0], "evidences")

    def test_spreading_activation_retrieval(self):
        # 1. Create entities and link them
        with self.conn.cursor() as cur:
            python_id = upsert_entity(cur, "python", "technology")
            fastapi_id = upsert_entity(cur, "fastapi", "technology")
            backend_id = upsert_entity(cur, "backend", "technology")
            
            # Link fastapi -> backend
            add_edge(cur, fastapi_id, "entity", backend_id, "entity", "related_to", 1.0)
            self.conn.commit()

        # 2. Insert turns that trigger episodes
        turn1_id = self._insert_turn("Learning python syntax.", salience=0.8)
        create_episode_from_turn(self.external_user_id, self.session_id, turn1_id)
        self.conn.commit()

        turn2_id = self._insert_turn("Built a backend API using fastapi.", salience=0.8)
        create_episode_from_turn(self.external_user_id, self.session_id, turn2_id)
        self.conn.commit()

        # 3. Verify retrieval by querying "python"
        res_python = retrieve_graph_memory(self.conn, external_user_id=self.external_user_id, query="python", max_hops=2)
        episodes_python = res_python["episodes"]
        self.assertEqual(len(episodes_python), 1)
        self.assertEqual(episodes_python[0]["content"], "Learning python syntax.")

        # 4. Verify multi-hop retrieval by querying "backend"
        # query "backend" -> finds entity "backend" -> hop 1 reaches entity "fastapi" -> hop 2 reaches Episode 2 ("Built a backend API using fastapi.")
        res_backend = retrieve_graph_memory(self.conn, external_user_id=self.external_user_id, query="backend", max_hops=2)
        episodes_backend = res_backend["episodes"]
        self.assertEqual(len(episodes_backend), 1) # Should find Episode 2 (via mentions backend)
        
        # Verify Episode 2 is ranked high
        contents = [ep["content"] for ep in episodes_backend]
        self.assertIn("Built a backend API using fastapi.", contents)
