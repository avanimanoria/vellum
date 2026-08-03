-- Migration 004: Overload get_memory_pack to support hybrid retrieval with pgvector
-- Drop the old single-parameter function first to avoid conflicts.
DROP FUNCTION IF EXISTS get_memory_pack(VARCHAR);

CREATE OR REPLACE FUNCTION get_memory_pack(
  p_external_user_id VARCHAR,
  p_query_vector VECTOR(1536) DEFAULT NULL
)
RETURNS TABLE (
  item_type TEXT,
  item_id TEXT,
  label_1 VARCHAR,
  label_2 TEXT,
  score_1 REAL,
  score_2 REAL,
  ts TIMESTAMPTZ
)
LANGUAGE plpgsql
AS $$
DECLARE
  v_user_id UUID;
  v_session_id UUID;
BEGIN
  -- Get user_id
  SELECT id INTO v_user_id FROM users WHERE external_user_id = p_external_user_id;
  IF NOT FOUND THEN
    RETURN;
  END IF;

  -- Get latest session_id
  SELECT id INTO v_session_id FROM sessions WHERE user_id = v_user_id ORDER BY started_at DESC LIMIT 1;

  RETURN QUERY
  WITH recent_turns AS (
    SELECT
      ct.id,
      ct.role,
      ct.content,
      ct.salience_score,
      ct.created_at
    FROM conversation_turns ct
    WHERE ct.session_id = v_session_id
    ORDER BY ct.turn_index DESC
    LIMIT 8
  ),
  episode_candidates AS (
    SELECT 
      e.id, e.event_type, COALESCE(e.summary, e.content) as summary,
      e.importance_score, e.strength_score, e.recency_score, e.access_count, e.created_at,
      (e.metadata @> '{"conflicted": true}'::jsonb) AS is_conflicted,
      CASE WHEN p_query_vector IS NOT NULL THEN (1 - (ee.embedding <=> p_query_vector))::real ELSE 0.0::real END AS semantic_similarity
    FROM episodes e
    LEFT JOIN episode_embeddings ee ON ee.episode_id = e.id
    WHERE e.user_id = v_user_id AND e.is_archived = FALSE
    ORDER BY CASE WHEN p_query_vector IS NOT NULL THEN ee.embedding <=> p_query_vector ELSE NULL END
    LIMIT CASE WHEN p_query_vector IS NOT NULL THEN 40 ELSE 1000000 END
  ),
  ranked_episodes AS (
    SELECT 
      ec.id, ec.event_type, ec.summary, ec.created_at, ec.strength_score,
      CASE WHEN p_query_vector IS NOT NULL THEN
        -- Hybrid score: 0.50 semantic + 0.18 importance + 0.16 strength + 0.12 recency + 0.04 access - conflict
        GREATEST(0.0, LEAST(1.0, 
          0.50 * ec.semantic_similarity +
          0.18 * ec.importance_score +
          0.16 * ec.strength_score +
          0.12 * GREATEST(ec.recency_score, exp(-0.69314718 * (extract(epoch from (now() - ec.created_at)) / 86400.0) / 45.0)) +
          0.04 * LEAST(1.0, ln(ec.access_count + 1) / ln(11)) -
          CASE WHEN ec.is_conflicted THEN 0.35 ELSE 0.0 END
        ))::real
      ELSE
        -- Structured score fallback
        GREATEST(0.0, LEAST(1.0, 0.45 * ec.importance_score + 0.35 * ec.strength_score + 0.20 * ec.recency_score))::real
      END AS hybrid_score
    FROM episode_candidates ec
    ORDER BY hybrid_score DESC, ec.created_at DESC
    LIMIT 5
  ),
  belief_candidates AS (
    SELECT 
      b.id, b.predicate, b.object_value, b.confidence, b.last_validated_at, b.status, b.metadata,
      COUNT(be.episode_id)::int AS evidence_count,
      CASE WHEN p_query_vector IS NOT NULL THEN (1 - (be2.embedding <=> p_query_vector))::real ELSE 0.0::real END AS semantic_similarity
    FROM beliefs b
    LEFT JOIN belief_embeddings be2 ON be2.belief_id = b.id
    LEFT JOIN belief_evidence be ON be.belief_id = b.id
    WHERE b.user_id = v_user_id AND b.status = 'active'
    GROUP BY b.id, be2.embedding, b.predicate, b.object_value, b.confidence, b.last_validated_at, b.status, b.metadata
    ORDER BY CASE WHEN p_query_vector IS NOT NULL THEN be2.embedding <=> p_query_vector ELSE NULL END
    LIMIT CASE WHEN p_query_vector IS NOT NULL THEN 30 ELSE 1000000 END
  ),
  ranked_beliefs AS (
    SELECT 
      bc.id, bc.predicate, bc.object_value, bc.last_validated_at, bc.confidence,
      CASE WHEN p_query_vector IS NOT NULL THEN
        -- Hybrid score: 0.58 semantic + 0.20 confidence + 0.12 recency + 0.10 evidence - conflict
        GREATEST(0.0, LEAST(1.0,
          0.58 * bc.semantic_similarity +
          0.20 * bc.confidence +
          0.12 * exp(-0.69314718 * (extract(epoch from (now() - bc.last_validated_at)) / 86400.0) / 120.0) +
          0.10 * LEAST(1.0, bc.evidence_count::real / 3.0) -
          CASE WHEN bc.status = 'conflicted' OR bc.metadata @> '{"conflicted": true}'::jsonb THEN 0.50 ELSE 0.0 END
        ))::real
      ELSE
        -- Structured score fallback
        GREATEST(0.0, LEAST(1.0, 0.48 * bc.confidence + 0.29 * exp(-0.69314718 * (extract(epoch from (now() - bc.last_validated_at)) / 86400.0) / 120.0) + 0.23 * LEAST(1.0, bc.evidence_count::real / 3.0)))::real
      END AS hybrid_score
    FROM belief_candidates bc
    ORDER BY hybrid_score DESC, bc.last_validated_at DESC
    LIMIT 5
  )
  -- Union all three results
  SELECT 'recent_turn'::TEXT, rt.id::TEXT, rt.role::VARCHAR, rt.content, rt.salience_score::REAL, NULL::REAL, rt.created_at FROM recent_turns rt
  UNION ALL
  SELECT 'episode'::TEXT, re.id::TEXT, re.event_type::VARCHAR, re.summary, re.hybrid_score::REAL, re.strength_score::REAL, re.created_at FROM ranked_episodes re
  UNION ALL
  SELECT 'belief'::TEXT, rb.id::TEXT, rb.predicate::VARCHAR, rb.object_value, rb.hybrid_score::REAL, rb.confidence::REAL, rb.last_validated_at FROM ranked_beliefs rb;
END;
$$;
