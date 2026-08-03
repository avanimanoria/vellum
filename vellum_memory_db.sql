SELECT name, default_version, installed_version
FROM pg_available_extensions
WHERE name = 'vector';
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";



CREATE TABLE users (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  external_user_id VARCHAR(128) UNIQUE NOT NULL,
  tenant_id UUID,
  display_name VARCHAR(150),
  email VARCHAR(255),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE TABLE sessions (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status VARCHAR(30) NOT NULL DEFAULT 'active',
  title VARCHAR(255),
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at TIMESTAMPTZ,
  last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  rolling_summary TEXT,
  rolling_summary_updated_at TIMESTAMPTZ
);


CREATE TABLE conversation_turns (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role VARCHAR(20) NOT NULL CHECK (role IN ('user','assistant','tool','system')),
  content TEXT NOT NULL,
  token_count INT,
  turn_index INT NOT NULL,
  salience_score REAL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE INDEX idx_conversation_turns_session_turn_index
ON conversation_turns(session_id, turn_index);


CREATE TABLE episodes (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  session_id UUID REFERENCES sessions(id) ON DELETE SET NULL,
  source_turn_id UUID REFERENCES conversation_turns(id) ON DELETE SET NULL,
  event_type VARCHAR(50) NOT NULL,
  content TEXT NOT NULL,
  summary TEXT,
  entities JSONB DEFAULT '[]'::jsonb,
  tags JSONB DEFAULT '[]'::jsonb,
  metadata JSONB DEFAULT '{}'::jsonb,
  importance_score REAL NOT NULL DEFAULT 0,
  novelty_score REAL NOT NULL DEFAULT 0,
  recency_score REAL NOT NULL DEFAULT 1,
  strength_score REAL NOT NULL DEFAULT 0,
  decay_factor REAL NOT NULL DEFAULT 1,
  access_count INT NOT NULL DEFAULT 0,
  last_accessed_at TIMESTAMPTZ,
  quality_score REAL,
  is_archived BOOLEAN NOT NULL DEFAULT FALSE,
  archived_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE INDEX idx_episodes_user_created_at
ON episodes(user_id, created_at DESC);

CREATE INDEX idx_episodes_user_archived
ON episodes(user_id, is_archived);


CREATE TABLE episode_embeddings (
  episode_id UUID PRIMARY KEY REFERENCES episodes(id) ON DELETE CASCADE,
  embedding VECTOR(1536) NOT NULL
);


CREATE TABLE beliefs (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  namespace VARCHAR(50) NOT NULL,
  subject VARCHAR(255) NOT NULL,
  predicate VARCHAR(255) NOT NULL,
  object_value TEXT NOT NULL,
  confidence REAL NOT NULL,
  status VARCHAR(30) NOT NULL CHECK (status IN ('active','stale','deprecated','conflicted')),
  version INT NOT NULL DEFAULT 1,
  first_inferred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_validated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ,
  superseded_by UUID REFERENCES beliefs(id) ON DELETE SET NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


CREATE INDEX idx_beliefs_user_namespace_status
ON beliefs(user_id, namespace, status);



CREATE TABLE belief_evidence (
  belief_id UUID NOT NULL REFERENCES beliefs(id) ON DELETE CASCADE,
  episode_id UUID NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
  evidence_weight REAL DEFAULT 1,
  linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (belief_id, episode_id)
);


CREATE TABLE belief_embeddings (
  belief_id UUID PRIMARY KEY REFERENCES beliefs(id) ON DELETE CASCADE,
  embedding VECTOR(1536) NOT NULL
);


CREATE TABLE revision_log (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  old_belief_id UUID REFERENCES beliefs(id) ON DELETE SET NULL,
  new_belief_id UUID REFERENCES beliefs(id) ON DELETE SET NULL,
  action VARCHAR(30) NOT NULL CHECK (action IN ('reinforce','supersede','deprecate','conflict','split')),
  reason TEXT,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_revision_log_user_created_at
ON revision_log(user_id, created_at DESC);

CREATE TABLE background_jobs (
  id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
  user_id UUID REFERENCES users(id) ON DELETE CASCADE,
  job_type VARCHAR(50) NOT NULL,
  status VARCHAR(30) NOT NULL CHECK (status IN ('queued','running','completed','failed')),
  payload JSONB DEFAULT '{}'::jsonb,
  error_message TEXT,
  scheduled_at TIMESTAMPTZ,
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- Example only, do not run yet
-- CREATE INDEX ON episode_embeddings USING hnsw (embedding vector_cosine_ops);
-- CREATE INDEX ON belief_embeddings USING hnsw (embedding vector_cosine_ops);


SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_type = 'BASE TABLE'
ORDER BY table_name;

INSERT INTO users (
  external_user_id,
  tenant_id,
  display_name,
  email
)
VALUES (
  'user_demo_001',
  NULL,
  'Avani',
  'avani@example.com'
)
RETURNING *;






INSERT INTO sessions (
  user_id,
  status,
  title
)
SELECT
  id,
  'active',
  'Career preference memory test'
FROM users
WHERE external_user_id = 'user_demo_001'
RETURNING *;




INSERT INTO conversation_turns (
  session_id,
  user_id,
  role,
  content,
  token_count,
  turn_index,
  salience_score
)
SELECT
  s.id,
  u.id,
  v.role,
  v.content,
  v.token_count,
  v.turn_index,
  v.salience_score
FROM users u
JOIN sessions s ON s.user_id = u.id
JOIN (
  VALUES
    ('user', 'I am preparing for ML interviews and want help making a study plan.', 14, 1, 0.90),
    ('assistant', 'Got it. I can help you build an ML interview preparation roadmap.', 13, 2, 0.60),
    ('user', 'I am more interested in backend and applied AI roles than pure research.', 14, 3, 0.95)
) AS v(role, content, token_count, turn_index, salience_score) ON TRUE
WHERE u.external_user_id = 'user_demo_001'
  AND s.id = (
    SELECT id
    FROM sessions
    WHERE user_id = u.id
    ORDER BY started_at DESC
    LIMIT 1
  )
RETURNING *;








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
  access_count,
  quality_score
)
SELECT
  u.id,
  s.id,
  ct.id,
  x.event_type,
  x.content,
  x.summary,
  x.entities::jsonb,
  x.tags::jsonb,
  x.metadata::jsonb,
  x.importance_score,
  x.novelty_score,
  x.recency_score,
  x.strength_score,
  x.decay_factor,
  x.access_count,
  x.quality_score
FROM users u
JOIN sessions s ON s.user_id = u.id
JOIN conversation_turns ct ON ct.session_id = s.id AND ct.user_id = u.id
JOIN (
  VALUES
    (
      1,
      'preference_signal',
      'User is preparing for ML interviews.',
      'Preparation focus is ML interviews.',
      '["ML interviews"]',
      '["career","ml","interview"]',
      '{"source":"seed","kind":"goal"}',
      0.90, 0.80, 1.00, 0.70, 1.00, 0, 0.95
    ),
    (
      3,
      'career_preference',
      'User prefers backend and applied AI roles over pure research.',
      'Career preference leans toward backend and applied AI.',
      '["backend","applied AI","research"]',
      '["career","preference","backend","applied-ai"]',
      '{"source":"seed","kind":"preference"}',
      0.97, 0.88, 1.00, 0.85, 1.00, 0, 0.98
    )
) AS x(
  turn_index,
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
  access_count,
  quality_score
) ON x.turn_index = ct.turn_index
WHERE u.external_user_id = 'user_demo_001'
  AND s.id = (
    SELECT id
    FROM sessions
    WHERE user_id = u.id
    ORDER BY started_at DESC
    LIMIT 1
  )
RETURNING *;








INSERT INTO beliefs (
  user_id,
  namespace,
  subject,
  predicate,
  object_value,
  confidence,
  status,
  version,
  metadata
)
SELECT
  id,
  'career',
  'user_demo_001',
  'prefers_role_family',
  'backend_and_applied_ai',
  0.92,
  'active',
  1,
  '{"source":"seed","inference":"from episodes"}'::jsonb
FROM users
WHERE external_user_id = 'user_demo_001'
RETURNING *;







INSERT INTO belief_evidence (
  belief_id,
  episode_id,
  evidence_weight
)
SELECT
  b.id,
  e.id,
  CASE
    WHEN e.event_type = 'career_preference' THEN 1.0
    ELSE 0.7
  END
FROM beliefs b
JOIN users u ON u.id = b.user_id
JOIN episodes e ON e.user_id = u.id
WHERE u.external_user_id = 'user_demo_001'
  AND b.namespace = 'career'
  AND b.subject = 'user_demo_001'
  AND b.predicate = 'prefers_role_family'
RETURNING *;






INSERT INTO belief_embeddings (belief_id, embedding)
SELECT
  b.id,
  ('[' || array_to_string(array_fill(0.001::float8, ARRAY[1536]), ',') || ']')::vector
FROM beliefs b
JOIN users u ON u.id = b.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND b.namespace = 'career'
  AND b.subject = 'user_demo_001'
  AND b.predicate = 'prefers_role_family'
RETURNING belief_id;





INSERT INTO episode_embeddings (episode_id, embedding)
SELECT
  e.id,
  ('[' || array_to_string(array_fill(0.001::float8, ARRAY[1536]), ',') || ']')::vector
FROM episodes e
JOIN users u ON u.id = e.user_id
WHERE u.external_user_id = 'user_demo_001'
RETURNING episode_id;






INSERT INTO background_jobs (
  user_id,
  job_type,
  status,
  payload,
  scheduled_at
)
SELECT
  id,
  'memory_consolidation',
  'queued',
  '{"reason":"seed demo","target":"episodes_to_beliefs"}'::jsonb,
  NOW()
FROM users
WHERE external_user_id = 'user_demo_001'
RETURNING *;












SELECT u.external_user_id, u.display_name, s.title, s.status
FROM users u
JOIN sessions s ON s.user_id = u.id
WHERE u.external_user_id = 'user_demo_001';


SELECT turn_index, role, content, salience_score
FROM conversation_turns
ORDER BY turn_index;

SELECT event_type, summary, importance_score, strength_score
FROM episodes
ORDER BY created_at;


SELECT
  b.namespace,
  b.subject,
  b.predicate,
  b.object_value,
  b.confidence,
  e.event_type,
  be.evidence_weight
FROM beliefs b
JOIN belief_evidence be ON be.belief_id = b.id
JOIN episodes e ON e.id = be.episode_id
ORDER BY b.created_at, e.created_at;


SELECT COUNT(*) AS episode_embedding_count FROM episode_embeddings;

SELECT COUNT(*) AS belief_embedding_count FROM belief_embeddings;








SELECT
  s.*
FROM sessions s
JOIN users u ON u.id = s.user_id
WHERE u.external_user_id = 'user_demo_001'
ORDER BY s.started_at DESC
LIMIT 1;





SELECT
  ct.turn_index,
  ct.role,
  ct.content,
  ct.salience_score,
  ct.created_at
FROM conversation_turns ct
JOIN sessions s ON s.id = ct.session_id
JOIN users u ON u.id = ct.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND s.id = (
    SELECT s2.id
    FROM sessions s2
    WHERE s2.user_id = u.id
    ORDER BY s2.started_at DESC
    LIMIT 1
  )
ORDER BY ct.turn_index DESC
LIMIT 10;



SELECT *
FROM (
  SELECT
    ct.turn_index,
    ct.role,
    ct.content,
    ct.salience_score,
    ct.created_at
  FROM conversation_turns ct
  JOIN sessions s ON s.id = ct.session_id
  JOIN users u ON u.id = ct.user_id
  WHERE u.external_user_id = 'user_demo_001'
    AND s.id = (
      SELECT s2.id
      FROM sessions s2
      WHERE s2.user_id = u.id
      ORDER BY s2.started_at DESC
      LIMIT 1
    )
  ORDER BY ct.turn_index DESC
  LIMIT 10
) t
ORDER BY turn_index ASC;





SELECT
  e.id,
  e.event_type,
  e.summary,
  e.importance_score,
  e.strength_score,
  e.created_at
FROM episodes e
JOIN users u ON u.id = e.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND e.is_archived = FALSE
ORDER BY e.created_at DESC
LIMIT 10;







SELECT
  e.id,
  e.event_type,
  e.summary,
  e.importance_score,
  e.strength_score,
  e.recency_score,
  (
    0.45 * e.importance_score +
    0.35 * e.strength_score +
    0.20 * e.recency_score
  ) AS retrieval_score
FROM episodes e
JOIN users u ON u.id = e.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND e.is_archived = FALSE
ORDER BY retrieval_score DESC, e.created_at DESC
LIMIT 10;







SELECT
  e.id,
  e.event_type,
  e.summary,
  e.tags,
  e.created_at
FROM episodes e
JOIN users u ON u.id = e.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND e.tags @> '["career"]'::jsonb
ORDER BY e.created_at DESC;







SELECT
  e.id,
  e.event_type,
  e.summary,
  e.importance_score,
  e.created_at
FROM episodes e
JOIN users u ON u.id = e.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND e.event_type = 'career_preference'
ORDER BY e.created_at DESC;









SELECT
  b.id,
  b.namespace,
  b.subject,
  b.predicate,
  b.object_value,
  b.confidence,
  b.last_validated_at
FROM beliefs b
JOIN users u ON u.id = b.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND b.status = 'active'
ORDER BY b.confidence DESC, b.last_validated_at DESC;




SELECT
  b.id,
  b.subject,
  b.predicate,
  b.object_value,
  b.confidence
FROM beliefs b
JOIN users u ON u.id = b.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND b.namespace = 'career'
  AND b.status = 'active'
ORDER BY b.confidence DESC
LIMIT 10;









SELECT
  b.id AS belief_id,
  b.namespace,
  b.subject,
  b.predicate,
  b.object_value,
  b.confidence,
  e.id AS episode_id,
  e.event_type,
  e.summary,
  be.evidence_weight
FROM beliefs b
JOIN belief_evidence be ON be.belief_id = b.id
JOIN episodes e ON e.id = be.episode_id
JOIN users u ON u.id = b.user_id
WHERE u.external_user_id = 'user_demo_001'
  AND b.status = 'active'
ORDER BY b.confidence DESC, be.evidence_weight DESC, e.created_at DESC;








WITH target_user AS (
  SELECT id
  FROM users
  WHERE external_user_id = 'user_demo_001'
),
latest_session AS (
  SELECT s.id
  FROM sessions s
  JOIN target_user tu ON tu.id = s.user_id
  ORDER BY s.started_at DESC
  LIMIT 1
),
recent_turns AS (
  SELECT
    ct.id,
    ct.turn_index,
    ct.role,
    ct.content,
    ct.salience_score,
    ct.created_at
  FROM conversation_turns ct
  JOIN latest_session ls ON ls.id = ct.session_id
  ORDER BY ct.turn_index DESC
  LIMIT 8
),
top_episodes AS (
  SELECT
    e.id,
    e.event_type,
    e.summary,
    e.importance_score,
    e.strength_score,
    e.created_at
  FROM episodes e
  JOIN target_user tu ON tu.id = e.user_id
  WHERE e.is_archived = FALSE
  ORDER BY
    (0.45 * e.importance_score + 0.35 * e.strength_score + 0.20 * e.recency_score) DESC,
    e.created_at DESC
  LIMIT 5
),
top_beliefs AS (
  SELECT
    b.id,
    b.namespace,
    b.subject,
    b.predicate,
    b.object_value,
    b.confidence,
    b.last_validated_at
  FROM beliefs b
  JOIN target_user tu ON tu.id = b.user_id
  WHERE b.status = 'active'
  ORDER BY b.confidence DESC, b.last_validated_at DESC
  LIMIT 5
)
SELECT
  'recent_turn' AS item_type,
  rt.id::text AS item_id,
  rt.role AS label_1,
  rt.content AS label_2,
  rt.salience_score AS score_1,
  NULL::real AS score_2,
  rt.created_at AS ts
FROM recent_turns rt

UNION ALL

SELECT
  'episode' AS item_type,
  te.id::text AS item_id,
  te.event_type AS label_1,
  te.summary AS label_2,
  te.importance_score AS score_1,
  te.strength_score AS score_2,
  te.created_at AS ts
FROM top_episodes te

UNION ALL

SELECT
  'belief' AS item_type,
  tb.id::text AS item_id,
  tb.predicate AS label_1,
  tb.object_value AS label_2,
  tb.confidence AS score_1,
  NULL::real AS score_2,
  tb.last_validated_at AS ts
FROM top_beliefs tb

ORDER BY ts DESC NULLS LAST;









CREATE OR REPLACE VIEW memory_pack_demo AS
WITH target_user AS (
  SELECT id
  FROM users
  WHERE external_user_id = 'user_demo_001'
),
latest_session AS (
  SELECT s.id
  FROM sessions s
  JOIN target_user tu ON tu.id = s.user_id
  ORDER BY s.started_at DESC
  LIMIT 1
),
recent_turns AS (
  SELECT
    ct.id,
    ct.turn_index,
    ct.role,
    ct.content,
    ct.salience_score,
    ct.created_at
  FROM conversation_turns ct
  JOIN latest_session ls ON ls.id = ct.session_id
  ORDER BY ct.turn_index DESC
  LIMIT 8
),
top_episodes AS (
  SELECT
    e.id,
    e.event_type,
    e.summary,
    e.importance_score,
    e.strength_score,
    e.created_at
  FROM episodes e
  JOIN target_user tu ON tu.id = e.user_id
  WHERE e.is_archived = FALSE
  ORDER BY
    (0.45 * e.importance_score + 0.35 * e.strength_score + 0.20 * e.recency_score) DESC,
    e.created_at DESC
  LIMIT 5
),
top_beliefs AS (
  SELECT
    b.id,
    b.namespace,
    b.subject,
    b.predicate,
    b.object_value,
    b.confidence,
    b.last_validated_at
  FROM beliefs b
  JOIN target_user tu ON tu.id = b.user_id
  WHERE b.status = 'active'
  ORDER BY b.confidence DESC, b.last_validated_at DESC
  LIMIT 5
)
SELECT
  'recent_turn' AS item_type,
  rt.id::text AS item_id,
  rt.role AS label_1,
  rt.content AS label_2,
  rt.salience_score AS score_1,
  NULL::real AS score_2,
  rt.created_at AS ts
FROM recent_turns rt

UNION ALL

SELECT
  'episode' AS item_type,
  te.id::text AS item_id,
  te.event_type AS label_1,
  te.summary AS label_2,
  te.importance_score AS score_1,
  te.strength_score AS score_2,
  te.created_at AS ts
FROM top_episodes te

UNION ALL

SELECT
  'belief' AS item_type,
  tb.id::text AS item_id,
  tb.predicate AS label_1,
  tb.object_value AS label_2,
  tb.confidence AS score_1,
  NULL::real AS score_2,
  tb.last_validated_at AS ts
FROM top_beliefs tb;





SELECT * FROM memory_pack_demo
ORDER BY ts DESC NULLS LAST;













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




SELECT *
FROM get_memory_pack('user_demo_001')
ORDER BY ts DESC NULLS LAST;


SELECT
  s.id AS session_id,
  s.started_at
FROM sessions s
JOIN users u ON u.id = s.user_id
WHERE u.external_user_id = 'user_demo_001'
ORDER BY s.started_at DESC
LIMIT 1;



SELECT
  id,
  session_id,
  role,
  content,
  token_count,
  turn_index,
  salience_score,
  created_at
FROM conversation_turns
WHERE session_id = '97a20243-b681-4cef-bc88-39f94b43007f'
ORDER BY turn_index DESC;





ALTER TABLE conversation_turns
ADD COLUMN IF NOT EXISTS decay_score DOUBLE PRECISION DEFAULT 1.0,
ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ NULL;

ALTER TABLE episodes
ADD COLUMN IF NOT EXISTS decay_score DOUBLE PRECISION DEFAULT 1.0,
ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ NULL,
ADD COLUMN IF NOT EXISTS last_consolidated_at TIMESTAMPTZ NULL;

ALTER TABLE beliefs
ADD COLUMN IF NOT EXISTS revision_count INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS last_revised_at TIMESTAMPTZ NULL;



SELECT
  s.id,
  s.title,
  s.started_at
FROM sessions s
JOIN users u ON u.id = s.user_id
WHERE u.external_user_id = 'user_demo_001'
ORDER BY s.started_at DESC;





SELECT
  e.id,
  e.event_type,
  e.summary,
  e.importance_score,
  e.strength_score,
  e.recency_score,
  e.created_at
FROM episodes e
JOIN users u ON u.id = e.user_id
WHERE u.external_user_id = 'user_demo_001'
ORDER BY e.created_at DESC
LIMIT 5;







ALTER TABLE episodes
ADD COLUMN IF NOT EXISTS consolidated_into_belief BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE episodes
ADD COLUMN IF NOT EXISTS consolidated_belief_id UUID REFERENCES beliefs(id) ON DELETE SET NULL;







SELECT
  b.id,
  b.namespace,
  b.subject,
  b.predicate,
  b.object_value,
  b.confidence,
  b.status,
  b.created_at
FROM beliefs b
JOIN users u ON u.id = b.user_id
WHERE u.external_user_id = 'user_demo_001'
ORDER BY b.created_at DESC;




SELECT
  e.id,
  e.event_type,
  e.summary,
  e.consolidated_into_belief,
  e.consolidated_belief_id,
  e.last_consolidated_at
FROM episodes e
JOIN users u ON u.id = e.user_id
WHERE u.external_user_id = 'user_demo_001'
ORDER BY e.created_at DESC;














SELECT
  b.id AS belief_id,
  b.predicate,
  b.object_value,
  e.id AS episode_id,
  e.event_type,
  e.summary,
  be.evidence_weight
FROM beliefs b
JOIN belief_evidence be ON be.belief_id = b.id
JOIN episodes e ON e.id = be.episode_id
JOIN users u ON u.id = b.user_id
WHERE u.external_user_id = 'user_demo_001'
ORDER BY b.created_at DESC, e.created_at DESC;




SELECT
  s.id AS session_id,
  s.title,
  s.status,
  s.started_at
FROM sessions s
JOIN users u ON u.id = s.user_id
WHERE u.external_user_id = 'user_demo_001'
ORDER BY s.started_at DESC;






-- 001_create_working_memory.sql

CREATE TABLE IF NOT EXISTS working_memory (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id          UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

    -- The active goal: short string, agent-written
    active_goal         TEXT,

    -- Current step within the goal (enum-like but flexible)
    task_phase          TEXT,                     -- e.g. 'gathering_context', 'proposing', 'confirming', 'resolved'

    -- Structured key-value state: entities, slots, tracked values
    -- Use JSONB so you can store {"entity": "login endpoint", "error_code": "401"}
    state_slots         JSONB NOT NULL DEFAULT '{}',

    -- Pending question the agent asked, waiting for user answer
    pending_question    TEXT,

    -- How many turns this goal has been active
    active_turn_count   INTEGER NOT NULL DEFAULT 0,

    -- Last time this working memory entry was touched
    last_updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Hard expiry: set to NOW() + interval on creation, checked on read
    expires_at          TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '2 hours'),

    -- Soft flag: can be explicitly marked resolved without deletion
    is_resolved         BOOLEAN NOT NULL DEFAULT FALSE,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Only one active working memory per session at a time
    CONSTRAINT uq_working_memory_session UNIQUE (session_id)
);

-- Index for fast lookup by session
CREATE INDEX IF NOT EXISTS idx_wm_session ON working_memory(session_id);

-- Index for TTL cleanup job
CREATE INDEX IF NOT EXISTS idx_wm_expires ON working_memory(expires_at) WHERE is_resolved = FALSE;

-- Index for user-level queries
CREATE INDEX IF NOT EXISTS idx_wm_user ON working_memory(user_id);