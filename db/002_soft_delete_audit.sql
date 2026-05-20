-- Migration 002 — soft delete + audit log
-- Applied 2026-05-20.  Adds non-destructive delete semantics across
-- memories and decisions, and an audit_log table that records every
-- mutation (who did what, when, to which row).

BEGIN;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT;

CREATE INDEX IF NOT EXISTS memories_deleted_idx ON memories (deleted_at) WHERE deleted_at IS NULL;

ALTER TABLE decisions
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS deleted_by TEXT;

CREATE INDEX IF NOT EXISTS decisions_deleted_idx ON decisions (deleted_at) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    target_kind TEXT NOT NULL,        -- 'memory' | 'decision'
    target_id UUID NOT NULL,
    action TEXT NOT NULL,             -- 'insert' | 'soft_delete' | 'restore' | 'hard_delete'
    actor TEXT,                       -- who did it (slug or 'dashboard-user')
    detail JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_log_target_idx ON audit_log (target_kind, target_id);
CREATE INDEX IF NOT EXISTS audit_log_created_idx ON audit_log (created_at DESC);

-- Update the activity NOTIFY trigger to also fire on delete/restore
CREATE OR REPLACE FUNCTION notify_activity() RETURNS trigger AS $$
DECLARE
    actor text;
    action text;
BEGIN
    BEGIN
        actor := NEW.written_by;
    EXCEPTION WHEN OTHERS THEN
        actor := NEW.called_by;
    END;
    IF TG_OP = 'INSERT' THEN
        action := 'insert';
    ELSIF TG_OP = 'UPDATE' THEN
        IF NEW.deleted_at IS NOT NULL AND OLD.deleted_at IS NULL THEN
            action := 'delete';
            actor := COALESCE(NEW.deleted_by, actor);
        ELSIF NEW.deleted_at IS NULL AND OLD.deleted_at IS NOT NULL THEN
            action := 'restore';
        ELSE
            action := 'update';
        END IF;
    ELSE
        action := lower(TG_OP);
    END IF;
    PERFORM pg_notify('zai_hub_activity', json_build_object(
        'kind',   TG_TABLE_NAME,
        'action', action,
        'id',     NEW.id::text,
        'by',     actor,
        'at',     NEW.created_at
    )::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Add UPDATE trigger on memories + decisions for soft-delete notifications
DROP TRIGGER IF EXISTS memories_update_activity ON memories;
CREATE TRIGGER memories_update_activity
    AFTER UPDATE ON memories
    FOR EACH ROW
    WHEN (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
    EXECUTE FUNCTION notify_activity();

DROP TRIGGER IF EXISTS decisions_update_activity ON decisions;
CREATE TRIGGER decisions_update_activity
    AFTER UPDATE ON decisions
    FOR EACH ROW
    WHEN (OLD.deleted_at IS DISTINCT FROM NEW.deleted_at)
    EXECUTE FUNCTION notify_activity();

COMMIT;
