-- Migration: Create gate_decision_logs table for Vellum's memory gate observability
CREATE TABLE IF NOT EXISTS gate_decision_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    turn_content TEXT NOT NULL,
    salience_score NUMERIC(4, 2),
    novelty_score NUMERIC(4, 2),
    contradiction_potential NUMERIC(4, 2),
    importance_score NUMERIC(4, 2),
    utility_score NUMERIC(4, 2),
    decisions JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
