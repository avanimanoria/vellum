# Vellum

**Vellum** is an advanced AI memory system for persistent, personalized, and self-updating agents. It is designed to help AI systems move beyond stateless chat by storing experiences, forming beliefs from repeated evidence, retrieving relevant context across sessions, and gradually improving what the system knows about a user over time.

## Resume-ready summary

Built **Vellum**, a FastAPI + PostgreSQL + pgvector memory engine for AI agents that transforms chat interactions into episodic memories and semantic beliefs, retrieves relevant user context across sessions, and lays the foundation for contradiction-aware belief revision, memory decay, and long-term personalization.

## Why this project matters

Most LLM applications are still stateless. They respond well in the current context window, but they lose continuity across sessions, forget user preferences, and struggle to update long-term understanding when new evidence appears.

Vellum addresses that gap by acting as a memory layer for AI systems. Instead of storing only raw chat history, it separates memory into different levels:

- **Conversation memory** for exact turn-by-turn history
- **Episodic memory** for important user events and experiences
- **Semantic memory** for structured beliefs inferred from repeated evidence
- **Memory maintenance** for reinforcement, decay, consolidation, and future revision

This makes Vellum closer to a real AI memory architecture than a basic chatbot backend.

## Key features

- FastAPI backend for memory APIs
- PostgreSQL schema for long-term structured memory
- pgvector support for semantic retrieval and future hybrid ranking
- User/session/turn architecture for persistent interaction history
- Episodic memory via `episodes`
- Semantic memory via `beliefs`
- Evidence-linked reasoning via `belief_evidence`
- Background maintenance hooks for consolidation and decay
- CLI-based testing client for end-to-end interaction flows

## Current system capabilities

Vellum currently supports the following memory loop:

1. Store a user message as a conversation turn.
2. Promote high-salience turns into episodic memories.
3. Retrieve recent turns, top episodes, and top beliefs through a memory-pack function.
4. Generate an assistant response grounded in retrieved memory.
5. Store the assistant reply as a new turn.
6. Support background memory maintenance through consolidation hooks.

Current core flow:

```text
turns -> episodes -> beliefs -> retrieval -> response
```

Working-memory migration:

```powershell
psql -U postgres -d vellum_memory_db -f migrations/002_working_memory_v2.sql
```

The v2 layer stores short-lived session task state in `working_memory_states`
and normalized active items in `working_memory_items`. It is intentionally not
embedded and is only retrieved by owned user/session ID. The existing legacy
`working_memory` table is not read by the application after this migration.

When FastAPI starts, it runs a PostgreSQL-backed expiry worker every five
minutes. The worker claims `expire_working_memory` jobs with `SKIP LOCKED`,
expires stale states, and queues the next job. Run the integration suite with:

```powershell
$env:VELLUM_TEST_DATABASE='1'
python -m unittest discover -s tests -p "test_*.py" -v
```

## Tech stack

- **Backend:** FastAPI, Python
- **Database:** PostgreSQL
- **Vector support:** pgvector
- **DB driver:** psycopg
- **Testing client:** Python CLI client

## Data model

### Main tables

- `users`
- `sessions`
- `conversation_turns`
- `episodes`
- `episode_embeddings`
- `beliefs`
- `belief_evidence`
- `belief_embeddings`
- `revision_log`
- `background_jobs`

### Memory design

- `conversation_turns` stores raw dialogue exactly as it happened.
- `episodes` stores event-like memories extracted from important moments.
- `beliefs` stores generalized knowledge inferred from evidence.
- `belief_evidence` links beliefs to the episodes that support them.
- `revision_log` is intended for future contradiction-aware updates to beliefs.

This design allows Vellum to move from noisy conversational input to reusable memory structures.

## API endpoints

### `GET /`
Basic API health endpoint.

### `GET /users/{external_user_id}/memory-pack`
Returns a memory pack containing:

- recent conversation turns
- top episodes
- top beliefs

### `POST /users/{external_user_id}/sessions/{session_id}/turns`
Stores a raw conversation turn for a user session.

### `POST /chat`
Main orchestration endpoint that:

- stores the user turn
- fetches memory
- generates a memory-grounded assistant reply
- stores the assistant reply
- returns memory ids used in the response

### `POST /admin/consolidate/{external_user_id}`
Schedules background memory maintenance for a user.

## System architecture

```text
┌───────────────────────────────┐
│           Client              │
│   CLI / future frontend app   │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│          FastAPI API          │
│  /chat /turns /memory-pack    │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│      Orchestration Layer      │
│ store turn -> fetch memory -> │
│ generate reply -> store reply │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│         Memory Layers         │
│                               │
│ conversation_turns            │
│ episodes                      │
│ beliefs                       │
│ belief_evidence               │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│     PostgreSQL + pgvector     │
│ long-term storage + retrieval │
└───────────────┬───────────────┘
                │
                ▼
┌───────────────────────────────┐
│ Background maintenance jobs   │
│ decay / consolidation / later │
│ revision + compression        │
└───────────────────────────────┘
```

## Example memory behavior

Example facts Vellum can currently represent:

- A user is preparing for ML interviews.
- A user prefers backend and applied AI roles over pure research.
- A belief such as `prefers_role_family = backend_and_applied_ai` can be linked to supporting episodes.

This gives the system both personalization and traceability.

## Setup

