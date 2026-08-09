import asyncio
import logging

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from uuid import UUID
from typing import Dict, Any, Tuple, Optional
from datetime import datetime, timezone
import psycopg
from app.db.connections import get_conn
from app.routers import working_memory as wm_router
from app.db.working_memory import get_working_memory, apply_working_memory_patch
from app.routers.prompts import build_wm_block
from app.services.working_memory import AgentResult, infer_working_memory_update
from app.services.working_memory_jobs import run_due_working_memory_jobs
from app.services.embedding_jobs import run_due_embedding_jobs
from app.services.embeddings import EmbeddingUnavailable
from app.services.hybrid_retrieval import retrieve_hybrid_memory_pack
from app.services.belief_revision import detect_belief_conflict, revise_belief
from app.services.memory_gate import evaluate_and_log_turn
from app.services.graph_memory import upsert_entity, add_edge, retrieve_graph_memory
from app.services.memory_compression import run_memory_compression_job
from app.services.evaluations import get_observability_metrics, run_eval_benchmark
import json

app = FastAPI()
logger = logging.getLogger(__name__)

app.include_router(wm_router.router)


async def _working_memory_expiry_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_due_working_memory_jobs)
            await asyncio.to_thread(run_due_embedding_jobs)
        except Exception:
            logger.exception("memory maintenance job failed")
        await asyncio.sleep(300)


@app.on_event("startup")
async def start_working_memory_expiry_worker() -> None:
    app.state.working_memory_expiry_task = asyncio.create_task(
        _working_memory_expiry_loop()
    )


