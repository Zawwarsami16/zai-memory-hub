-- ZAI Memory Hub — initial schema
-- Idempotent: safe to re-run.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- entities -----------------------------------------------------
CREATE TABLE IF NOT EXISTS entities (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text UNIQUE NOT NULL,
    kind        text NOT NULL,
    display     text NOT NULL,
    metadata    jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS entities_kind_idx ON entities(kind);

-- memories -----------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content     text NOT NULL,
    tags        text[] NOT NULL DEFAULT '{}',
    entity_ids  uuid[] NOT NULL DEFAULT '{}',
    written_by  text NOT NULL,
    session_id  text,
    embedding   vector(1024),
    importance  smallint NOT NULL DEFAULT 3 CHECK (importance BETWEEN 1 AND 5),
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS memories_tags_idx ON memories USING gin(tags);
CREATE INDEX IF NOT EXISTS memories_entities_idx ON memories USING gin(entity_ids);
CREATE INDEX IF NOT EXISTS memories_created_idx ON memories(created_at DESC);
-- ivfflat index requires lists; can rebuild when memories grow
CREATE INDEX IF NOT EXISTS memories_embedding_idx ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- decisions ----------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    summary       text NOT NULL,
    rationale     text NOT NULL,
    alternatives  text,
    entity_ids    uuid[] NOT NULL DEFAULT '{}',
    written_by    text NOT NULL,
    supersedes    uuid REFERENCES decisions(id),
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS decisions_entities_idx ON decisions USING gin(entity_ids);
CREATE INDEX IF NOT EXISTS decisions_created_idx ON decisions(created_at DESC);

-- tool_calls ---------------------------------------------------
CREATE TABLE IF NOT EXISTS tool_calls (
    id           bigserial PRIMARY KEY,
    tool_name    text NOT NULL,
    args         jsonb NOT NULL,
    result_brief text,
    called_by    text NOT NULL,
    session_id   text,
    duration_ms  integer,
    status       text NOT NULL DEFAULT 'ok',
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tool_calls_created_idx ON tool_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS tool_calls_called_by_idx ON tool_calls(called_by, created_at DESC);

-- interactions -------------------------------------------------
CREATE TABLE IF NOT EXISTS interactions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    text NOT NULL,
    surface       text NOT NULL,
    summary       text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    ended_at      timestamptz,
    metadata      jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS interactions_session_idx ON interactions(session_id);
CREATE INDEX IF NOT EXISTS interactions_surface_idx ON interactions(surface, started_at DESC);

-- LISTEN/NOTIFY triggers ---------------------------------------
CREATE OR REPLACE FUNCTION notify_activity() RETURNS trigger AS $$
DECLARE
    actor text;
BEGIN
    BEGIN
        actor := NEW.written_by;
    EXCEPTION WHEN OTHERS THEN
        actor := NEW.called_by;
    END;
    PERFORM pg_notify('zai_hub_activity', json_build_object(
        'kind', TG_TABLE_NAME,
        'id',   NEW.id::text,
        'by',   actor,
        'at',   NEW.created_at
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS memories_notify  ON memories;
DROP TRIGGER IF EXISTS decisions_notify ON decisions;
DROP TRIGGER IF EXISTS tool_calls_notify ON tool_calls;
CREATE TRIGGER memories_notify  AFTER INSERT ON memories  FOR EACH ROW EXECUTE FUNCTION notify_activity();
CREATE TRIGGER decisions_notify AFTER INSERT ON decisions FOR EACH ROW EXECUTE FUNCTION notify_activity();
CREATE TRIGGER tool_calls_notify AFTER INSERT ON tool_calls FOR EACH ROW EXECUTE FUNCTION notify_activity();

-- Seed entities ------------------------------------------------
INSERT INTO entities(slug, kind, display) VALUES
    ('zawwar',           'person',  'Zawwar Sami'),
    ('zai-memory-hub',   'project', 'ZAI Memory Hub'),
    ('anteroom-studio',  'project', 'Anteroom Studio'),
    ('vps-claude',       'thread',  'VPS Claude session pool'),
    ('local-claude',     'thread',  'Local Claude session pool'),
    ('chat-claude',      'thread',  'claude.ai chat sessions')
ON CONFLICT (slug) DO NOTHING;

-- v_conflicts view (heuristic v1) -----------------------------
CREATE OR REPLACE VIEW v_conflicts AS
SELECT
    a.id AS memory_a, b.id AS memory_b,
    a.content AS content_a, b.content AS content_b,
    a.written_by AS by_a, b.written_by AS by_b,
    a.created_at AS at_a, b.created_at AS at_b
FROM memories a
JOIN memories b ON a.id < b.id
WHERE a.entity_ids && b.entity_ids
  AND a.created_at > now() - interval '14 days'
  AND b.created_at > now() - interval '14 days'
  AND (
      (a.content ILIKE '%is true%'  AND b.content ILIKE '%is false%') OR
      (a.content ILIKE '%works%'    AND (b.content ILIKE '%broken%' OR b.content ILIKE '%fails%')) OR
      (a.content ILIKE '%use %'     AND b.content ILIKE '%avoid %')
  );