### 1. Enable extensions

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
```

### 2. Create schema

Run the SQL for tables, indexes, helper views, and the `get_memory_pack(...)` function.

### 3. Seed demo data

Insert demo data for:

- user
- session
- conversation turns
- episodes
- beliefs
- evidence links
- embeddings
- background job rows

### 4. Start the API

```bash
uvicorn main:app --reload
```

### 5. Run the client

```bash
python client.py
```

## Example development workflow

1. Start PostgreSQL.
2. Run schema + seed SQL.
3. Launch FastAPI.
4. Use `client.py` to send messages.
5. Inspect memory-pack output.
6. Trigger consolidation.
7. Observe how turns become episodes and beliefs.

## Why Vellum stands out

Most resume projects in this space are simple RAG chatbots or wrappers around LLM APIs. Vellum is different because it focuses on the memory lifecycle itself:

- what gets remembered
- how memories are structured
- how memories are retrieved
- how beliefs are formed
- how stale information can later be revised or forgotten

That makes it much stronger as an AI systems project for backend, applied AI, and agent infrastructure roles.

## Roadmap

The strongest next upgrades for Vellum are:

### 1. Working memory layer
Add a short-term memory layer for active goals, current plans, and unresolved tasks.

### 2. Hybrid retrieval
Combine embedding similarity with recency, importance, strength, and access patterns.

### 3. Belief revision
Detect contradictions between new evidence and existing beliefs, then reinforce, supersede, deprecate, or split beliefs accordingly.

### 4. Selective memory writing
Add a memory gate to decide whether new input becomes a turn, episode, semantic candidate, or revision trigger.

### 5. Decay and compression
Improve forgetting so low-value memories decay while repeated high-value memories are reinforced or summarized.

### 6. Explainability and evals
Add retrieval explanations, quality metrics, and benchmark scenarios to make the system easier to trust and evaluate.

## Ideal use cases

Vellum can power systems such as:

- personalized learning assistants
## Ideal use cases

Vellum can power systems such as:

- personalized learning assistants
- long-term coaching agents
- interview preparation copilots
- AI copilots that need cross-session continuity
- memory-aware customer support or productivity agents

## Project status

### Fully Implemented Cognitive Layers

- **FastAPI Memory Core**: Persistent REST endpoints for agent coordination.
- **Working Memory (v2)**: Normalized working memory items and goals scoped by user/session with transactional concurrency locks.
- **Selective Memory Gate**: High-salience turn promotion, novelty detection, and chitchat filtering.
- **Embedding-Powered Hybrid Retrieval**: Ranked matches fusing semantic vectors with structured decay, recency, and conflict penalties.
- **Belief Revision & Contradiction Handling**: State updates (`supersede`, `reinforce`, `conflict`, `deprecate`) with auditing inside `revision_log`.
- **Forgetting & Compression Lifecycles**: Automatic 10% active decay of episodes, belief confidence decay, and summarization consolidation of weak memory clusters.
- **Explainable Retrieval**: Structure reason codes and explanation metadata in all memory packets and chat payloads.
- **Graph Memory / Relational Recall**: Relational memory graph linking entities and episodes via double-polymorphic edge traversal.
- **Evals & Observability Dashboard**: Consistency ratios and a 3-step conversation benchmark suite.

---

## Vellum Interactive Memory Console

Vellum now serves a high-fidelity, dark-mode administrative memory console directly from the API origin.

### Running the App
1. Ensure the PostgreSQL database is running.
2. Initialize the dev server:
   ```bash
   uvicorn main:app --reload
   ```
3. Open your browser and navigate to:
   ```text
   http://localhost:8000/
   ```

### Console Layout & Capabilities
- **Chat Terminal**: Message field scoped to the active user profile, returning message logs and matching memory type chips.
- **Explainable Retrieval Panel**: Highlights selection reason badges (`semantic_match`, `high_salience`, `reinforced_recent`) and the human-readable retrieval explanation for the turn.
- **Memory Inspector**: Searchable grid displaying all episodic and semantic memory pack items.
- **Belief Timeline**: Displays active, conflicted, and deprecated belief cards linked to the complete revision trail.
- **Observability Tab**: Displays cognitive health metrics and action buttons to trigger evals or background jobs.

---

## Guided Demo Flows

### 1. Belief Contradiction Demonstration
You can execute this flow directly in the browser or via the provided script:
- **In-Browser**: Click the **⚡ Run Revision Flow** button in the sidebar. It will automatically simulate:
  1. Setting career preference: `"I want to focus on backend engineering in my new role."` (Creates an active belief).
  2. Setting contradicting preference: `"Actually, I decided to switch and focus on frontend UI today."` (Detects contradiction, deprecates backend preference, active frontend belief, and appends `supersede` to the revision trail).
  3. Seamlessly redirects you to the **Belief Timeline** tab to view the audit log.
- **Via CLI Script**: Run the self-contained flow runner:
  ```bash
  $env:PYTHONPATH='.'
  python scratch/example_flow.py
  ```

### 2. Automated Benchmark Harness
Run Vellum's scripted conversation benchmark containing 3 conversation turns (greeting, preference expression, and contradiction):
- **Via Console**: Navigate to the **Observability** tab and click **Run Benchmark Evals**.
- **Via Endpoint**:
  ```bash
  curl -X POST http://localhost:8000/admin/run-evals/avani_researcher
  ```

---

## Notes for interview discussion

Good ways to present this project in interviews:

- Explain why stateless LLMs are limited.
- Show how Vellum separates raw history from structured memory.
- Explain the turns -> episodes -> beliefs lifecycle.
- Emphasize evidence-linked reasoning and contradiction handling.
- Discuss how this architecture supports personalization, consistency, and long-term agent behavior.
---

## Author

**Avani Manoria**

- GitHub: [@avanimanoria](https://github.com/avanimanoria)
- Project repository: [Vellum](https://github.com/avanimanoria/vellum.git)
