-- Migration: Create entities and memory_edges tables for Vellum's relationship-aware graph memory model
CREATE TABLE IF NOT EXISTS entities (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) UNIQUE NOT NULL,
    category VARCHAR(50) NOT NULL, -- e.g. 'technology', 'role', 'domain'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS memory_edges (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL,
    source_type VARCHAR(50) NOT NULL, -- 'episode', 'belief', 'entity'
    target_id UUID NOT NULL,
    target_type VARCHAR(50) NOT NULL, -- 'episode', 'belief', 'entity'
    edge_type VARCHAR(50) NOT NULL,   -- 'mentions', 'evidences', 'related_to', 'supersedes'
    weight NUMERIC(3, 2) DEFAULT 1.00,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    CONSTRAINT unique_edge UNIQUE (source_id, source_type, target_id, target_type, edge_type)
);
