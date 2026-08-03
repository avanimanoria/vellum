-- Vellum working-memory v2
-- Apply after vellum_memory_db.sql. This intentionally creates new v2 tables;
-- the old working_memory table can be retained until the application cutover is verified.

CREATE TABLE IF NOT EXISTS working_memory_states (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    agent_key TEXT NOT NULL DEFAULT 'primary',
    active_goal TEXT CHECK (active_goal IS NULL OR length(active_goal) <= 2000),
    execution_state TEXT NOT NULL DEFAULT 'idle'
        CHECK (execution_state IN ('idle', 'in_progress', 'waiting_user', 'waiting_tool', 'blocked', 'done')),
    task_context JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(task_context) = 'object'),
    last_source_turn_id UUID REFERENCES conversation_turns(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'expired', 'archived')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '90 minutes'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT working_memory_states_session_agent_unique UNIQUE (session_id, agent_key)
);

CREATE INDEX IF NOT EXISTS idx_wm_states_active_lookup
    ON working_memory_states (user_id, session_id, agent_key, expires_at)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_wm_states_expiry
    ON working_memory_states (expires_at)
    WHERE status = 'active';

CREATE TABLE IF NOT EXISTS working_memory_items (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    working_memory_state_id UUID NOT NULL REFERENCES working_memory_states(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN (
        'subtask', 'next_action', 'constraint', 'decision',
        'open_question', 'entity_reference', 'temporary_fact'
    )),
    dedupe_key TEXT NOT NULL CHECK (length(dedupe_key) BETWEEN 1 AND 160),
    content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 2000),
    priority SMALLINT NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'resolved', 'superseded', 'expired')),
    confidence NUMERIC(4,3) NOT NULL DEFAULT 1.000
        CHECK (confidence >= 0 AND confidence <= 1),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(metadata) = 'object'),
    source_turn_id UUID REFERENCES conversation_turns(id) ON DELETE SET NULL,
    last_confirmed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT working_memory_items_state_kind_key_unique
        UNIQUE (working_memory_state_id, kind, dedupe_key)
);

CREATE INDEX IF NOT EXISTS idx_wm_items_active_lookup
    ON working_memory_items (working_memory_state_id, priority DESC, updated_at DESC)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_wm_items_expiry
    ON working_memory_items (expires_at)
    WHERE status = 'active';

-- revision_log is belief-specific in the current schema, so working-memory
-- revisions receive their own explicit audit table rather than overloading it.
CREATE TABLE IF NOT EXISTS working_memory_revision_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    working_memory_state_id UUID NOT NULL REFERENCES working_memory_states(id) ON DELETE CASCADE,
    source_turn_id UUID REFERENCES conversation_turns(id) ON DELETE SET NULL,
    action TEXT NOT NULL CHECK (action IN ('created', 'updated', 'resolved', 'expired')),
    before_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_wm_revision_state_created
    ON working_memory_revision_log (working_memory_state_id, created_at DESC);

-- A state must never pair a valid user with somebody else's valid session.
-- The individual foreign keys above cannot enforce that relationship.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sessions_id_user_unique'
    ) THEN
        ALTER TABLE sessions
            ADD CONSTRAINT sessions_id_user_unique UNIQUE (id, user_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'wm_states_session_user_owner_fk'
    ) THEN
        ALTER TABLE working_memory_states
            ADD CONSTRAINT wm_states_session_user_owner_fk
            FOREIGN KEY (session_id, user_id)
            REFERENCES sessions (id, user_id)
            ON DELETE CASCADE;
    END IF;
END $$;
