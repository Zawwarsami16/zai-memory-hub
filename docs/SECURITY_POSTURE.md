# ZAI Memory Hub — Security Posture

Last audited: 2026-05-21.

## What's protected today

| Layer | Method | Status |
|-------|--------|--------|
| **In-transit (agent ↔ hub)** | Caddy auto-TLS, Let's Encrypt, HTTPS only | ✅ |
| **In-transit (Postgres connections)** | SSL on, scram-sha-256 password hashing | ✅ |
| **Authentication** | Per-agent bearer tokens, sha256-hashed in DB, never logged | ✅ |
| **Authorization** | Per-role tool catalog filter + per-call `_require_role` | ✅ |
| **Attribution** | Server stamps `written_by` from token row; client cannot lie | ✅ |
| **Secret leak (write path)** | 14-pattern scanner blocks credentials in `memory_add` + `decision_log`, audit-logged | ✅ |
| **Soft delete + audit** | Every mutation logged; only the human's dashboard can hard-delete | ✅ |
| **B2 backup encryption** | Backblaze B2 server-side encryption (AES-256, SSE-B2) is the default | ✅ |
| **GitHub repo backup** | Repo is private; auth via personal access token | ✅ |

## What's NOT protected (honest gaps)

| Gap | Threat model | Severity for this hub |
|-----|--------------|----------------------|
| **Disk-level encryption on VPS** | Provider snapshot, disk seizure, hypervisor compromise → reads plain ext4 | Low for current use (memory content isn't classified). Becomes higher if hub stores PII or secrets the user actually saved (which the PII scanner already blocks at write time). |
| **Column-level encryption in Postgres** | Same as above — `memory.content` is stored as plain text | Low for the above reason. |
| **Provider-side full-disk encryption** | Same | Depends on VPS provider's policy. Current host: generic QEMU, no explicit encryption guarantee. |

## Practical assessment

For the actual threat model — a homeless-traveler personal hub on a single VPS — the data **is not classified or regulated**, the PII scanner blocks credential leaks at write time, and the bearer token is the real barrier. Migrating Postgres to a LUKS-encrypted partition would buy marginal protection at significant operational cost (downtime, restore-from-backup risk).

**Recommendation: don't migrate to LUKS now.** Re-evaluate if either of these becomes true:
- The hub starts storing data the user would care about leaking (medical, financial, identity)
- The user moves to a VPS provider that doesn't offer disk encryption by default

## Migration paths (when ready)

### Option 1: LUKS on the Postgres data partition (~30 min downtime)

```bash
# 1. Full B2 backup + verify it restores cleanly
./scripts/backup_to_b2.py
# 2. Stop services
sudo systemctl stop zai-hub-mcp zai-hub-dashboard postgresql
# 3. Move data dir aside
sudo mv /var/lib/postgresql/16/main /var/lib/postgresql/16/main.plain
# 4. Create a LUKS container at /var/lib/postgresql/16/main.luks
sudo cryptsetup luksFormat /var/lib/postgresql/16/main.luks   # passphrase prompted
sudo cryptsetup open /var/lib/postgresql/16/main.luks zai_pg
sudo mkfs.ext4 /dev/mapper/zai_pg
sudo mount /dev/mapper/zai_pg /var/lib/postgresql/16/main
sudo chown postgres:postgres /var/lib/postgresql/16/main
# 5. Restore data
sudo cp -a /var/lib/postgresql/16/main.plain/. /var/lib/postgresql/16/main/
# 6. Start services
sudo systemctl start postgresql zai-hub-mcp zai-hub-dashboard
# 7. Verify everything works
curl https://hub.example.com/health
# 8. Once verified, securely wipe the plaintext copy
sudo shred -fzu /var/lib/postgresql/16/main.plain/*
sudo rm -rf /var/lib/postgresql/16/main.plain
# 9. Add to /etc/crypttab so it unlocks on boot (with passphrase or keyfile)
```

**Watch out:** the passphrase must be entered (or unlocked via keyfile) on every boot. Auto-unlock with a keyfile defeats the point unless the keyfile is itself protected. Realistically, a passphrase prompted on boot means *you need physical/console access on every reboot* — that's the cost of LUKS.

### Option 2: pgcrypto column-level (drop-in, no downtime)

Apply this migration:

```sql
-- Encrypt the `content` column of memories using a key from env var
-- Read at app startup time (ZAI_HUB_PGCRYPTO_KEY)
-- This is application-layer; Postgres still sees ciphertext on disk.
-- Search becomes limited (no ILIKE on encrypted columns - must decrypt first).
```

**Watch out:** breaks `memory.recall` substring search. Would need to combine with Voyage embeddings on plaintext computed at write time, stored separately. Adds complexity.

### Option 3: Change VPS provider

Some providers offer block-device encryption transparently:
- **Hetzner Cloud** — encrypted volumes available
- **DigitalOcean** — encrypted droplets in select regions
- **Vultr** — VFS encryption available

Migration: `./scripts/backup_to_b2.py` from current → spin up new VPS with encrypted root → restore from B2 → DNS cutover. ~1 hour of focused work.

## Defensive layers in priority order

If you're hardening the hub from the most-likely attack to least-likely:

1. **Bearer token leakage** — biggest real risk. Mitigation: don't paste tokens into logs, rotate via `/agents` panel if you suspect leak, agent_tokens table makes this a one-click revoke. ✅ shipped.
2. **Agent writes credentials by mistake** — Mitigation: server-side PII scanner. ✅ shipped.
3. **Disk seizure of running VPS** — low likelihood, low impact for this data class. Could mitigate via LUKS later.
4. **Provider hypervisor compromise** — extremely low likelihood, no practical mitigation other than not using cloud at all.

## What to test if encryption matters

If you decide encryption-at-rest is critical:

```bash
# 1. Verify B2 server-side encryption is on
b2 bucket-info zai-hub-backups-zawwarsami16 | grep -i encryption
# 2. Check that dump files are unreadable without B2 credentials
# 3. Test the LUKS restore cycle on a staging VPS BEFORE doing it live
```
