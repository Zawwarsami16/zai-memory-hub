# ZAI Memory Hub — Schema

> Postgres 16 + pgvector. Tables named for the *entity*, not the *table operation*. All timestamps `timestamptz default now()`.

## Tables

### `entities`
First-class concept: a person, project, thread, repo, location — anything memories can be *about*.

```sql
CREATE TABLE entities (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text UNIQUE NOT NULL,             -- e.g. 'me', 'my-project', 'a-thread-i-care-about'
    kind        text NOT NULL,                    -- 'person', 'project', 'thread', 'repo', 'task'
    display     text NOT NULL,
    metadata    jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX entities_kind_idx ON entities(kind);
```

### `memories`
Free-form observations. Multiple Claudes can write. Tagged with entities. Embedded for semantic recall.

```sql
CREATE TABLE memories (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    content     text NOT NULL,
    tags        text[] NOT NULL DEFAULT '{}',     -- free-form labels
    entity_ids  uuid[] NOT NULL DEFAULT '{}',     -- references entities.id
    written_by  text NOT NULL,                    -- session identifier: 'vps-claude', 'local-claude', 'chat-claude'
    session_id  text,                              -- optional finer-grained session
    embedding   vector(1024),                     -- Voyage AI; nullable until backfilled
    importance  smallint NOT NULL DEFAULT 3,      -- 1=ephemeral, 5=load-bearing
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memories_tags_idx ON memories USING gin(tags);
CREATE INDEX memories_entities_idx ON memories USING gin(entity_ids);
CREATE INDEX memories_created_idx ON memories(created_at DESC);
CREATE INDEX memories_embedding_idx ON memories USING ivfflat (embedding vector_cosine_ops);
```

### `decisions`
Durable choices. Separate from memories because they're auditable and rarely revised. When revised, a new row references the old.

```sql
CREATE TABLE decisions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    summary       text NOT NULL,                   -- one-line headline
    rationale     text NOT NULL,                   -- why this choice
    alternatives  text,                             -- what was considered and rejected
    entity_ids    uuid[] NOT NULL DEFAULT '{}',
    written_by    text NOT NULL,
    supersedes    uuid REFERENCES decisions(id),    -- if revising an earlier decision
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX decisions_entities_idx ON decisions USING gin(entity_ids);
CREATE INDEX decisions_created_idx ON decisions(created_at DESC);
```

### `tool_calls`
Every MCP tool invocation. The activity stream feeds from here.

```sql
CREATE TABLE tool_calls (
    id           bigserial PRIMARY KEY,
    tool_name    text NOT NULL,                   -- 'memory.recall', 'memory.add', etc.
    args         jsonb NOT NULL,
    result_brief text,                             -- short result for stream display
    called_by    text NOT NULL,                   -- session identifier
    session_id   text,
    duration_ms  integer,
    status       text NOT NULL DEFAULT 'ok',      -- 'ok', 'error'
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX tool_calls_created_idx ON tool_calls(created_at DESC);
CREATE INDEX tool_calls_called_by_idx ON tool_calls(called_by, created_at DESC);
```

### `interactions`
Coarser-grained sessions. A conversation start, end, key turning point. Optional.

```sql
CREATE TABLE interactions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    text NOT NULL,
    surface       text NOT NULL,                   -- 'vps-cli', 'local-cli', 'claude.ai-web', 'mobile'
    summary       text,
    started_at    timestamptz NOT NULL DEFAULT now(),
    ended_at      timestamptz,
    metadata      jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX interactions_session_idx ON interactions(session_id);
CREATE INDEX interactions_surface_idx ON interactions(surface, started_at DESC);
```

## Views

### `v_conflicts`
Memories that contradict each other on the same entity within a recency window. Surfaces to dashboard.

```sql
-- Heuristic v1: same entity, opposite sentiment keywords, within 7 days.
-- This is intentionally crude; refinement deferred to v2.
CREATE VIEW v_conflicts AS
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
      (a.content ILIKE '%is true%' AND b.content ILIKE '%is false%')
      OR (a.content ILIKE '%works%' AND b.content ILIKE '%broken%' OR b.content ILIKE '%fails%')
      OR (a.content ILIKE '%use %' AND b.content ILIKE '%avoid %')
  );
```

## Triggers — LISTEN/NOTIFY

```sql
CREATE OR REPLACE FUNCTION notify_activity() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('zai_hub_activity', json_build_object(
        'kind', TG_TABLE_NAME,
        'id', NEW.id,
        'by', COALESCE(NEW.written_by, NEW.called_by),
        'at', NEW.created_at
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER memories_notify AFTER INSERT ON memories FOR EACH ROW EXECUTE FUNCTION notify_activity();
CREATE TRIGGER decisions_notify AFTER INSERT ON decisions FOR EACH ROW EXECUTE FUNCTION notify_activity();
CREATE TRIGGER tool_calls_notify AFTER INSERT ON tool_calls FOR EACH ROW EXECUTE FUNCTION notify_activity();
```

Dashboard subscribes to `zai_hub_activity` channel, fans out to connected SSE clients.

## Seed data

On migration init, insert canonical entities:

```sql
INSERT INTO entities(slug, kind, display) VALUES
    ('me',               'person',  'Me'),
    ('zai-memory-hub',   'project', 'ZAI Memory Hub'),
    ('anteroom-studio',  'project', 'Anteroom Studio'),
    ('vps-claude',       'thread',  'VPS Claude session pool'),
    ('local-claude',     'thread',  'Local Claude session pool'),
    ('chat-claude',      'thread',  'claude.ai chat sessions')
ON CONFLICT (slug) DO NOTHING;
```