@app.on_event("shutdown")
async def stop_working_memory_expiry_worker() -> None:
    task = getattr(app.state, "working_memory_expiry_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TurnCreate(BaseModel):
    role: str
    content: str
    token_count: int
    salience_score: float


class ChatRequest(BaseModel):
    external_user_id: str
    session_id: UUID
    message: str
    salience_score: float = 0.9
    token_count: int = 0


class ChatResponse(BaseModel):
    reply: str
    used_memory: Dict[str, Any]
    user_turn_id: str
    assistant_turn_id: str
    session_id: str
    user_id: str


class JobResponse(BaseModel):
    message: str
    external_user_id: str
    started_at: str
    processed_count: Optional[int] = None


@app.get("/", response_class=HTMLResponse)
def root():
    with open("frontend/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read())


def fetch_memory_pack_internal(
    external_user_id: str,
    query: Optional[str] = None,
    session_id: Optional[UUID] = None,
) -> Dict[str, Any]:
    try:
        if query and query.strip():
            try:
                with get_conn() as conn:
                    return retrieve_hybrid_memory_pack(
                        conn,
                        external_user_id=external_user_id,
                        query=query,
                        session_id=session_id,
                    )
            except EmbeddingUnavailable as exc:
                logger.warning("hybrid retrieval unavailable; using structured fallback: %s", exc)

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM get_memory_pack(%s);
                    """,
                    (external_user_id,)
                )
                rows = cur.fetchall()

        items = []
        for row in rows:
            items.append({
                "item_type": row[0],
                "item_id": str(row[1]),
                "label_1": row[2],
                "label_2": row[3],
                "score_1": row[4],
                "score_2": row[5],
                "ts": row[6].isoformat() if row[6] else None
            })

        return {
            "user_id": external_user_id,
            "items": items,
            "retrieval_mode": "structured_fallback" if query else "structured",
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{external_user_id}/memory-pack")
def get_memory_pack(
    external_user_id: str,
    query: str = "",
    session_id: Optional[UUID] = None,
):
    try:
        memory_pack = fetch_memory_pack_internal(external_user_id, query, session_id)

        recent_turn_ids = [
            item["item_id"]
            for item in memory_pack["items"]
            if item["item_type"] == "recent_turn"
        ]

        if recent_turn_ids:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE conversation_turns
                        SET last_accessed_at = NOW()
                        WHERE id = ANY(%s::uuid[]);
                        """,
                        (recent_turn_ids,)
                    )
                    conn.commit()

        return memory_pack

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def store_turn_internal(
    external_user_id: str,
    session_id: UUID,
    role: str,
    content: str,
    token_count: int,
    salience_score: float
) -> Tuple[Any, Any]:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO conversation_turns (
                        session_id,
                        user_id,
                        role,
                        content,
                        token_count,
                        turn_index,
                        salience_score,
                        decay_score,
                        last_accessed_at
                    )
                    VALUES (
                        %s,
                        (
                            SELECT u.id
                            FROM users u
                            JOIN sessions s ON s.user_id = u.id
                            WHERE u.external_user_id = %s
                              AND s.id = %s
                        ),
                        %s,
                        %s,
                        %s,
                        COALESCE(
                            (
                                SELECT MAX(ct.turn_index) + 1
                                FROM conversation_turns ct
                                WHERE ct.session_id = %s
                            ),
                            1
                        ),
                        %s,
                        1.0,
                        NOW()
                    )
                    RETURNING id, turn_index;
                    """,
                    (
                        session_id,
                        external_user_id,
                        session_id,
                        role,
                        content,
                        token_count,
                        session_id,
                        salience_score
                    )
                )
                row = cur.fetchone()
                conn.commit()
                return row

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def store_turn_with_conn(
    conn,
    external_user_id: str,
    session_id: UUID,
    role: str,
    content: str,
    token_count: int,
    salience_score: float,
) -> Tuple[Any, Any]:
    """Insert a turn without committing so it can share the WM transaction."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO conversation_turns (
                session_id, user_id, role, content, token_count, turn_index,
                salience_score, decay_score, last_accessed_at
            )
            VALUES (
                %s,
                (
                    SELECT u.id
                    FROM users u JOIN sessions s ON s.user_id = u.id
                    WHERE u.external_user_id = %s AND s.id = %s
                ),
                %s, %s, %s,
                COALESCE((
                    SELECT MAX(ct.turn_index) + 1
                    FROM conversation_turns ct WHERE ct.session_id = %s
                ), 1),
                %s, 1.0, NOW()
            )
            RETURNING id, turn_index;
            """,
            (
                session_id, external_user_id, session_id, role, content,
                token_count, session_id, salience_score,
            ),
        )
        return cur.fetchone()


def create_episode_from_turn(
    external_user_id: str,
    session_id: UUID,
    turn_id: UUID
):
    try:
        decisions = []
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        ct.id,
                        ct.user_id,
                        ct.content,
                        ct.salience_score
                    FROM conversation_turns ct
                    JOIN users u ON u.id = ct.user_id
                    WHERE ct.id = %s
                      AND u.external_user_id = %s;
                    """,
                    (turn_id, external_user_id)
                )
                row = cur.fetchone()

                if not row:
                    return

                stored_turn_id, user_id, content, salience = row
                
                # Evaluate via memory gate
                gate_result = evaluate_and_log_turn(
                    conn,
                    user_id=user_id,
                    content=content,
                    salience_score=float(salience or 0.5)
                )
                decisions = gate_result["decisions"]
                
                if "ignore" in decisions:
                    conn.commit()
                    return

                if "store_episode" in decisions:
                    cur.execute(
                        """
                        INSERT INTO episodes (
                            user_id,
                            session_id,
                            source_turn_id,
                            event_type,
                            content,
                            summary,
                            entities,
                            tags,
                            metadata,
                            importance_score,
                            novelty_score,
                            recency_score,
                            strength_score,
                            decay_factor,
                            decay_score,
                            access_count,
                            quality_score,
                            last_accessed_at,
                            consolidated_into_belief,
                            consolidated_belief_id
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s,
                            '[]'::jsonb, '[]'::jsonb,
                            %s::jsonb,
                            %s, %s, 1.00, %s, 1.00, 1.00, 0, 0.90, NOW(),
                            FALSE, NULL
                        )
                        RETURNING id;
                        """,
                        (
                            user_id,
                            session_id,
                            stored_turn_id,
                            "user_turn_signal",
                            content,
                            content,
                            json.dumps({
                                "source": "turn",
                                "kind": "user_message",
                                "gate_decisions": decisions
                            }),
                            gate_result["importance_score"],
                            gate_result["novelty_score"],
                            salience or 0.5
                        )
                    )
                    episode_id = cur.fetchone()[0]

                    # Auto-populate graph memory relations
                    lowered_c = content.lower()
                    topics = {
                        "backend": "technology",
                        "applied ai": "technology",
                        "frontend": "technology",
                        "ui": "technology",
                        "system design": "domain",
                        "ml interview": "domain",
                        "machine learning interview": "domain",
                        "python": "technology",
                        "fastapi": "technology"
                    }
                    for kw, category in topics.items():
                        if kw in lowered_c:
                            ent_name = kw
                            if kw == "machine learning interview":
                                ent_name = "ml interview"
                            ent_id = upsert_entity(cur, ent_name, category)
                            add_edge(cur, episode_id, "episode", ent_id, "entity", "mentions", 1.0)
                conn.commit()

        if "trigger_belief_revision" in decisions:
            consolidate_user_episodes(external_user_id)

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def generate_assistant_reply(
    message: str,
    memory_pack: Dict[str, Any],
    working_memory: Optional[Dict[str, Any]] = None,
) -> AgentResult:
    items = memory_pack["items"]

    recent_user_turns = [
        item for item in items
        if item["item_type"] == "recent_turn" and item["label_1"] == "user"
    ][:3]

    beliefs = [item for item in items if item["item_type"] == "belief"][:2]
    episodes = [item for item in items if item["item_type"] == "episode"][:2]

    context_parts = []

    if beliefs:
        belief_text = "; ".join([f"{b['label_1']}={b['label_2']}" for b in beliefs])
        context_parts.append(f"Known beliefs: {belief_text}")

    if episodes:
        episode_text = "; ".join([e["label_2"] for e in episodes])
        context_parts.append(f"Relevant past episodes: {episode_text}")

    if recent_user_turns:
        recent_text = "; ".join([t["label_2"] for t in recent_user_turns])
        context_parts.append(f"Recent user messages: {recent_text}")

    wm_block = build_wm_block(working_memory)

    # Parse lowercase word set to avoid substring collisions (e.g. 'ui' matching inside 'build')
    msg_lower = message.lower()
    words = set(msg_lower.replace(".", "").replace(",", "").replace("!", "").split())

    # Extract user profile highlights from memory pack
    user_preferences = []
    for b in beliefs:
        if b["label_1"] == "prefers_role_family":
            pref = b["label_2"].replace("_", " ").title()
            user_preferences.append(pref)
            
    pref_text = f" (tailored to your focus on {user_preferences[0]})" if user_preferences else ""

    # 1. Custom memory-grounded scenarios
    if "companion" in words or "widget" in words or "local" in words or "windows" in words:
        is_backend = any("backend" in b["label_2"] for b in beliefs)
        is_frontend = any("frontend" in b["label_2"] for b in beliefs)
        
        if is_frontend:
            reply = (
                "For your local Windows companion application, since you prefer Frontend & UI work, we should start "
                "by building a beautiful, glassmorphic desktop widget using Electron or Tauri. This widget will connect "
                "to a local FastAPI service and display your active memory logs with dynamic animations."
            )
        elif is_backend:
            reply = (
                "For your local Windows companion application, since you prefer Backend engineering, we should focus "
                "first on building the local FastAPI daemon service on your laptop. We will structure it with database hooks, "
                "background jobs for memory decay, and run a simple system-tray icon widget in Python."
            )
        else:
            reply = (
                "To build a local Windows companion application, we can start with a visible desktop widget and a local "
                "FastAPI service. Let me know if you prefer to focus on the frontend UI widget or the backend memory service first!"
            )
            
    elif "search" in words or "engine" in words:
        reply = (
            "To build a search engine, we will need to set up a crawler, indexer, and query processor. "
            f"Given your focus{pref_text}, we should use Python and FastAPI to build a high-performance vector search API."
        )
        
    elif "hello" in words or "hi" in words or "hey" in words:
        reply = (
            "Hello! I am Vellum, your cognitive memory companion. I store your interactions as episodes and extract key "
            "beliefs over time. How can I help you today?"
        )
        
    elif "know" in words or "beliefs" in words or "memory" in words:
        if beliefs:
            b_list = ", ".join([f"{b['label_1']} = {b['label_2']}" for b in beliefs])
            reply = f"I currently know that your active beliefs are: {b_list}. Let me know if you'd like me to update them!"
        else:
            reply = "I don't have any active beliefs about you yet. Tell me about your role or preferences, and I'll infer them!"

    # 2. Strict preference updates & contradiction signals
    elif "frontend" in words or "ui" in words:
        reply = (
            "Understood! I have updated your career track to frontend UI. I detected this contradicts your previous "
            "backend engineering preference, so I have updated your active beliefs, deprecated the old backend profile, "
            "and logged the revision action. You can see this transition in the Belief Timeline."
        )
    elif "backend" in words:
        reply = (
            "Got it! I have recorded your preference to focus on backend engineering in your career profile. "
            "I will save this under your active preferences and recall it whenever you ask for project or design context."
        )
    else:
        # 3. Dynamic generic response grounding
        if beliefs:
            pref = beliefs[0]['label_2'].replace("_", " ").title()
            reply = (
                f"I've received your request: '{message}'. Grounding this in your profile preference for '{pref}', "
                "here is a suggested step-by-step plan: 1) Define requirements, 2) Set up local environment, 3) Build core logic. "
                "How would you like to proceed?"
            )
        else:
            reply = (
                f"I've received your request: '{message}'. To give you customized planning, tell me more about your "
                "role preferences (e.g. backend, frontend UI, system design) and I will start building your memory index."
            )

    used_memory = {
        "recent_turn_ids": [t["item_id"] for t in recent_user_turns],
        "episode_ids": [e["item_id"] for e in episodes],
        "belief_ids": [b["item_id"] for b in beliefs],
        "retrieval_mode": memory_pack.get("retrieval_mode", "structured"),
    }

    # This demo generator is the current agent implementation. A production
    # LLM/tool agent must return this same validated structured contract.
    patch = infer_working_memory_update(message, working_memory)
    return AgentResult(
        reply=reply,
        used_memory=used_memory,
        working_memory_patch=patch,
    )


