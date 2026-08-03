import json
from uuid import uuid4
from app.db.connections import get_conn
from app.services.graph_memory import upsert_entity, add_edge

def seed():
    print("Starting database seeding...")
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            # 1. Clean existing demo users
            cur.execute("DELETE FROM users WHERE external_user_id IN ('avani_researcher', 'bob_developer', 'charlie_designer')")
            conn.commit()

        with conn.cursor() as cur:
            # --- USER 1: AVANI ---
            cur.execute(
                """
                INSERT INTO users (external_user_id, display_name, email)
                VALUES ('avani_researcher', 'Avani Manoria', 'avani@vellum.ai')
                RETURNING id
                """
            )
            avani_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO sessions (user_id, title) VALUES (%s, 'Applied AI Engineering Design') RETURNING id",
                (avani_id,)
            )
            avani_sess_id = cur.fetchone()[0]

            # Episodes for Avani
            episodes_avani = [
                ("Avani focuses on building context retrieval systems for LLM agents.", 0.90, 0.90, "user_turn_signal"),
                ("Avani preferred using Python and FastAPI for her memory gate service.", 0.85, 0.80, "user_turn_signal"),
                ("Designing a relational memory graph database model on Postgres.", 0.88, 0.85, "user_turn_signal")
            ]
            avani_ep_ids = []
            for content, imp, strength, event_t in episodes_avani:
                cur.execute(
                    """
                    INSERT INTO episodes (
                        user_id, session_id, event_type, content, summary,
                        importance_score, novelty_score, recency_score, strength_score,
                        decay_factor, decay_score, access_count, quality_score, last_accessed_at,
                        is_archived, consolidated_into_belief
                    ) VALUES (%s, %s, %s, %s, %s, %s, 1.0, 1.0, %s, 1.0, 1.0, 3, 0.95, NOW(), FALSE, TRUE)
                    RETURNING id;
                    """,
                    (avani_id, avani_sess_id, event_t, content, content, imp, strength),
                )
                avani_ep_ids.append(cur.fetchone()[0])

            # Beliefs for Avani
            cur.execute(
                """
                INSERT INTO beliefs (
                    user_id, namespace, subject, predicate, object_value,
                    confidence, status, version, first_inferred_at, last_validated_at
                ) VALUES (%s, 'career', 'avani_researcher', 'prefers_role_family', 'backend_and_applied_ai', 0.95, 'active', 1, NOW(), NOW())
                RETURNING id
                """,
                (avani_id,),
            )
            avani_belief_id = cur.fetchone()[0]

            # Evidence linking
            for ep_id in avani_ep_ids:
                cur.execute(
                    "INSERT INTO belief_evidence (belief_id, episode_id, evidence_weight) VALUES (%s, %s, 0.90)",
                    (avani_belief_id, ep_id)
                )

            # Seed Graph Entities and Edges
            python_id = upsert_entity(cur, "python", "technology")
            fastapi_id = upsert_entity(cur, "fastapi", "technology")
            backend_id = upsert_entity(cur, "backend", "technology")
            system_id = upsert_entity(cur, "system design", "domain")

            add_edge(cur, avani_ep_ids[0], "episode", backend_id, "entity", "mentions", 1.0)
            add_edge(cur, avani_ep_ids[1], "episode", python_id, "entity", "mentions", 1.0)
            add_edge(cur, avani_ep_ids[1], "episode", fastapi_id, "entity", "mentions", 1.0)
            add_edge(cur, avani_ep_ids[2], "episode", system_id, "entity", "mentions", 1.0)
            add_edge(cur, avani_belief_id, "belief", avani_ep_ids[0], "episode", "evidences", 1.0)

            # --- USER 2: BOB ---
            cur.execute(
                """
                INSERT INTO users (external_user_id, display_name, email)
                VALUES ('bob_developer', 'Bob Builder', 'bob@builder.com')
                RETURNING id
                """
            )
            bob_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO sessions (user_id, title) VALUES (%s, 'Flutter Mobile Dashboards') RETURNING id",
                (bob_id,)
            )
            bob_sess_id = cur.fetchone()[0]

            episodes_bob = [
                ("Bob likes to develop hybrid mobile applications.", 0.80, 0.75, "user_turn_signal"),
                ("Bob preferred using Flutter and Dart for his client dashboards.", 0.85, 0.80, "user_turn_signal"),
                ("Working on implementing UI designs from Figma.", 0.70, 0.70, "user_turn_signal")
            ]
            bob_ep_ids = []
            for content, imp, strength, event_t in episodes_bob:
                cur.execute(
                    """
                    INSERT INTO episodes (
                        user_id, session_id, event_type, content, summary,
                        importance_score, novelty_score, recency_score, strength_score,
                        decay_factor, decay_score, access_count, quality_score, last_accessed_at,
                        is_archived, consolidated_into_belief
                    ) VALUES (%s, %s, %s, %s, %s, %s, 1.0, 1.0, %s, 1.0, 1.0, 2, 0.90, NOW(), FALSE, TRUE)
                    RETURNING id;
                    """,
                    (bob_id, bob_sess_id, event_t, content, content, imp, strength),
                )
                bob_ep_ids.append(cur.fetchone()[0])

            cur.execute(
                """
                INSERT INTO beliefs (
                    user_id, namespace, subject, predicate, object_value,
                    confidence, status, version, first_inferred_at, last_validated_at
                ) VALUES (%s, 'career', 'bob_developer', 'prefers_role_family', 'frontend_and_ui', 0.85, 'active', 1, NOW(), NOW())
                RETURNING id
                """,
                (bob_id,),
            )
            bob_belief_id = cur.fetchone()[0]

            for ep_id in bob_ep_ids:
                cur.execute(
                    "INSERT INTO belief_evidence (belief_id, episode_id, evidence_weight) VALUES (%s, %s, 0.80)",
                    (bob_belief_id, ep_id)
                )

            frontend_id = upsert_entity(cur, "frontend", "technology")
            ui_id = upsert_entity(cur, "ui", "technology")
            add_edge(cur, bob_ep_ids[0], "episode", frontend_id, "entity", "mentions", 1.0)
            add_edge(cur, bob_ep_ids[1], "episode", frontend_id, "entity", "mentions", 1.0)
            add_edge(cur, bob_ep_ids[2], "episode", ui_id, "entity", "mentions", 1.0)

            # --- USER 3: CHARLIE ---
            cur.execute(
                """
                INSERT INTO users (external_user_id, display_name, email)
                VALUES ('charlie_designer', 'Charlie Sketch', 'charlie@sketch.org')
                RETURNING id
                """
            )
            charlie_id = cur.fetchone()[0]

            cur.execute(
                "INSERT INTO sessions (user_id, title) VALUES (%s, 'Casual UX Ideation') RETURNING id",
                (charlie_id,)
            )
            charlie_sess_id = cur.fetchone()[0]

            # Casual turns
            cur.execute(
                """
                INSERT INTO conversation_turns (
                    session_id, user_id, role, content, token_count, turn_index,
                    salience_score, decay_score, last_accessed_at
                ) VALUES 
                (%s, %s, 'user', 'hello there assistant', 4, 1, 0.2, 1.0, NOW()),
                (%s, %s, 'assistant', 'Hello Charlie, how can I help you design today?', 9, 2, 0.5, 1.0, NOW())
                """,
                (charlie_sess_id, charlie_id, charlie_sess_id, charlie_id)
            )

            conn.commit()
            print("Database successfully seeded with Avani, Bob, and Charlie!")

    except Exception as e:
        conn.rollback()
        print(f"Error during seeding: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    seed()
