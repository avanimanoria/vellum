from __future__ import annotations

from typing import Dict, Any

def explain_memory_selection(item: Dict[str, Any], score_details: Dict[str, Any]) -> Dict[str, Any]:
    """Generate structured reason codes and human-readable explanations for why a memory was selected."""
    reasons = []
    
    # Check semantic similarity
    sem_score = float(score_details.get("semantic_score", 0.0))
    if sem_score > 0.65:
        reasons.append("semantic_match")
    
    # Check importance/confidence
    imp_score = float(item.get("importance_score") or item.get("confidence") or 0.0)
    if imp_score > 0.75:
        reasons.append("high_salience")
        
    # Check strength/validation
    strength = float(item.get("strength_score", 0.0))
    if strength > 0.70:
        reasons.append("reinforced_recent")

    # Check graph activation score
    graph_relevance = float(item.get("graph_relevance", 0.0))
    if graph_relevance > 0.0:
        reasons.append("graph_multi_hop")

    # Conflict check
    if score_details.get("conflict_penalty", 0.0) > 0.0:
        reasons.append("penalized_conflict")

    # Human readable text
    item_type = item.get("item_type", "episode")
    if item_type == "episode":
        desc = (
            f"Retrieved past episode because it matched semantically (similarity: {sem_score:.2f}) "
            f"and has high strength/salience."
        )
    elif item_type == "belief":
        desc = (
            f"Retrieved active belief '{item.get('predicate')}' = '{item.get('object_value')}' "
            f"with confidence {item.get('confidence', 0.0):.2f} due to relevance."
        )
    else:
        desc = f"Retrieved {item_type} based on query context match."
        
    return {
        "item_id": str(item["id"]),
        "item_type": item_type,
        "selection_reasons": reasons,
        "human_explanation": desc
    }
