-- Migration 003 — per-agent tokens with role-based scopes + OAuth 2.0 DCR
-- Applied 2026-05-20.
--
-- Replaces the single shared bearer token with a table of tokens, each
-- bound to one slug + one role.  The MCP server uses this table to:
--   1.  Validate the incoming bearer (replaces Caddy edge check)
--   2.  Stamp `written_by` from the token row's slug (not from client env)
--   3.  Filter the tool list returned via MCP capabilities by the token's role
--
-- Also adds the storage tables for OAuth 2.0 Dynamic Client Registration so
-- Claude.ai web (and any other MCP-aware OAuth client) can self-register and
-- get a bearer through a standard auth-code flow.

BEGIN;

-- ----- agent_tokens ------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_tokens (
    id            BIGSERIAL PRIMARY KEY,
    token_hash    TEXT NOT NULL UNIQUE,    -- sha256 of the actual bearer string
    slug          TEXT NOT NULL,           -- attributed writer slug
    role          TEXT NOT NULL CHECK (role IN ('admin', 'writer', 'recall-only')),
    label         TEXT,                    -- "claude-ai-web", "local-laptop", human-readable
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    issued_via    TEXT DEFAULT 'manual'    -- 'manual' | 'oauth' | 'bootstrap'
);
CREATE INDEX IF NOT EXISTS agent_tokens_slug_idx ON agent_tokens(slug);
CREATE INDEX IF NOT EXISTS agent_tokens_active_idx ON agent_tokens(token_hash) WHERE revoked_at IS NULL;

-- ----- OAuth 2.0 DCR storage --------------------------------------
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id      TEXT PRIMARY KEY,
    client_secret  TEXT,                   -- nullable for public clients
    client_name    TEXT NOT NULL,          -- from DCR registration ('Claude.ai', etc)
    redirect_uris  TEXT[] NOT NULL,
    grant_types    TEXT[] NOT NULL DEFAULT ARRAY['authorization_code', 'refresh_token'],
    token_endpoint_auth_method TEXT NOT NULL DEFAULT 'none',
    scope          TEXT NOT NULL DEFAULT 'mcp',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS oauth_codes (
    code           TEXT PRIMARY KEY,
    client_id      TEXT NOT NULL REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    redirect_uri   TEXT NOT NULL,
    code_challenge TEXT,
    code_challenge_method TEXT,
    scope          TEXT,
    slug           TEXT NOT NULL,          -- what slug this code will mint for
    role           TEXT NOT NULL,
    expires_at     TIMESTAMPTZ NOT NULL,
    used_at        TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS oauth_codes_exp_idx ON oauth_codes(expires_at);

COMMIT;
