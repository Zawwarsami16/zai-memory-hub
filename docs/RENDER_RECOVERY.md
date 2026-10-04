# Render recovery and asset storage

## Current deployment

The supported Render entrypoint is `uvicorn server.render_app:app`. It serves
the Postgres-backed dashboard and authenticated MCP on one origin. The older
`render_mcp.py` GitHub-overlay prototype is not the deployed architecture.

Apply all SQL migrations before using the dashboard on another host. Render
bootstrap does this automatically. Bootstrap imports the configured private
memory snapshot only when the memories table is empty. It must not replay old
entity metadata on routine starts.

## Files are part of the data

Migration 005 stores uploads and generated media in `hub_assets`. The paths
remain `/static/uploads/...` and `/static/gen/...`; existing memory links are
unchanged. Files on local disk are a disposable read-through cache. Newly
uploaded PDFs and generated covers commit to Postgres before their links are
returned. Never copy private document bytes into this public code repository.

The initial asset restoration is pinned to an immutable private recovery
commit and validates the archive SHA-256 and generated-media Git blob hashes.
It imports all files transactionally, records completion in `hub_asset_imports`,
and skips subsequent downloads. It refuses path traversal, archive links,
oversized contents, and collisions with different existing bytes.

Uploads require the existing dashboard login cookie. Generated UI art remains
available for page presentation. Archived HTML is sandboxed. PDF byte ranges
remain supported. No OAuth scope or agent role is widened by this change.

## Back up and restore

1. Authenticate as the dashboard owner using the platform-stored dashboard key.
2. Download `/api/export?include_tool_calls=true`. Current exports include
   binary assets as base64 by default, plus recovery-import completion markers.
   `include_assets=false` explicitly requests an incomplete, metadata-only copy.
3. Store the export in a private off-host recovery repository or encrypted
   backup service. Record its SHA-256 and the deployed code commit. Keep older
   recovery snapshots; do not overwrite the only known-good copy.
4. On a fresh Postgres database, apply `db/00*.sql`, set `ZAI_HUB_DSN`, and run
   `python scripts/import_export.py <export.json>`. Imported asset bytes are
   checksum-verified and conflicting historical bytes are never overwritten.
5. Configure a fresh owner key/token in the hosting secret environment, then
   start `server.render_app:app`. Export files intentionally exclude OAuth
   credentials; reconnect clients or separately restore an encrypted full SQL
   backup if preserving those sessions is required.
6. Verify `/health`, authenticated counts, document links, images, MCP recall,
   and an asset-inclusive export. A database-only JSON snapshot from before
   migration 005 still requires its separate `uploads.tgz` and generated art.

For a full infrastructure backup, use `pg_dump` with platform-managed secrets;
it includes the asset and auth tables. Do not assume legacy VPS cron jobs are
running on Render. Check actual backup execution and database expiry in the
hosting dashboard.

## Rollback

The asset migration is additive. Keep its tables and all data if rolling code
back. The prior code cannot serve DB-only assets after a cold start, so roll
forward with a corrected asset reader when possible. Never reset the database
or re-import an old snapshot to repair a UI failure.

## Known boundaries

The historical private recovery code has chat-window and long-form MCP tools
not present in the public deployed server. Preserve that source and its schema;
do not assume those features were migrated just because their documentation
mentions them. Restoring them is a separately reviewed compatibility change.

The free Render service sleeps when idle; a cold-start loading screen is
hosting behavior, not evidence of missing data. Asset bytes are durable only
as long as the database and off-host backups are retained.