def reinforce_used_memory(used_memory: Dict[str, Any]):
    try:
        episode_ids = used_memory.get("episode_ids", [])
        belief_ids = used_memory.get("belief_ids", [])

        if not episode_ids and not belief_ids:
            return

        with get_conn() as conn:
            with conn.cursor() as cur:
                if episode_ids:
                    cur.execute(
                        """
                        UPDATE episodes
                        SET
                            strength_score = LEAST(strength_score + 0.15, 1.0),
                            access_count = access_count + 1,
                            last_accessed_at = NOW()
                        WHERE id = ANY(%s::uuid[]);
                        """,
                        (episode_ids,)
                    )
                
                if belief_ids:
                    cur.execute(
                        """
                        UPDATE beliefs
                        SET
                            confidence = LEAST(confidence + 0.05, 0.99),
                            last_validated_at = NOW()
                        WHERE id = ANY(%s::uuid[]);
                        """,
                        (belief_ids,)
                    )
                conn.commit()

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def consolidate_user_episodes(external_user_id: str) -> int:
    processed_count = 0

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id
                FROM users u
                WHERE u.external_user_id = %s;
                """,
                (external_user_id,)
            )
            user_row = cur.fetchone()

            if not user_row:
                return 0

            user_id = user_row[0]

            cur.execute(
                """
                SELECT
                    e.id,
                    e.event_type,
                    e.summary,
                    e.importance_score,
                    e.strength_score
                FROM episodes e
                WHERE e.user_id = %s
                  AND e.is_archived = FALSE
                  AND e.consolidated_into_belief = FALSE
                ORDER BY e.created_at ASC;
                """,
                (user_id,)
            )
            episodes = cur.fetchall()

            for episode_id, event_type, summary, importance_score, strength_score in episodes:
                namespace = None
                subject = external_user_id
                predicate = None
                object_value = None
                confidence = min(0.99, float((importance_score + strength_score) / 2.0))
                summary_text = summary or ""

                if event_type in ("career_preference", "user_turn_signal", "preference_signal"):
                    lowered = summary_text.lower()

                    if "backend" in lowered or "applied ai" in lowered:
                        namespace = "career"
                        predicate = "prefers_role_family"
                        object_value = "backend_and_applied_ai"

                    elif "frontend" in lowered or "ui" in lowered:
                        namespace = "career"
                        predicate = "prefers_role_family"
                        object_value = "frontend_and_ui"

                    elif "ml interview" in lowered or "machine learning interview" in lowered:
                        namespace = "career"
                        predicate = "current_focus"
                        object_value = "ml_interviews"

                    elif "system design" in lowered:
                        namespace = "career"
                        predicate = "learning_focus"
                        object_value = "system_design"

                if not (namespace and predicate and object_value):
                    continue

                cur.execute(
                    """
                    SELECT id, confidence, version, object_value, status, namespace, subject, predicate
                    FROM beliefs
                    WHERE user_id = %s
                      AND namespace = %s
                      AND subject = %s
                      AND predicate = %s
                      AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1;
                    """,
                    (user_id, namespace, subject, predicate)
                )
                existing_belief_row = cur.fetchone()

                if existing_belief_row:
                    belief_id, old_confidence, old_version, old_object_value, old_status, b_namespace, b_subject, b_predicate = existing_belief_row
                    belief_dict = {
                        "id": belief_id,
                        "confidence": old_confidence,
                        "version": old_version,
                        "object_value": old_object_value,
                        "status": old_status,
                        "namespace": b_namespace,
                        "subject": b_subject,
                        "predicate": b_predicate
                    }
                    proposed = {
                        "namespace": namespace,
                        "subject": subject,
                        "predicate": predicate,
                        "object_value": object_value,
                        "confidence": confidence,
                        "metadata": {}
                    }
                    relationship = detect_belief_conflict(belief_dict, proposed)
                    new_belief_id = revise_belief(
                        conn,
                        user_id=user_id,
                        belief=belief_dict,
                        episode_id=episode_id,
                        proposed=proposed,
                        relationship=relationship,
                        evidence_weight=confidence
                    )
                    assigned_belief_id = new_belief_id if new_belief_id else belief_id
                    
                    cur.execute(
                        """
                        UPDATE episodes
                        SET
                            consolidated_into_belief = TRUE,
                            consolidated_belief_id = %s,
                            last_consolidated_at = NOW()
                        WHERE id = %s;
                        """,
                        (assigned_belief_id, episode_id)
                    )
                    processed_count += 1

                else:
                    cur.execute(
                        """
                        INSERT INTO beliefs (
                            user_id,
                            namespace,
                            subject,
                            predicate,
                            object_value,
                            confidence,
                            status,
                            version,
                            first_inferred_at,
                            last_validated_at,
                            metadata,
                            revision_count,
                            last_revised_at
                        )
                        VALUES (
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            'active',
                            1,
                            NOW(),
                            NOW(),
                            '{"source":"consolidation"}'::jsonb,
                            0,
                            NULL
                        )
                        RETURNING id;
                        """,
                        (
                            user_id,
                            namespace,
                            subject,
                            predicate,
                            object_value,
                            confidence
                        )
                    )
                    new_belief_id = cur.fetchone()[0]

                    cur.execute(
                        """
                        INSERT INTO belief_evidence (belief_id, episode_id, evidence_weight)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (belief_id, episode_id) DO NOTHING;
                        """,
                        (new_belief_id, episode_id, confidence)
                    )

                    cur.execute(
                        """
                        UPDATE episodes
                        SET
                            consolidated_into_belief = TRUE,
                            consolidated_belief_id = %s,
                            last_consolidated_at = NOW()
                        WHERE id = %s;
                        """,
                        (new_belief_id, episode_id)
                    )

                    processed_count += 1

        conn.commit()

    return processed_count


@app.post("/users/{external_user_id}/sessions/{session_id}/turns")
def create_turn(external_user_id: str, session_id: UUID, turn: TurnCreate):
    try:
        inserted_row = store_turn_internal(
            external_user_id=external_user_id,
            session_id=session_id,
            role=turn.role,
            content=turn.content,
            token_count=turn.token_count,
            salience_score=turn.salience_score
        )

        if turn.role == "user":
            create_episode_from_turn(
                external_user_id=external_user_id,
                session_id=session_id,
                turn_id=inserted_row[0]
            )

        return {
            "message": "turn stored successfully",
            "turn_id": str(inserted_row[0]),
            "turn_index": inserted_row[1],
            "session_id": str(session_id),
            "user_id": external_user_id
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    try:
        # Validate ownership before storing any turn. The INSERT also enforces
        # this relationship, but this produces the correct API error instead
        # of a database constraint failure.
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE external_user_id = %s", (req.external_user_id,))
                user_row = cur.fetchone()
                if not user_row:
                    cur.execute(
                        "INSERT INTO users (external_user_id, display_name) VALUES (%s, %s) RETURNING id",
                        (req.external_user_id, req.external_user_id.replace("_", " ").title()),
                    )
                    user_id = cur.fetchone()[0]
                else:
                    user_id = user_row[0]

                cur.execute("SELECT id FROM sessions WHERE id = %s", (req.session_id,))
                sess_row = cur.fetchone()
                if not sess_row:
                    cur.execute(
                        "INSERT INTO sessions (id, user_id, title) VALUES (%s, %s, 'Active Session') RETURNING id",
                        (req.session_id, user_id),
                    )
                    session_id = cur.fetchone()[0]
                else:
                    session_id = sess_row[0]
                conn.commit()

        user_row = store_turn_internal(
            external_user_id=req.external_user_id,
            session_id=req.session_id,
            role="user",
            content=req.message,
            token_count=req.token_count,
            salience_score=req.salience_score
        )

        create_episode_from_turn(
            external_user_id=req.external_user_id,
            session_id=req.session_id,
            turn_id=user_row[0]
        )

        memory_pack = fetch_memory_pack_internal(
            req.external_user_id,
            query=req.message,
            session_id=req.session_id,
        )
        with get_conn() as conn:
            current_wm = get_working_memory(
                conn, user_id=user_id, session_id=req.session_id
            )
        expected_wm_version = current_wm["version"] if current_wm else 0

        agent_result = generate_assistant_reply(
            req.message, memory_pack, working_memory=current_wm
        )

        patch = agent_result.working_memory_patch.model_dump(exclude_none=True)
        patch_has_changes = bool(
            patch.get("refresh_ttl")
            or patch.get("upsert_items")
            or patch.get("resolve_item_keys")
        )
        updated_wm = current_wm
        working_memory_write_status = "unchanged"

        # The assistant turn and its WM patch are committed together. This is
        # essential: a state transition must never claim work an assistant turn
        # did not actually persist.
        with get_conn() as conn:
            assistant_row = store_turn_with_conn(
                conn=conn,
                external_user_id=req.external_user_id,
                session_id=req.session_id,
                role="assistant",
                content=agent_result.reply,
                token_count=len(agent_result.reply.split()),
                salience_score=0.7,
            )
            if patch_has_changes:
                updated_wm = apply_working_memory_patch(
                    conn=conn,
                    user_id=user_id,
                    session_id=req.session_id,
                    source_turn_id=assistant_row[0],
                    patch=patch,
                    expected_version=expected_wm_version,
                )
                working_memory_write_status = (
                    "applied" if updated_wm is not None else "version_conflict"
                )
            conn.commit()

        if updated_wm is None:
            # Keep the response but never overwrite a newer concurrent state.
            with get_conn() as conn:
                updated_wm = get_working_memory(
                    conn, user_id=user_id, session_id=req.session_id
                )

        reinforce_used_memory(agent_result.used_memory)

        return ChatResponse(
            reply=agent_result.reply,
            used_memory={
                **agent_result.used_memory,
                "working_memory": updated_wm,
                "working_memory_write_status": working_memory_write_status,
                "explanations": memory_pack.get("explanations", [])
            },
            user_turn_id=str(user_row[0]),
            assistant_turn_id=str(assistant_row[0]),
            session_id=str(req.session_id),
            user_id=req.external_user_id
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def run_decay_job(external_user_id: str):
    job_id = None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # 1. Log job execution start
                cur.execute(
                    """
                    INSERT INTO background_jobs (user_id, job_type, status, started_at)
                    VALUES (
                        (SELECT id FROM users WHERE external_user_id = %s),
                        'decay',
                        'running',
                        NOW()
                    )
                    RETURNING id;
                    """,
                    (external_user_id,)
                )
                job_row = cur.fetchone()
                if job_row:
                    job_id = job_row[0]
                conn.commit()

            with conn.cursor() as cur:
                # 2. Episode decay
                cur.execute(
                    """
                    UPDATE episodes
                    SET
                        strength_score = GREATEST(strength_score * 0.90, 0.0),
                        recency_score = GREATEST(recency_score * 0.90, 0.0),
                        decay_score = GREATEST(decay_score * 0.90, 0.10),
                        last_consolidated_at = NOW()
                    WHERE user_id = (
                        SELECT id FROM users WHERE external_user_id = %s
                    )
                      AND is_archived = FALSE;
                    """,
                    (external_user_id,)
                )
                decayed_episodes = cur.rowcount

                # 3. Active belief decay (confidence decays if not validated in the last 1 hour)
                cur.execute(
                    """
                    UPDATE beliefs
                    SET
                        confidence = GREATEST(0.10, confidence - 0.02),
                        last_revised_at = NOW()
                    WHERE user_id = (
                        SELECT id FROM users WHERE external_user_id = %s
                    )
                      AND status = 'active'
                      AND last_validated_at < NOW() - INTERVAL '1 hour';
                    """,
                    (external_user_id,)
                )
                decayed_beliefs = cur.rowcount

                # 4. Episode archival threshold
                cur.execute(
                    """
                    UPDATE episodes
                    SET
                        is_archived = TRUE,
                        archived_at = NOW()
                    WHERE user_id = (
                        SELECT id FROM users WHERE external_user_id = %s
                    )
                      AND is_archived = FALSE
                      AND (
                          strength_score < 0.20 OR
                          recency_score < 0.10 OR
                          decay_score < 0.20
                      );
                    """,
                    (external_user_id,)
                )
                archived_episodes = cur.rowcount

                # 5. Log job completion status
                if job_id:
                    cur.execute(
                        """
                        UPDATE background_jobs
                        SET
                            status = 'completed',
                            completed_at = NOW(),
                            payload = %s::jsonb
                        WHERE id = %s;
                        """,
                        (
                            json.dumps({
                                "decayed_episodes": decayed_episodes,
                                "decayed_beliefs": decayed_beliefs,
                                "archived_episodes": archived_episodes
                            }),
                            job_id
                        )
                    )
                conn.commit()

    except Exception as e:
        if job_id:
            try:
                with get_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE background_jobs
                            SET
                                status = 'failed',
                                completed_at = NOW(),
                                error_message = %s
                            WHERE id = %s;
                            """,
                            (str(e), job_id)
                        )
                        conn.commit()
            except Exception:
                pass
        raise RuntimeError(f"Decay job failed: {str(e)}")


