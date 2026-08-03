from __future__ import annotations

import json
from uuid import UUID
from typing import Dict, Any, List, Set, Tuple

def upsert_entity(cur, name: str, category: str) -> UUID:
    """Insert or update a conceptual entity in the graph."""
    cur.execute(
        """
        INSERT INTO entities (name, category)
        VALUES (%s, %s)
        ON CONFLICT (name) DO UPDATE SET
            category = EXCLUDED.category
        RETURNING id;
        """,
        (name.lower().strip(), category)
    )
    return cur.fetchone()[0]


def add_edge(
    cur,
    source_id: UUID,
    source_type: str,
    target_id: UUID,
    target_type: str,
    edge_type: str,
    weight: float = 1.0
) -> None:
    """Insert or update a polymorphic edge between two memory nodes."""
    cur.execute(
        """
        INSERT INTO memory_edges (source_id, source_type, target_id, target_type, edge_type, weight)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (source_id, source_type, target_id, target_type, edge_type) DO UPDATE SET
            weight = EXCLUDED.weight
        """,
        (source_id, source_type, target_id, target_type, edge_type, weight)
    )


def retrieve_graph_memory(
    conn,
    *,
    external_user_id: str,
    query: str,
    max_hops: int = 2,
    decay_factor: float = 0.7
) -> Dict[str, Any]:
    """Retrieve episodic and belief memories using spreading activation on the graph.
    
    1. Finds seed entities in the query.
    2. Spreads activation weights through memory_edges up to max_hops.
    3. Ranks and returns the reached episodes and beliefs.
    """
    lowered_query = query.lower()
    
    # Step 1: Find seed entity nodes
    seeds: List[Tuple[UUID, str, float]] = [] # list of (node_id, node_type, activation)
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM entities")
        all_entities = cur.fetchall()
        for ent_id, ent_name in all_entities:
            if ent_name in lowered_query:
                seeds.append((ent_id, "entity", 1.0))

    if not seeds:
        return {"episodes": [], "beliefs": []}

    # Visited maps (node_id, node_type) -> max_activation_score
    visited: Dict[Tuple[UUID, str], float] = {}
    for node_id, node_type, score in seeds:
        visited[(node_id, node_type)] = score

    frontier = [s for s in seeds]

    # Step 2: Spreading activation loop
    for hop in range(max_hops):
        if not frontier:
            break
        
        next_frontier = []
        with conn.cursor() as cur:
            for active_id, active_type, parent_score in frontier:
                # Find all neighbors (outgoing and incoming edges)
                cur.execute(
                    """
                    SELECT target_id, target_type, edge_type, weight
                    FROM memory_edges
                    WHERE source_id = %s AND source_type = %s
                    UNION ALL
                    SELECT source_id, source_type, edge_type, weight
                    FROM memory_edges
                    WHERE target_id = %s AND target_type = %s
                    """,
                    (active_id, active_type, active_id, active_type)
                )
                neighbors = cur.fetchall()

                for n_id, n_type, edge_type, weight in neighbors:
                    # Calculate weight based on type and parent score
                    edge_wt = float(weight or 1.0)
                    new_score = parent_score * edge_wt * decay_factor
                    
                    key = (n_id, n_type)
                    if key not in visited or new_score > visited[key]:
                        visited[key] = new_score
                        next_frontier.append((n_id, n_type, new_score))
        
        frontier = next_frontier

    # Step 3: Fetch details for reached episodes & beliefs, enforcing user isolation
    scored_episodes = []
    scored_beliefs = []
    
    # Extract user ID
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE external_user_id = %s", (external_user_id,))
        user_row = cur.fetchone()
        if not user_row:
            return {"episodes": [], "beliefs": []}
        user_id = user_row[0]

        # Fetch episodes
        episode_nodes = [k[0] for k, v in visited.items() if k[1] == "episode"]
        if episode_nodes:
            cur.execute(
                """
                SELECT id, event_type, content, summary, importance_score, novelty_score, recency_score
                FROM episodes
                WHERE id = ANY(%s::uuid[]) AND user_id = %s AND is_archived = FALSE
                """,
                (episode_nodes, user_id)
            )
            episodes = cur.fetchall()
            for ep in episodes:
                score = visited[(ep[0], "episode")]
                scored_episodes.append({
                    "id": str(ep[0]),
                    "event_type": ep[1],
                    "content": ep[2],
                    "summary": ep[3],
                    "importance_score": float(ep[4]),
                    "novelty_score": float(ep[5]),
                    "recency_score": float(ep[6]),
                    "graph_relevance": score
                })
            # Sort by relevance
            scored_episodes.sort(key=lambda x: x["graph_relevance"], reverse=True)

        # Fetch beliefs
        belief_nodes = [k[0] for k, v in visited.items() if k[1] == "belief"]
        if belief_nodes:
            cur.execute(
                """
                SELECT id, namespace, subject, predicate, object_value, confidence, status
                FROM beliefs
                WHERE id = ANY(%s::uuid[]) AND user_id = %s AND status = 'active'
                """,
                (belief_nodes, user_id)
            )
            beliefs = cur.fetchall()
            for b in beliefs:
                score = visited[(b[0], "belief")]
                scored_beliefs.append({
                    "id": str(b[0]),
                    "namespace": b[1],
                    "subject": b[2],
                    "predicate": b[3],
                    "object_value": b[4],
                    "confidence": float(b[5]),
                    "status": b[6],
                    "graph_relevance": score
                })
            scored_beliefs.sort(key=lambda x: x["graph_relevance"], reverse=True)

    return {
        "episodes": scored_episodes,
        "beliefs": scored_beliefs
    }
