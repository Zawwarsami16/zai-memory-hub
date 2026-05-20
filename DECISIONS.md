# ZAI Memory Hub — Decisions Log

> Each entry: date, the choice, what was considered and rejected, why. Append-only.

---

## 2026-05-19 — Self-hosted Postgres on VPS, not Supabase

**Choice:** Self-hosted Postgres 16 + pgvector on the existing VPS.

**Considered:** Supabase (managed Postgres + Realtime + Auth in one).

**Rejected because:**
- Adds vendor lock-in for a layer that we want to *be ours*. The Hub is supposed to be the identity backbone — putting it on a managed service we can be evicted from defeats the point.
- Latency: every MCP tool call would round-trip Toronto → Supabase region. Self-hosted is sub-1ms DB latency.
- Free tier has read/write quotas. The Hub will be chatty (every tool call inserts a row). Hitting limits → forced upgrade or rate-limit pain.
- pg_dump → migration to managed later is one command if we ever need to.

**Trade-off accepted:** We own Postgres ops (backups, upgrades, security patches). Backups already part of existing VPS cron. Security: Caddy + magic-link auth, no public Postgres port.

---

## 2026-05-19 — FastMCP (Python) over building MCP from scratch

**Choice:** Use [FastMCP](https://github.com/jlowin/fastmcp) (or the official `mcp` Python SDK).

**Considered:** Hand-rolling JSON-RPC, Node-based MCP server.

**Rejected because:**
- Anthropic ships the MCP spec; using the official SDK means protocol changes don't break us.
- Python is what I (VPS-Claude) reach for first; faster iteration.
- Node version exists too but JS/TS adds a build step we don't need.

---

## 2026-05-19 — `decisions` table separate from `memories`

**Choice:** Two tables: memories (observational) and decisions (durable choices).

**Considered:** One unified `memories` table with a `kind` enum.

**Rejected because:**
- Decisions have *different lifecycles*. They get superseded, not deleted. They have rationale + alternatives fields that don't apply to memories. Forcing both into one schema either bloats memories with mostly-null columns or weakens the decision audit trail.
- Querying "what was decided about X" should be a clean lookup, not a filtered scan.

**Trade-off:** Two tables to keep migrations consistent. Worth it.

---

## 2026-05-19 — pgvector in-DB, not a separate vector store

**Choice:** pgvector extension, embeddings stored in the same DB as the source memory.

**Considered:** Qdrant, Pinecone, Chroma.

**Rejected because:**
- For <100K memories (realistic v1 ceiling), pgvector is fast enough.
- One DB = one backup, one transaction boundary.
- Hot-swap to a dedicated vector store later if recall latency becomes a problem. Schema doesn't change.

---

## 2026-05-19 — Magic-link auth (single user) for v1

**Choice:** Magic-link email to Zawwar's gmail. One user account hardcoded.

**Considered:** Public dashboard, OAuth (Google login), GitHub OAuth.

**Rejected because:**
- Public: would force censoring of what's memo'd, defeating utility.
- OAuth: overkill for one user, adds external dependency on identity provider.
- Magic link via Gmail API (we already have Gmail access on this VPS) is friction-free for Zawwar and adds no new identity provider.

**Path to v2:** If we ever expose a public-read activity stream subdomain (a subdomain), we'll have to add filtering by `importance` or explicit `public:true` flag on memories. Decision deferred.

---

## 2026-05-19 — `interactions.surface` enum-as-text, not enum type

**Choice:** Use `text` with documented values, not Postgres `CREATE TYPE`.

**Considered:** PG enum type for surface (`vps-cli`, `local-cli`, `claude.ai-web`, ...).

**Rejected because:**
- PG enums require ALTER TYPE for new values, harder to migrate.
- Application validates the set.
- Tiny perf gain from enum is not worth the migration friction.

---

## 2026-05-19 — Conflict detection is heuristic, not semantic

**Choice:** v1 `v_conflicts` view uses simple keyword/phrase pairs. v2+ may use LLM-judged contradiction.

**Rationale:** Semantic conflict detection is itself an LLM problem. Putting an LLM in the hot path of every dashboard load is wrong. Heuristic surfaces *candidate* conflicts; user (or a periodic batch job) confirms. Quality < precision; we want recall.

---

## 2026-05-19 — VPS-Claude is build coordinator; no parallel work

**Choice:** VPS-Claude builds the entire Hub. Local-Claude and chat-Claude do not write code for this project until the Hub is live.

**Per Zawwar (verbatim):** "do it all yourself, don't divide work because that becomes a mess."

**Rationale:** Three Claudes writing to the same nascent codebase without a shared memory layer = exactly the problem we're solving. Build the bridge first, *then* coordinate.

---

(append new entries below as the project evolves)