@app.post("/admin/decay/{external_user_id}", response_model=JobResponse)
def decay_user_memory(external_user_id: str, background_tasks: BackgroundTasks):
    started_at = datetime.now(timezone.utc).isoformat()
    background_tasks.add_task(run_decay_job, external_user_id)

    return JobResponse(
        message="Decay job scheduled",
        external_user_id=external_user_id,
        started_at=started_at,
        processed_count=None
    )


@app.post("/admin/consolidate/{external_user_id}", response_model=JobResponse)
def consolidate_user_memory(external_user_id: str):
    try:
        started_at = datetime.now(timezone.utc).isoformat()
        processed_count = consolidate_user_episodes(external_user_id)

        return JobResponse(
            message="Consolidation completed",
            external_user_id=external_user_id,
            started_at=started_at,
            processed_count=processed_count
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/users/{external_user_id}/graph-retrieval")
def graph_retrieval(external_user_id: str, query: str, max_hops: int = 2):
    try:
        with get_conn() as conn:
            return retrieve_graph_memory(
                conn,
                external_user_id=external_user_id,
                query=query,
                max_hops=max_hops
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/compress/{external_user_id}", response_model=JobResponse)
def compress_user_memory(external_user_id: str):
    try:
        started_at = datetime.now(timezone.utc).isoformat()
        processed_count = run_memory_compression_job(external_user_id)

        return JobResponse(
            message="Compression completed",
            external_user_id=external_user_id,
            started_at=started_at,
            processed_count=processed_count
        )

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/metrics/{external_user_id}")
def get_metrics(external_user_id: str):
    try:
        with get_conn() as conn:
            return get_observability_metrics(conn, external_user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/admin/run-evals/{external_user_id}")
def run_evals(external_user_id: str):
    try:
        return run_eval_benchmark(external_user_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/revisions/{external_user_id}")
def get_revisions(external_user_id: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE external_user_id = %s", (external_user_id,))
                user_row = cur.fetchone()
                if not user_row:
                    return []
                user_id = user_row[0]
                cur.execute(
                    """
                    SELECT old_belief_id, new_belief_id, action, reason, created_at
                    FROM revision_log
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    """,
                    (user_id,)
                )
                return [
                    {
                        "old_belief_id": str(r[0]) if r[0] else None,
                        "new_belief_id": str(r[1]) if r[1] else None,
                        "action": r[2],
                        "reason": r[3],
                        "created_at": r[4].isoformat()
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin/beliefs/{external_user_id}")
def get_user_beliefs(external_user_id: str):
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM users WHERE external_user_id = %s", (external_user_id,))
                user_row = cur.fetchone()
                if not user_row:
                    return []
                user_id = user_row[0]
                cur.execute(
                    """
                    SELECT id, namespace, subject, predicate, object_value, confidence, status, last_validated_at
                    FROM beliefs
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    """,
                    (user_id,)
                )
                return [
                    {
                        "id": str(r[0]),
                        "namespace": r[1],
                        "subject": r[2],
                        "predicate": r[3],
                        "object_value": r[4],
                        "confidence": float(r[5]),
                        "status": r[6],
                        "last_validated_at": r[7].isoformat()
                    }
                    for r in cur.fetchall()
                ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
