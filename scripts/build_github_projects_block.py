#!/usr/bin/env python3
"""Index your GitHub repos into the Memory Hub as a portfolio block.

For each repo you own (and any org you set in OWNER_KIND): fetches
description, primary language, last push, README first 6 KB, open issues,
topics — then pushes one `memory_add_full` entry per repo with the right
tags and an auto-rendered PDF.

Idempotent — fingerprint state file at ~/.github_projects_state.json
re-pushes only on metadata change.

Requirements:
  - `gh` CLI installed + authenticated (`gh auth login`), or GH_TOKEN env.
  - `hub_client.py` next to this script (ships in scripts/).
  - A hub admin or writer bearer token visible via ZAI_HUB_TOKEN env or
    ~/.config/zai-hub/token.

Configure for yourself: edit OWNER_KIND below — map every GitHub user/org
you want indexed to a tag string ("personal", "work-org", whatever).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hub_client  # type: ignore


# ✏️ EDIT THIS: map every GitHub user/org you want indexed to a tag string.
# Repos owned by users/orgs NOT in this map are ignored.
OWNER_KIND = {
    "your-username":    "personal",
    "your-org-slug":    "work-org",
}
STATE_FILE = Path.home() / ".github_projects_state.json"


def gh_api(path: str, accept: str = "application/vnd.github+json", paginate: bool = False) -> str:
    """Call `gh api <path>` and return stdout. Empty string on 404."""
    cmd = ["gh", "api", "-H", f"Accept: {accept}", path]
    if paginate:
        cmd.append("--paginate")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            if "404" in (out.stderr or ""):
                return ""
            raise RuntimeError(f"gh api {path}: {out.stderr[:200]}")
        return out.stdout
    except subprocess.TimeoutExpired:
        return ""


def list_all_repos() -> list[dict]:
    """All repos the authenticated user can see (owned + org membership), any visibility."""
    raw = gh_api("/user/repos?visibility=all&affiliation=owner,organization_member&per_page=100",
                 paginate=True)
    if not raw:
        return []
    # paginate returns multiple JSON arrays concatenated; split on `][`
    repos = []
    chunks = raw.replace("][", "]\n[").splitlines()
    for c in chunks:
        c = c.strip()
        if not c:
            continue
        try:
            repos.extend(json.loads(c))
        except json.JSONDecodeError:
            continue

    out = []
    for r in repos:
        if r.get("fork"):  # skip forks
            continue
        owner = r["owner"]["login"]
        if owner not in OWNER_KIND:
            continue  # ignore repos outside our two orgs
        out.append({
            "name":            r["name"],
            "full_name":       r["full_name"],
            "owner":           owner,
            "owner_kind":      OWNER_KIND[owner],
            "private":         r.get("private", False),
            "archived":        r.get("archived", False),
            "description":     r.get("description") or "",
            "language":        r.get("language") or "",
            "size_kb":         r.get("size", 0),
            "stargazers":      r.get("stargazers_count", 0),
            "forks":           r.get("forks_count", 0),
            "open_issues":     r.get("open_issues_count", 0),
            "topics":          r.get("topics", []),
            "html_url":        r["html_url"],
            "ssh_url":         r["ssh_url"],
            "pushed_at":       r.get("pushed_at", ""),
            "default_branch":  r.get("default_branch", "main"),
        })
    return out


def fetch_readme(full_name: str, max_chars: int = 6000) -> str:
    raw = gh_api(f"repos/{full_name}/readme", accept="application/vnd.github.raw+json")
    if not raw:
        return ""
    return raw[:max_chars].rstrip()


def build_body(repo: dict, readme: str) -> str:
    parts = []
    parts.append(f"# {repo['full_name']}")
    parts.append("")
    if repo["description"]:
        parts.append(f"> {repo['description']}")
        parts.append("")
    badges = []
    badges.append("🔒 Private" if repo["private"] else "🌐 Public")
    if repo["archived"]:
        badges.append("📦 Archived")
    if repo["language"]:
        badges.append(f"`{repo['language']}`")
    if repo["stargazers"]:
        badges.append(f"⭐ {repo['stargazers']}")
    if repo["forks"]:
        badges.append(f"🍴 {repo['forks']}")
    if repo["open_issues"]:
        badges.append(f"❗ {repo['open_issues']} open")
    parts.append(" · ".join(badges))
    parts.append("")
    parts.append(f"**URL**: {repo['html_url']}  ")
    parts.append(f"**Clone**: `{repo['ssh_url']}`  ")
    parts.append(f"**Last push**: {repo['pushed_at'][:10] if repo['pushed_at'] else '—'}  ")
    parts.append(f"**Size**: {repo['size_kb']/1024:.1f} MB  ")
    if repo["topics"]:
        parts.append(f"**Topics**: {', '.join(repo['topics'])}  ")
    parts.append("")
    parts.append("---")
    parts.append("")
    if readme:
        parts.append("## README excerpt")
        parts.append("")
        parts.append(readme)
    else:
        parts.append("_(no README in repo)_")
    return "\n".join(parts)


def build_title(repo: dict) -> str:
    bits = [repo["full_name"]]
    if repo["description"]:
        desc = repo["description"][:90]
        bits.append(f"— {desc}")
    if repo["archived"]:
        bits.append("[archived]")
    return " ".join(bits)[:200]


def build_tags(repo: dict, owner_kind: str) -> list[str]:
    tags = ["github-project", owner_kind]
    tags.append("private" if repo["private"] else "public")
    if repo["archived"]:
        tags.append("archived")
    if repo["language"]:
        tags.append(repo["language"].lower())
    # Carry topic tags too (max 4 to avoid taxonomy theater)
    for t in repo["topics"][:4]:
        if t and t not in tags:
            tags.append(t.lower())
    return tags


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def upsert(repo: dict, owner_kind: str, state: dict, dry_run: bool) -> dict:
    """Push (or skip) one repo. Returns stats."""
    key = repo["full_name"]
    fingerprint = json.dumps({
        "p": repo["pushed_at"], "d": repo["description"],
        "a": repo["archived"], "v": repo["private"],
        "i": repo["open_issues"], "t": repo["topics"],
    }, sort_keys=True)
    if state.get(key) == fingerprint and not dry_run:
        return {"name": key, "action": "skipped", "tokens": 0}

    readme = fetch_readme(repo["full_name"])
    body = build_body(repo, readme)
    title = build_title(repo)
    tags = build_tags(repo, owner_kind)

    if dry_run:
        print(f"  WOULD PUSH  {key:50}  tags={tags}  body={len(body)}ch  readme={len(readme)}ch")
        return {"name": key, "action": "dry", "tokens": 0, "chars": len(body)}

    t0 = time.time()
    out = hub_client.push_research_full(
        title=title, body=body, tags=tags,
        importance=4, attach_pdf=True,
    )
    elapsed = time.time() - t0

    ok = out.get("ok") and out.get("result", {}).get("ok")
    err = out.get("error") or out.get("result", {}).get("error") or ""
    if not ok and "credential patterns" in err:
        # README has secret-shaped strings — push a metadata-only stub so the
        # repo still appears in the block. Don't try to redact.
        stub_body = build_body({**repo, "description":
                                repo["description"] + " [README withheld — secret patterns detected]"}, "")
        out = hub_client.push_research_full(
            title=title, body=stub_body, tags=tags + ["readme-withheld"],
            importance=4, attach_pdf=True,
        )
        ok = out.get("ok") and out.get("result", {}).get("ok")
        err = out.get("error") or out.get("result", {}).get("error") or ""

    if ok:
        r = out["result"]
        state[key] = fingerprint
        print(f"  ✓ {key:50}  {r['memory_id'][:8]}  "
              f"{r['body_chars']}ch  pdf={r.get('pdf_bytes',0)/1024:.0f}KB  "
              f"{elapsed:.1f}s")
        return {"name": key, "action": "pushed", "elapsed": elapsed,
                "chars": r["body_chars"], "pdf_bytes": r.get("pdf_bytes", 0)}
    else:
        print(f"  ✗ {key}: {err}", file=sys.stderr)
        return {"name": key, "action": "failed", "error": err}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reset-state", action="store_true",
                    help="ignore state file, re-push every repo")
    args = ap.parse_args()

    if args.reset_state and STATE_FILE.exists():
        STATE_FILE.unlink()

    state = _load_state()
    all_stats = []
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[github_projects] starting at {started}")

    repos = list_all_repos()
    by_owner = {}
    for r in repos:
        by_owner.setdefault(r["owner"], []).append(r)

    for owner, repo_list in sorted(by_owner.items()):
        kind = OWNER_KIND[owner]
        print(f"\n--- {owner} ({kind}) — {len(repo_list)} repos ---")
        for repo in repo_list:
            stats = upsert(repo, kind, state, args.dry_run)
            all_stats.append(stats)

    if not args.dry_run:
        _save_state(state)

    pushed  = [s for s in all_stats if s["action"] == "pushed"]
    skipped = [s for s in all_stats if s["action"] == "skipped"]
    failed  = [s for s in all_stats if s["action"] == "failed"]

    total_chars = sum(s.get("chars", 0) for s in pushed)
    total_pdf   = sum(s.get("pdf_bytes", 0) for s in pushed)
    total_time  = sum(s.get("elapsed", 0) for s in pushed)

    print()
    print("=" * 60)
    print(f"  pushed:  {len(pushed)}")
    print(f"  skipped: {len(skipped)} (unchanged)")
    print(f"  failed:  {len(failed)}")
    print(f"  total body chars: {total_chars:,}")
    print(f"  total PDF bytes : {total_pdf:,} ({total_pdf/1024/1024:.2f} MB)")
    print(f"  total wall time : {total_time:.1f}s")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
