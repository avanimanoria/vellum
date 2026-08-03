-- Hybrid retrieval metadata and pgvector indexes.
-- text-embedding-3-small uses 1536 dimensions, matching the existing tables.

ALTER TABLE episode_embeddings
    ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT 'text-embedding-3-small',
    ADD COLUMN IF NOT EXISTS source_hash TEXT,
    ADD COLUMN IF NOT EXISTS generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE belief_embeddings
    ADD COLUMN IF NOT EXISTS embedding_model TEXT NOT NULL DEFAULT 'text-embedding-3-small',
    ADD COLUMN IF NOT EXISTS source_hash TEXT,
    ADD COLUMN IF NOT EXISTS generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- HNSW is used for approximate nearest-neighbour candidate selection. Final
-- ranking remains exact, deterministic hybrid scoring in the application.
CREATE INDEX IF NOT EXISTS idx_episode_embeddings_hnsw_cosine
    ON episode_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_belief_embeddings_hnsw_cosine
    ON belief_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_background_jobs_embedding_queue
    ON background_jobs (job_type, status, scheduled_at)
    WHERE status = 'queued';
