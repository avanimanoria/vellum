from __future__ import annotations

import json
from uuid import UUID
from typing import Any, Dict, Optional
from app.services.graph_memory import add_edge

def detect_belief_conflict(belief: dict, proposed: dict) -> str:
    """Detect the relationship between an existing belief and proposed new episodic data.

    Returns:
        "support": The proposed data validates/supports the existing belief.
        "contradict": The proposed data contradicts the existing belief.
        "negate": The proposed data explicitly negates the belief.
        "irrelevant": The proposed data is unrelated to this belief.
    """
    if (
        proposed.get("namespace") != belief.get("namespace")
        or proposed.get("subject") != belief.get("subject")
        or proposed.get("predicate") != belief.get("predicate")
    ):
        return "irrelevant"

    # Explicit negation check in proposed metadata or object value
    if proposed.get("metadata", {}).get("negated") or proposed.get("object_value") == "__negated__":
        return "negate"

    # Check if object values match
    if proposed.get("object_value") == belief.get("object_value"):
        return "support"
    else:
        return "contradict"


def revise_belief(
    conn,
    *,
    user_id: UUID,
    belief: dict,
    episode_id: UUID,
    proposed: dict,
    relationship: str,
    evidence_weight: float = 1.0
) -> Optional[UUID]:
    """Execute database mutations to revise a belief based on the relationship with new evidence.

    Returns:
        The UUID of any newly created belief (e.g. during supersede/split), or None.
    """
    belief_id = belief["id"]
    old_confidence = float(belief["confidence"])

    with conn.cursor() as cur:
        if relationship == "support":
            # Action: reinforce
            # C_new = min(0.99, C_old + (1.0 - C_old) * 0.1 * evidence_weight)
            new_confidence = min(0.99, old_confidence + (1.0 - old_confidence) * 0.1 * evidence_weight)
            
            cur.execute(
                """
                UPDATE beliefs
                SET confidence = %s,
                    last_validated_at = NOW(),
                    revision_count = COALESCE(revision_count, 0) + 1,
                    last_revised_at = NOW()
                WHERE id = %s
                """,
                (new_confidence, belief_id),
            )
            
            cur.execute(
                """
                INSERT INTO belief_evidence (belief_id, episode_id, evidence_weight)
                VALUES (%s, %s, %s)
                ON CONFLICT (belief_id, episode_id) DO UPDATE SET
                    evidence_weight = EXCLUDED.evidence_weight,
                    linked_at = NOW()
                """,
                (belief_id, episode_id, evidence_weight),
            )
            
            cur.execute(
                """
                INSERT INTO revision_log (user_id, old_belief_id, action, reason)
                VALUES (%s, %s, 'reinforce', %s)
                """,
                (user_id, belief_id, f"Reinforced belief with confidence {new_confidence:.4f}"),
            )
            add_edge(cur, belief_id, 'belief', episode_id, 'episode', 'evidences', evidence_weight)
            return None

        elif relationship == "negate":
            # Action: deprecate
            cur.execute(
                """
                UPDATE beliefs
                SET status = 'deprecated',
                    last_validated_at = NOW(),
                    revision_count = COALESCE(revision_count, 0) + 1,
                    last_revised_at = NOW()
                WHERE id = %s
                """,
                (belief_id,),
            )
            
            cur.execute(
                """
                INSERT INTO revision_log (user_id, old_belief_id, action, reason)
                VALUES (%s, %s, 'deprecate', %s)
                """,
                (user_id, belief_id, "Deprecated belief due to explicit user negation"),
            )
            return None

        elif relationship == "contradict":
            proposed_importance = proposed.get("confidence", 0.5)
            
            # Action selection rule:
            # If the proposed contradicting evidence has high confidence (e.g. >= 0.8) and is stronger
            # or comparable to the existing belief's confidence, we supersede.
            # Otherwise, we mark the belief as conflicted.
            if proposed_importance >= 0.80 and proposed_importance >= old_confidence - 0.15:
                # Action: supersede
                cur.execute(
                    """
                    UPDATE beliefs
                    SET status = 'deprecated',
                        last_validated_at = NOW(),
                        revision_count = COALESCE(revision_count, 0) + 1,
                        last_revised_at = NOW()
                    WHERE id = %s
                    """,
                    (belief_id,),
                )
                
                # Insert the new belief
                cur.execute(
                    """
                    INSERT INTO beliefs (
                        user_id, namespace, subject, predicate, object_value,
                        confidence, status, version, first_inferred_at, last_validated_at,
                        metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'active', %s, NOW(), NOW(), %s::jsonb)
                    RETURNING id
                    """,
                    (
                        user_id,
                        proposed["namespace"],
                        proposed["subject"],
                        proposed["predicate"],
                        proposed["object_value"],
                        proposed_importance,
                        belief.get("version", 1) + 1,
                        json.dumps({"source": "revision_supersede", "previous_belief_id": str(belief_id)}),
                    ),
                )
                new_belief_id = cur.fetchone()[0]
                
                # Link old belief to the new one
                cur.execute(
                    "UPDATE beliefs SET superseded_by = %s WHERE id = %s",
                    (new_belief_id, belief_id),
                )
                
                # Link evidence to new belief
                cur.execute(
                    """
                    INSERT INTO belief_evidence (belief_id, episode_id, evidence_weight)
                    VALUES (%s, %s, %s)
                    """,
                    (new_belief_id, episode_id, evidence_weight),
                )
                
                # Log supersede
                cur.execute(
                    """
                    INSERT INTO revision_log (user_id, old_belief_id, new_belief_id, action, reason)
                    VALUES (%s, %s, %s, 'supersede', %s)
                    """,
                    (
                        user_id,
                        belief_id,
                        new_belief_id,
                        f"Superseded object value '{belief['object_value']}' with '{proposed['object_value']}'",
                    ),
                )
                add_edge(cur, belief_id, 'belief', new_belief_id, 'belief', 'superseded_by', 1.0)
                add_edge(cur, new_belief_id, 'belief', episode_id, 'episode', 'evidences', evidence_weight)
                return new_belief_id
            
            else:
                # Action: conflict
                cur.execute(
                    """
                    UPDATE beliefs
                    SET status = 'conflicted',
                        last_validated_at = NOW(),
                        revision_count = COALESCE(revision_count, 0) + 1,
                        last_revised_at = NOW()
                    WHERE id = %s
                    """,
                    (belief_id,),
                )
                
                cur.execute(
                    """
                    INSERT INTO belief_evidence (belief_id, episode_id, evidence_weight)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (belief_id, episode_id) DO NOTHING
                    """,
                    (belief_id, episode_id, evidence_weight),
                )
                
                cur.execute(
                    """
                    INSERT INTO revision_log (user_id, old_belief_id, action, reason)
                    VALUES (%s, %s, 'conflict', %s)
                    """,
                    (
                        user_id,
                        belief_id,
                        f"Conflict detected with proposed value '{proposed['object_value']}'",
                    ),
                )
                add_edge(cur, belief_id, 'belief', episode_id, 'episode', 'evidences', evidence_weight)
                return None
    return None
