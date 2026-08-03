import os
import sys

# Ensure PYTHONPATH includes current directory
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.connections import get_conn
from main import store_turn_internal, create_episode_from_turn, consolidate_user_episodes

def run_flow():
    print("======================================================================")
    print("VELLUM MEMORY LIFECYCLE & REVISION DEMO FLOW")
    print("======================================================================\n")

    external_user_id = "avani_flow_test"
    conn = get_conn()

    try:
        # Step 0: Clean up and Setup
        print("[Step 0] Cleaning database for fresh flow test...")
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE external_user_id = %s", (external_user_id,))
            user_row = cur.fetchone()
            if user_row:
                user_id = user_row[0]
                cur.execute("DELETE FROM beliefs WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM revision_log WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM episodes WHERE user_id = %s", (user_id,))
                cur.execute("DELETE FROM conversation_turns WHERE user_id = %s", (user_id,))
            else:
                cur.execute("INSERT INTO users (external_user_id, display_name) VALUES (%s, %s) RETURNING id", (external_user_id, 'Avani Test'))
                user_id = cur.fetchone()[0]

            cur.execute("INSERT INTO sessions (user_id, title) VALUES (%s, 'Career Preference Simulation') RETURNING id", (user_id,))
            session_id = cur.fetchone()[0]
            conn.commit()
        print(f"Setup complete. User: {external_user_id}, Session: {session_id}\n")

        # Step 1: Store Initial Preference
        print("[Step 1] User states initial preference: 'I want to focus on backend engineering in my new role.'")
        turn1_id = store_turn_internal(
            external_user_id=external_user_id,
            session_id=session_id,
            role="user",
            content="I want to focus on backend engineering in my new role.",
            token_count=11,
            salience_score=0.9
        )[0]
        create_episode_from_turn(external_user_id, session_id, turn1_id)
        conn.commit()

        # Step 2: Consolidate turn into belief
        print("\n[Step 2] Memory consolidation triggers. Checking for new beliefs...")
        consolidate_user_episodes(external_user_id)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT namespace, predicate, object_value, confidence, status FROM beliefs WHERE user_id = %s", (user_id,))
            belief = cur.fetchone()
            if belief:
                print(f"-> INFERRED BELIEF:")
                print(f"   Namespace:   {belief[0]}")
                print(f"   Predicate:   {belief[1]}")
                print(f"   Value:       {belief[2]}")
                print(f"   Confidence:  {belief[3]:.2f}")
                print(f"   Status:      {belief[4]}\n")
            else:
                print("-> Error: Belief was not created.\n")

        # Step 3: User contradicts preference
        print("[Step 3] User expresses contradicting preference: 'Actually, I want to switch and focus on frontend UI today.'")
        turn2_id = store_turn_internal(
            external_user_id=external_user_id,
            session_id=session_id,
            role="user",
            content="Actually, I want to switch and focus on frontend UI today.",
            token_count=11,
            salience_score=0.9
        )[0]
        create_episode_from_turn(external_user_id, session_id, turn2_id)
        conn.commit()

        # Step 4: Revision check
        print("\n[Step 4] Memory consolidation triggers again. Detecting contradiction and updating beliefs...")
        consolidate_user_episodes(external_user_id)
        conn.commit()

        with conn.cursor() as cur:
            print("-> CURRENT BELIEF STATE IN DATABASE:")
            cur.execute("SELECT id, object_value, status, confidence FROM beliefs WHERE user_id = %s ORDER BY created_at ASC", (user_id,))
            beliefs = cur.fetchall()
            for b in beliefs:
                print(f"   ID: {b[0]} | Value: {b[1]:<25} | Status: {b[2]:<10} | Confidence: {b[3]:.2f}")

            # Print revision trail
            print("\n-> AUDIT REVISION TRAIL:")
            cur.execute("SELECT old_belief_id, new_belief_id, action, reason, created_at FROM revision_log WHERE user_id = %s", (user_id,))
            revisions = cur.fetchall()
            for r in revisions:
                print(f"   Action:   {r[2].upper()}")
                print(f"   Reason:   {r[3]}")
                print(f"   From:     {r[0]}")
                print(f"   To:       {r[1]}")
                print(f"   Logged:   {r[4].isoformat()}\n")

        print("======================================================================")
        print("FLOW VERIFICATION COMPLETED SUCCESSFULLY!")
        print("======================================================================")

    except Exception as e:
        conn.rollback()
        print(f"Error executing flow: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    run_flow()
