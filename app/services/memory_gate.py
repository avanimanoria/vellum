from __future__ import annotations

import json
from uuid import UUID
from typing import Dict, Any, List

def evaluate_and_log_turn(
    conn,
    *,
    user_id: UUID,
    content: str,
    salience_score: float
) -> Dict[str, Any]:
    """Evaluate an incoming conversation turn across cognitive dimensions.
    
    Categorizes the turn into one or more decisions (ignore, store_episode,
    update_working_memory, semantic_candidate, trigger_belief_revision) and
    logs the scores and decisions to the database.
    """
    lowered = content.lower().strip()
    words = set(lowered.split())
    
    # 1. Chitchat & Noise Check
    chitchat_keywords = {"hello", "hi", "hey", "thanks", "thank", "ok", "okay", "cool", "bye", "goodbye", "yes", "no"}
    is_pure_chitchat = len(words) <= 3 and words.issubset(chitchat_keywords)
    
    # 2. Novelty Score (via Jaccard overlap check against last 5 episodes)
    novelty_score = 1.0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT content
            FROM episodes
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 5
            """,
            (user_id,)
        )
        recent_episodes = cur.fetchall()
        
        if recent_episodes:
            overlaps = []
            for (ep_content,) in recent_episodes:
                ep_words = set(ep_content.lower().split()) if ep_content else set()
                if ep_words or words:
                    intersection = words.intersection(ep_words)
                    union = words.union(ep_words)
                    jaccard_overlap = len(intersection) / len(union) if union else 0.0
                    overlaps.append(jaccard_overlap)
            if overlaps:
                novelty_score = max(0.0, 1.0 - max(overlaps))

    # 3. Contradiction Potential
    contradiction_potential = 0.0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT predicate, object_value
            FROM beliefs
            WHERE user_id = %s AND status = 'active'
            """,
            (user_id,)
        )
        active_beliefs = cur.fetchall()
        
        for pred, obj_val in active_beliefs:
            # Check prefers_role_family contradictions
            if pred == "prefers_role_family":
                if obj_val == "backend_and_applied_ai" and ("frontend" in lowered or "ui" in lowered):
                    contradiction_potential = 0.90
                elif obj_val == "frontend_and_ui" and ("backend" in lowered or "applied ai" in lowered):
                    contradiction_potential = 0.90
            
            # Check current_focus contradictions
            elif pred == "current_focus":
                if obj_val == "ml_interviews" and "system design" in lowered:
                    contradiction_potential = 0.85
            
            # Check learning_focus contradictions
            elif pred == "learning_focus":
                if obj_val == "system_design" and ("ml interview" in lowered or "machine learning interview" in lowered):
                    contradiction_potential = 0.85

    # 4. Importance Score
    importance_score = 0.30
    preference_keywords = {"prefer", "focus", "learning", "want", "like", "focusing"}
    career_keywords = {"backend", "applied ai", "frontend", "ui", "ml interview", "system design"}
    
    if words.intersection(preference_keywords) or words.intersection(career_keywords):
        importance_score = 0.90
    elif salience_score > 0.7:
        importance_score = salience_score
    else:
        importance_score = max(0.30, salience_score)

    # 5. Utility Score
    utility_score = 0.40
    goal_keywords = {"goal", "task", "plan", "todo", "resolve", "resolved", "milestone", "focus is"}
    if words.intersection(goal_keywords):
        utility_score = 0.80

    # Determine Decisions
    decisions = []
    
    if is_pure_chitchat and importance_score < 0.5 and utility_score < 0.5:
        decisions.append("ignore")
    else:
        # Check store_episode
        if importance_score >= 0.5 or salience_score >= 0.5 or utility_score >= 0.5:
            decisions.append("store_episode")
            
        # Check update_working_memory
        if utility_score >= 0.7 or "goal is" in lowered or "working on" in lowered:
            decisions.append("update_working_memory")
            
        # Check semantic_candidate
        if "store_episode" in decisions and len(content) > 10 and not is_pure_chitchat:
            decisions.append("semantic_candidate")
            
        # Check trigger_belief_revision
        has_pref_keyword = any(k in lowered for k in ["prefer", "focus", "learning", "goal is"])
        has_signal_value = any(v in lowered for v in ["backend", "applied ai", "frontend", "ui", "system design", "ml interview"])
        if "store_episode" in decisions and (contradiction_potential >= 0.7 or (importance_score >= 0.8 and (has_pref_keyword or has_signal_value))):
            decisions.append("trigger_belief_revision")

    # If empty, default to ignore
    if not decisions:
        decisions.append("ignore")

    # Log to Database
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO gate_decision_logs (
                user_id, turn_content, salience_score, novelty_score,
                contradiction_potential, importance_score, utility_score, decisions
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                user_id,
                content,
                salience_score,
                novelty_score,
                contradiction_potential,
                importance_score,
                utility_score,
                json.dumps(decisions)
            )
        )
        
    return {
        "salience_score": salience_score,
        "novelty_score": novelty_score,
        "contradiction_potential": contradiction_potential,
        "importance_score": importance_score,
        "utility_score": utility_score,
        "decisions": decisions
    }
