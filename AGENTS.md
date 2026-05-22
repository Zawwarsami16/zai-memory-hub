# AGENTS.md

If you're an AI agent connecting to this hub for the first time, read this file end-to-end before you write anything. It's short.

## What this is

A shared Postgres-backed memory store for one human (me) and any number of AI agents I run on my behalf. The hub lives at `https://<your-domain>/`, behind a single bearer token. Everything you write is visible on the dashboard the next time I open it.

The hub is **append-mostly**. Writes always create new rows. Edits to existing memories aren't a thing - if you find a memory that's wrong, write a new one that supersedes it. Deletes are soft and recoverable. See the soft-delete section below.

## How to connect

MCP over HTTP. Bearer token in the `Authorization` header.

```
URL:    https://<your-domain>/mcp
Header: Authorization: Bearer <token>
```

Set three env vars on your machine. Once, not per session.

```
ZAI_HUB_URL=https://<your-domain>/mcp
ZAI_HUB_TOKEN=<bearer token>
ZAI_HUB_WRITTEN_BY=<a stable slug that identifies you>
```

The slug matters. It's how I know which agent wrote what, and it's the key for your panel on the Active Agents row. Pick something descriptive and stable: `local-claude`, `cursor-laptop`, `phone-claude`. Don't change it across sessions.

## Tools

All exposed via MCP, all defined in `server/hub.py`.

```
context.bootstrap     one-call orientation when you first connect
memory.add            write a new one-line atomic memory (~110-char headline)
memory.add_full       save long-form content verbatim + auto-rendered PDF
memory.recall         search by text (Voyage-backed when embeddings are wired)
memory.get_recent     the latest N, optionally filtered by author or tag
memory.delete         soft-delete a memory you wrote
decision.log          a durable choice with rationale + alternatives
entity.upsert         create or update an entity (project, person, thread)
entity.neighborhood   walk the graph from any entity
interaction.log       mark a session start or turning point
chat_window.create    dump a ~500-line conversation, 10-day TTL, pinnable
chat_window.list      list recent windows
chat_window.get       fetch one window's full transcript
chat_window.pin       keep past TTL (admin)
chat_window.delete    soft-delete a window (admin)
```

There is no `memory.edit`. There is no hard delete via MCP. There is no admin command.

### When to use which write tool

```
Human's intent                              → Tool
─────────────────────────────────────────────────────────────────────
"remember this" / "save"                    → memory.add
"save as is" / "with context" / "verbatim"  → memory.add_full
"save this conversation"                    → chat_window.create
"this is the rule going forward"            → decision.log
"new project / concept / person"            → entity.upsert
"I'm starting a new thread"                 → interaction.log
```

The save-with-context rule: when the human uses phrases like "as is", "with context", "proper memory mein daalo", "save all of this" — they mean preserve verbatim, don't summarize. Use `memory.add_full(title, body, attach_pdf=True)`. PDF gets rendered and linked from the memory card automatically.

## What to do as a new agent

In this order:

1. Call `memory.get_recent(n=20)`. Read what other agents have written in the last day. You'll learn current focus, recent shipments, pending work, and conventions in use.
2. Pull the last 10 decisions. These are the load-bearing course-corrections you must respect.
3. Check `interaction.log` for the last hour. If another agent claimed a task, don't duplicate it.
4. Log your own arrival with `interaction.log(surface="<your-slug>", summary="connected, picking up <task>")`.
5. Start working. Write memories as you go, not at the end.

## Etiquette

Six rules. None of them are subtle.

**1. Append, don't overwrite.** Found an old memory that's wrong? Write a new one that says "supersedes `<id>`: <corrected content>". Don't try to mutate the original. The whole hub is built on this invariant.

**2. Only delete your own memories.** Use `memory.delete` only when `written_by` matches your slug. If a memory from another agent really needs to go, write a `decision.log` first, coordinate with the human, then delete. Reading other agents' memories is encouraged; quietly removing them is not.

**3. Claim long-running work before you start it.** Before spending more than 5 minutes on something, log an interaction: `interaction.log(surface="<slug>", summary="working on X for the next ~Y minutes")`. The next agent that connects will see it and pick something else.

**4. Stay in your lane.** Tag memories with the vocabulary below. If you're an HTB agent, tag with `htb` / `ctf` / etc. If you're shipping dashboard features, tag `hub` / `ui` / `milestone`. Tags are how memories surface in the right knowledge block.

**5. `decision.log` is for course-corrections.** Anything that changes what other agents should be doing - a pivot, an abandoned approach, a chosen tradeoff - gets a decision log entry with rationale + alternatives. Decisions are how multiple agents stay coherent across time.

**6. Soft delete is real delete in the UI.** Memories you soft-delete stop appearing in feeds, recall, knowledge blocks. They sit in `/trash` until the human permanently removes them. For your purposes, treat soft-delete as the only delete you have.

## Soft delete + audit

Every mutation goes into an `audit_log` table (`target_kind`, `target_id`, `action`, `actor`, `detail`, `created_at`). Soft delete sets `deleted_at = now()` + `deleted_by = <actor>` and is reversible in one click from the dashboard `/trash` route.

Hard delete is dashboard-only, never exposed via MCP. There is no way for an agent to permanently destroy data.

## Tag vocabulary

The dashboard's knowledge blocks each filter on a tag set. If you want your memory to show up in a block, use one of these tags.

| Block            | Tags                                                                                                                          |
|------------------|-------------------------------------------------------------------------------------------------------------------------------|
| Philosophy       | philosophy, draft, idea, thought, thinking, essay, note                                                                       |
| Hacking & CTF    | htb, ctf, pwn, exploit, recon, payload, shell, reverse, web-ex, binary, rop, buffer-overflow, rce, sqli, xss, lfi, rfi, priv-esc, pivot, active-directory |
| Crypto & Markets | crypto, market, trade, liquidity, regime, macro, btc, eth, framework                                                          |
| Infrastructure   | infra, vps, mcp, systemd, pipeline, deploy, config, tech-debt, state                                                          |
| Now Building     | milestone, ship, in-flight, ui, feature, build                                                                                |
| GitHub Projects  | `github-project` (use `scripts/build_github_projects_block.py` to populate)                                                   |
| Documents        | `document` (auto-added by `memory.add_full` / PDF upload; don't set manually)                                                 |
| Chats            | _(kind: chats, not tag — written by `chat_window.create`)_                                                                    |

If your memory genuinely fits none of those, write it anyway with no tags. It still shows in the Timeline and recent feeds. Tags are for discoverability, not gatekeeping.

## Quality bar

A few rules that keep the hub from becoming noise:

- No empty memories. "test" / "hello" memories get soft-deleted in the same session that wrote them.
- First sentence is the headline. The dashboard shows ~110 chars as a card title; lead with the conclusion, not the setup.
- Six tags is plenty. More than that is taxonomy theater.
- Importance defaults to 3. 1 = ephemeral, 5 = load-bearing / must-not-be-lost. Be honest.
- Reference other memories by UUID inside your content (`see <uuid>`). The dashboard relation map picks this up.

## When in doubt

Write a `decision.log` entry. Future agents (including future you) will thank you.
