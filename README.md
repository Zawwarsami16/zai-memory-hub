# zai-memory-hub

A self-hosted memory store I built so that every AI assistant I use writes to the same Postgres. Claude Code on my VPS, Claude Code on my laptop, Cursor, a phone client, a hand-rolled Python script - they all hit one MCP endpoint and share one bearer token. The dashboard is where I actually go look at what they've written.

I'm putting it up because friends kept asking how the agent-shared memory works. The repo is the skeleton: fork it, point it at your own Postgres, fill it with your own context. Don't import my memories - the whole point is that the data is yours.

![dashboard hero](docs/screenshot-home.png)

## Why this exists

Most "memory" features for LLM tools are scoped to one client. Claude.ai web doesn't see what Claude Code on my VPS wrote yesterday. Cursor doesn't know I already decided not to use Redis. The fix everyone keeps re-inventing is some flavor of "stuff a markdown file in a folder and load it on session start," which works until you have more than one device or more than one assistant.

This is the version of that I actually use. One Postgres, one MCP endpoint, one bearer token, a dashboard with a delete button. Everything is append-mostly: agents can write and soft-delete, but only I (through the dashboard) can hard-delete. The audit log tracks every mutation.

## What you get

- `memory.add` / `memory.recall` / `memory.get_recent` over MCP. Cheap to call, indexed by tag, optionally filtered by author or entity.
- `decision.log` for course-corrections that other agents should respect. ("We tried Redis, it's overkill, we're sticking with Postgres LISTEN/NOTIFY.")
- `interaction.log` so concurrent agents can see who's working on what without stepping on each other.
- `memory.delete` (soft, recoverable, audited). There's no hard delete from MCP. That's the rule, not a TODO.
- A dashboard at `/` with knowledge blocks (Philosophy, Hacking & CTF, Crypto & Markets, Infrastructure, Now Building), a timeline, a per-agent recent-activity row, a Library view for PDFs, and a `/trash` page with restore + full audit log.
- Off-site nightly backup to Backblaze B2 (optional). Hourly state backups to a git remote (optional).
- A `/api/export` endpoint that streams the whole hub as one JSON file plus an `import_export.py` script that re-imports idempotently. UUIDs are preserved, so when you move boxes you don't lose foreign-key references.

## What it costs to run

For one person plus a handful of agents:

- VPS: $5-10/mo (any 2GB Linux box. I'm on a $7 instance.)
- Domain: ~$12/yr.
- Postgres: $0, runs on the VPS.
- B2 backup: a few cents a month at most.
- Replicate cover art (optional): ~$0.04 per generated cover.
- Voyage embeddings (optional, for semantic recall): free tier covers a single user comfortably.

Everything past VPS + domain is optional.

## Quick install

Assumes Ubuntu 24.04 with a domain pointed at the box. You'll need root once.

```bash
git clone https://github.com/Zawwarsami16/zai-memory-hub.git
cd zai-memory-hub
cp .env.example .env
$EDITOR .env
```

Edit `.env` and at minimum set: `ZAI_HUB_DSN`, `ZAI_HUB_AGENT_TOKEN` (generate a long random one), `ZAI_HUB_DASHBOARD_KEY` (separate from the agent token), `ZAI_HUB_PUBLIC_URL`.

Then Postgres:

```bash
sudo apt install -y postgresql-16 postgresql-16-pgvector
sudo -u postgres psql <<SQL
CREATE USER zai_hub WITH PASSWORD 'change-me';
CREATE DATABASE zai_hub OWNER zai_hub;
SQL
sudo -u postgres psql -d zai_hub -f db/001_init.sql
sudo -u postgres psql -d zai_hub -f db/002_soft_delete_audit.sql
```

Then Python:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
set -a; source .env; set +a
.venv/bin/python server/run_http.py &
.venv/bin/python -m uvicorn dashboard.app:app --host 127.0.0.1 --port 8766 &
```

For production: put Caddy in front (sample in `deploy/Caddyfile.example`), run both services under systemd (sample units in `deploy/`), and schedule `scripts/backup_to_b2.py` as a nightly cron.

## Connecting an agent

Three env vars on the agent's machine:

```
ZAI_HUB_URL=https://hub.example.com/mcp
ZAI_HUB_TOKEN=<bearer token from .env>
ZAI_HUB_WRITTEN_BY=<stable slug for this agent>
```

Claude Code:

```bash
claude mcp add zai-hub --transport http \
  --url https://hub.example.com/mcp \
  --header "Authorization: Bearer $ZAI_HUB_TOKEN"
```

Any MCP client that speaks Streamable-HTTP works the same way. The first time an agent connects, point it at `AGENTS.md` - that file is the etiquette that keeps two agents from clobbering each other.

## Screenshots

The Library view (knowledge blocks, recent uploads, trending tags, latest decision):

![dashboard blocks](docs/screenshot-blocks.png)

The trash + audit page (every mutation, with restore):

![trash page](docs/screenshot-trash.png)

## Moving your hub to a new box

Two commands. The exporter streams (no memory pressure even on big dumps), the importer is idempotent.

```bash
# old host
curl -b cookies.txt https://old.example.com/api/export -o hub-export.json
tar czf uploads.tgz dashboard/static/uploads/

# new host (fresh install of this repo, then)
ZAI_HUB_DSN='host=... dbname=zai_hub user=... password=...' \
  python scripts/import_export.py hub-export.json
tar xzf uploads.tgz -C ./
```

UUIDs are preserved, so foreign-key references survive.

## Security model

- One bearer token gates the entire MCP endpoint, enforced at the Caddy edge. Lose the token, lose the hub. Treat it like a password.
- A separate dashboard key gates the web UI. Different surface, different blast radius, different cookie.
- No anonymous writes. No public read.
- `auth/` is gitignored. `*.token` is gitignored. `.env` is gitignored. The `.gitignore` is paranoid on purpose.
- The only hard delete is dashboard-only and audited. There's no way for an agent to permanently destroy data.

## What's intentionally not here

- Multi-tenancy. This is a single-person tool. If you fork it for a team, the bearer token model needs replacing.
- A reset / wipe button. The export+restart path is the wipe path.
- Edits to existing memories. Append a superseding memory; don't mutate.
- Anything Slack-like (channels, presence, threads). Interactions are a log, not a chat.

## Roadmap

Stuff I'm working on next, in roughly the order I'll get to it:

- Voyage embeddings on `memory.recall` so it stops being a substring search. (Have to migrate the existing rows in batches.)
- A read-only public-share mode for individual memories (signed URL, time-boxed).
- A small CLI (`zai-hub recall "what was that thing about..."`) so I don't need MCP for one-shot queries from a terminal.

If you fork this and add something I'd want, send a PR.

## License

MIT. See [LICENSE](LICENSE).
