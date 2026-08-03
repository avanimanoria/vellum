"""Semantic candidate retrieval plus deterministic Vellum memory re-ranking."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.embeddings import EmbeddingUnavailable, embedding_literal, get_embedding_provider
from app.services.explanations import explain_memory_selection

EPISODE_CANDIDATES = 40
BELIEF_CANDIDATES = 30
MAX_EPISODES = 5
MAX_BELIEFS = 5


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value or 0.0)))


def _days_ago(value: datetime | None) -> float:
    if not value:
        return 3650.0
    return max(0.0, (datetime.now(timezone.utc) - value).total_seconds() / 86400)


def _recency(value: datetime | None, half_life_days: float) -> float:
    return math.exp(-math.log(2) * _days_ago(value) / half_life_days)


def score_episode(candidate: dict) -> dict:
    """0.50 semantic + 0.18 importance + 0.16 strength + 0.12 recency + 0.04 access - conflict."""
    semantic = _clamp(candidate["semantic_similarity"])
    importance = _clamp(candidate["importance_score"])
    strength = _clamp(candidate["strength_score"])
    recency = max(_clamp(candidate.get("recency_score", 0)), _recency(candidate["created_at"], 45))
    access = min(1.0, math.log1p(int(candidate.get("access_count") or 0)) / math.log(11))
    conflict_penalty = 0.35 if candidate.get("is_conflicted") else 0.0
    hybrid = _clamp(.50 * semantic + .18 * importance + .16 * strength + .12 * recency + .04 * access - conflict_penalty)
    return {"hybrid_score": hybrid, "semantic_score": semantic, "structured_score": _clamp(.36 * importance + .32 * strength + .24 * recency + .08 * access), "conflict_penalty": conflict_penalty}


def score_belief(candidate: dict) -> dict:
    """0.58 semantic + 0.20 confidence + 0.12 recency + 0.10 evidence - conflict."""
    semantic = _clamp(candidate["semantic_similarity"])
    confidence = _clamp(candidate["confidence"])
    recency = _recency(candidate["last_validated_at"], 120)
    evidence = min(1.0, int(candidate.get("evidence_count") or 0) / 3)
    conflict_penalty = .50 if candidate.get("is_conflicted") else 0.0
    hybrid = _clamp(.58 * semantic + .20 * confidence + .12 * recency + .10 * evidence - conflict_penalty)
    return {"hybrid_score": hybrid, "semantic_score": semantic, "structured_score": _clamp(.48 * confidence + .29 * recency + .23 * evidence), "conflict_penalty": conflict_penalty}


def _recent_turns(cur, user_id, session_id) -> list[dict]:
    if session_id:
        cur.execute(
            """
            SELECT id, role, content, salience_score, created_at
            FROM conversation_turns
            WHERE user_id = %s AND session_id = %s
            ORDER BY turn_index DESC LIMIT 8
            """, (user_id, session_id),
        )
    else:
        cur.execute(
            """
            SELECT ct.id, ct.role, ct.content, ct.salience_score, ct.created_at
            FROM conversation_turns ct
            JOIN sessions s ON s.id = ct.session_id
            WHERE ct.user_id = %s
            ORDER BY s.started_at DESC, ct.turn_index DESC LIMIT 8
            """, (user_id,),
        )
    return [{"item_type": "recent_turn", "item_id": str(r[0]), "label_1": r[1], "label_2": r[2], "score_1": r[3], "score_2": None, "ts": r[4].isoformat()} for r in cur.fetchall()]


def retrieve_hybrid_memory_pack(conn, *, external_user_id: str, query: str, session_id=None) -> dict:
    """Retrieve semantic candidates in PostgreSQL and rank them in one explicit formula."""
    vector = embedding_literal(get_embedding_provider().embed([query])[0])
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE external_user_id = %s", (external_user_id,))
        row = cur.fetchone()
        if not row:
            return {"user_id": external_user_id, "items": [], "retrieval_mode": "hybrid"}
        user_id = row[0]
        recent = _recent_turns(cur, user_id, session_id)

        cur.execute(
            """
            SELECT e.id, e.event_type, COALESCE(e.summary, e.content),
                   e.importance_score, e.strength_score, e.recency_score,
                   e.access_count, e.created_at,
                   (1 - (ee.embedding <=> %s::vector))::real AS semantic_similarity,
                   (e.metadata @> '{"conflicted": true}'::jsonb) AS is_conflicted
            FROM episode_embeddings ee
            JOIN episodes e ON e.id = ee.episode_id
            WHERE e.user_id = %s AND e.is_archived = FALSE
            ORDER BY ee.embedding <=> %s::vector
            LIMIT %s
            """, (vector, user_id, vector, EPISODE_CANDIDATES),
        )
        episodes = []
        for r in cur.fetchall():
            candidate = {"id": r[0], "event_type": r[1], "summary": r[2], "importance_score": r[3], "strength_score": r[4], "recency_score": r[5], "access_count": r[6], "created_at": r[7], "semantic_similarity": r[8], "is_conflicted": r[9]}
            episodes.append((candidate, score_episode(candidate)))

        cur.execute(
            """
            SELECT b.id, b.predicate, b.object_value, b.confidence, b.last_validated_at,
                   COUNT(be.episode_id)::int AS evidence_count,
                   (1 - (be2.embedding <=> %s::vector))::real AS semantic_similarity,
                   (b.status = 'conflicted' OR b.metadata @> '{"conflicted": true}'::jsonb) AS is_conflicted
            FROM belief_embeddings be2
            JOIN beliefs b ON b.id = be2.belief_id
            LEFT JOIN belief_evidence be ON be.belief_id = b.id
            WHERE b.user_id = %s AND b.status = 'active'
            GROUP BY b.id, be2.embedding
            ORDER BY be2.embedding <=> %s::vector
            LIMIT %s
            """, (vector, user_id, vector, BELIEF_CANDIDATES),
        )
        beliefs = []
        for r in cur.fetchall():
            candidate = {"id": r[0], "predicate": r[1], "object_value": r[2], "confidence": r[3], "last_validated_at": r[4], "evidence_count": r[5], "semantic_similarity": r[6], "is_conflicted": r[7]}
            beliefs.append((candidate, score_belief(candidate)))

    episodes.sort(key=lambda item: item[1]["hybrid_score"], reverse=True)
    beliefs.sort(key=lambda item: item[1]["hybrid_score"], reverse=True)
    items = recent
    explanations = []
    for candidate, scores in episodes[:MAX_EPISODES]:
        candidate["item_type"] = "episode"
        explanation = explain_memory_selection(candidate, scores)
        explanations.append(explanation)
        items.append({"item_type": "episode", "item_id": str(candidate["id"]), "label_1": candidate["event_type"], "label_2": candidate["summary"], "score_1": scores["hybrid_score"], "score_2": candidate["strength_score"], "ts": candidate["created_at"].isoformat(), "retrieval": scores})
    for candidate, scores in beliefs[:MAX_BELIEFS]:
        candidate["item_type"] = "belief"
        explanation = explain_memory_selection(candidate, scores)
        explanations.append(explanation)
        items.append({"item_type": "belief", "item_id": str(candidate["id"]), "label_1": candidate["predicate"], "label_2": candidate["object_value"], "score_1": scores["hybrid_score"], "score_2": candidate["confidence"], "ts": candidate["last_validated_at"].isoformat(), "retrieval": scores})
    return {"user_id": external_user_id, "items": items, "retrieval_mode": "hybrid", "explanations": explanations}
