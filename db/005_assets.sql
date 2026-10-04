-- Durable binary assets. Render's filesystem is only a read-through cache.
BEGIN;
CREATE TABLE IF NOT EXISTS hub_assets (
    path TEXT PRIMARY KEY CHECK (path LIKE 'uploads/%' OR path LIKE 'gen/%'),
    data BYTEA NOT NULL,
    sha256 TEXT NOT NULL,
    media_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hub_asset_imports (
    source TEXT PRIMARY KEY,
    asset_count INTEGER NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMIT;
