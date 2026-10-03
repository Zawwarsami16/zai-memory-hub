"""ZAI Memory Hub — zero-cost Render adapter.

Durable storage model:
- Base snapshot: private GitHub recovery repo (state/hub-export.json)
- New writes: append-only JSON files in state/live-render/
- Runtime: in-memory merged view for fast recall

This keeps the Hub usable on a free Render web service without a persistent disk
or database. GitHub remains the durable, versioned source of truth.
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI
from fastmcp import FastMCP


GITHUB_TOKEN = os.environ["GITHUB_TOKEN"].strip()
BASE_REPO = os.environ.get("ZAI_BASE_REPO", "Zawwarsami16/zai-personal-hub")
BASE_PATH = os.environ.get("ZAI_BASE_PATH", "state/hub-export.json")
LIVE_REPO = os.environ.get("ZAI_LIVE_REPO", BASE_REPO)
LIVE_PREFIX = os.environ.get("ZAI_LIVE_PREFIX", "state/live-render").strip("/")
MCP_PREFIX = "/" + os.environ.get("ZAI_MCP_PREFIX", "mcp").strip("/")
PUBLIC_URL = os.environ.get("ZAI_HUB_PUBLIC_URL", "").rstrip("/")
ACTOR = os.environ.get("ZAI_HUB_ACTOR", "chatgpt-render")
GITHUB_API = "https://api.github.com"
TIMEOUT = 30.0

_lock = threading.RLock()
_state: dict[str, list[dict[str, Any]]] = {
    "entities": [],
    "memories": [],
    "decisions": [],
    "interactions": [],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _headers(raw: bool = False) -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "zai-memory-hub-render",
    }
    h["Accept"] = "application/vnd.github.raw+json" if raw else "application/vnd.github+json"
    return h


def _gh_get_raw(repo: str, path: str) -> str:
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    r = httpx.get(url, headers=_headers(raw=True), timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _gh_list(repo: str, path: str) -> list[dict[str, Any]]:
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    r = httpx.get(url, headers=_headers(), timeout=TIMEOUT)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    payload = r.json()
    return payload if isinstance(payload, list) else [payload]


def _gh_put_json(repo: str, path: str, payload: dict[str, Any], message: str) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    data = {
        "message": message[:240],
        "content": base64.b64encode(body).decode("ascii"),
    }
    url = f"{GITHUB_API}/repos/{repo}/contents/{path}"
    r = httpx.put(url, headers=_headers(), json=data, timeout=TIMEOUT)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub persistence failed ({r.status_code}): {r.text[:300]}")


def _load_overlay_dir(kind: str) -> list[dict[str, Any]]:
    path = f"{LIVE_PREFIX}/{kind}"
    out: list[dict[str, Any]] = []
    for item in _gh_list(LIVE_REPO, path):
        if item.get("type") != "file" or not str(item.get("name", "")).endswith(".json"):
            continue
        try:
            out.append(json.loads(_gh_get_raw(LIVE_REPO, item["path"])))
        except Exception:
            continue
    return out


def _merge_by_id(base: list[dict[str, Any]], overlay: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in base + overlay:
        key = str(item.get("id") or item.get("slug") or uuid.uuid4())
        merged[key] = item
    return list(merged.values())


def load_state() -> None:
    base = json.loads(_gh_get_raw(BASE_REPO, BASE_PATH))
    with _lock:
        for kind in ("entities", "memories", "decisions", "interactions"):
            base_items = base.get(kind) or []
            overlay_items = _load_overlay_dir(kind)
            _state[kind] = _merge_by_id(base_items, overlay_items)


def _persist(kind: str, item: dict[str, Any]) -> None:
    ident = str(item.get("id") or item.get("slug") or uuid.uuid4())
    path = f"{LIVE_PREFIX}/{kind}/{ident}.json"
    _gh_put_json(LIVE_REPO, path, item, f"hub: append {kind[:-1]} {ident[:12]}")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_\-]{2,}", (text or "").lower()))


def _memory_score(memory: dict[str, Any], query: str, wanted_tags: Optional[list[str]]) -> float:
    content = str(memory.get("content") or "")
    tags = [str(x).lower() for x in (memory.get("tags") or [])]
    q = _tokens(query)
    c = _tokens(content)
    overlap = len(q & c)
    phrase = 4.0 if query and query.lower() in content.lower() else 0.0
    tag_score = 0.0
    if wanted_tags:
        wanted = {x.lower() for x in wanted_tags}
        tag_score = 2.0 * len(wanted & set(tags))
        if wanted and not (wanted & set(tags)):
            return -1.0
    importance = float(memory.get("importance") or 3) * 0.08
    return overlap + phrase + tag_score + importance


def _recent(items: list[dict[str, Any]], n: int, key: str = "created_at") -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: str(x.get(key) or ""), reverse=True)[: max(1, min(n, 100))]


def _brief_memory(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m.get("id"),
        "content": str(m.get("content") or "")[:1200],
        "tags": m.get("tags") or [],
        "written_by": m.get("written_by"),
        "importance": m.get("importance"),
        "created_at": m.get("created_at"),
        "entity_slugs": m.get("entity_slugs") or [],
    }


mcp = FastMCP(
    "zai-memory-hub",
    instructions=(
        "Shared persistent memory for Zawwar Sami and his AI assistants. "
        "Call context_bootstrap once when useful. Append rather than overwrite. "
        "Use decision_log for durable course corrections."
    ),
)


@mcp.tool()
def hub_status() -> dict:
    """Return current restored-memory counts and storage mode."""
    with _lock:
        return {
            "ok": True,
            "mode": "github-backed-free",
            "base_repo": BASE_REPO,
            "base_snapshot": BASE_PATH,
            "live_repo": LIVE_REPO,
            "counts": {k: len(v) for k, v in _state.items()},
            "actor": ACTOR,
            "public_url": PUBLIC_URL or None,
        }


@mcp.tool()
def context_bootstrap(your_slug: Optional[str] = None) -> dict:
    """Load a compact orientation: latest decisions, memories, entities and counts."""
    with _lock:
        decisions = _recent(_state["decisions"], 8)
        memories = [_brief_memory(x) for x in _recent(_state["memories"], 16)]
        entities = _recent(_state["entities"], 25, "updated_at")
        return {
            "ok": True,
            "requested_slug": your_slug,
            "actor": ACTOR,
            "counts": {k: len(v) for k, v in _state.items()},
            "latest_decisions": decisions,
            "latest_memories": memories,
            "entities": [
                {
                    "slug": e.get("slug"),
                    "kind": e.get("kind"),
                    "display": e.get("display"),
                    "updated_at": e.get("updated_at"),
                }
                for e in entities
            ],
        }


@mcp.tool()
def memory_get_recent(n: int = 10, written_by: Optional[str] = None) -> dict:
    """Return latest memories, optionally restricted to one author."""
    with _lock:
        items = _state["memories"]
        if written_by:
            items = [m for m in items if m.get("written_by") == written_by]
        return {"ok": True, "items": [_brief_memory(x) for x in _recent(items, n)]}


@mcp.tool()
def memory_recall(
    query: str,
    k: int = 5,
    tags: Optional[list[str]] = None,
    full: bool = False,
) -> dict:
    """Search restored memories using lexical + tag + importance scoring."""
    with _lock:
        scored = []
        for m in _state["memories"]:
            score = _memory_score(m, query, tags)
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: (x[0], str(x[1].get("created_at") or "")), reverse=True)
        top = [m for _, m in scored[: max(1, min(k, 50))]]
        if not full:
            top = [_brief_memory(m) for m in top]
        return {"ok": True, "query": query, "count": len(top), "items": top}


@mcp.tool()
def memory_add(
    content: str,
    tags: Optional[list[str]] = None,
    importance: int = 3,
    entity_slugs: Optional[list[str]] = None,
) -> dict:
    """Append a durable memory to the private GitHub-backed live layer."""
    content = (content or "").strip()
    if not content:
        return {"ok": False, "error": "content is required"}
    item = {
        "id": str(uuid.uuid4()),
        "content": content,
        "tags": tags or [],
        "entity_slugs": entity_slugs or [],
        "written_by": ACTOR,
        "session_id": "render-live",
        "importance": max(1, min(int(importance), 5)),
        "deleted_at": None,
        "deleted_by": None,
        "created_at": _now(),
    }
    _persist("memories", item)
    with _lock:
        _state["memories"].append(item)
    return {"ok": True, "id": item["id"], "persisted": "private-github"}


@mcp.tool()
def memory_add_full(
    content: str,
    tags: Optional[list[str]] = None,
    importance: int = 4,
    entity_slugs: Optional[list[str]] = None,
) -> dict:
    """Append a long-form durable memory. Content is preserved as supplied."""
    if len(content.encode("utf-8")) > 200_000:
        return {"ok": False, "error": "content exceeds 200 KB"}
    return memory_add(content, tags, importance, entity_slugs)


@mcp.tool()
def decision_log(
    summary: str,
    rationale: str,
    alternatives: Optional[str] = None,
    entity_slugs: Optional[list[str]] = None,
    supersedes: Optional[str] = None,
) -> dict:
    """Append a durable decision with rationale and optional supersession link."""
    item = {
        "id": str(uuid.uuid4()),
        "summary": (summary or "").strip(),
        "rationale": (rationale or "").strip(),
        "alternatives": alternatives,
        "entity_slugs": entity_slugs or [],
        "written_by": ACTOR,
        "supersedes": supersedes,
        "deleted_at": None,
        "deleted_by": None,
        "created_at": _now(),
    }
    if not item["summary"] or not item["rationale"]:
        return {"ok": False, "error": "summary and rationale are required"}
    _persist("decisions", item)
    with _lock:
        _state["decisions"].append(item)
    return {"ok": True, "id": item["id"], "persisted": "private-github"}


@mcp.tool()
def entity_upsert(
    slug: str,
    kind: str,
    display: str,
    metadata: Optional[dict[str, Any]] = None,
) -> dict:
    """Create or update a live entity in the GitHub-backed overlay."""
    clean = re.sub(r"[^a-z0-9\-]+", "-", (slug or "").lower()).strip("-")
    if not clean:
        return {"ok": False, "error": "valid slug required"}
    existing = None
    with _lock:
        for e in _state["entities"]:
            if e.get("slug") == clean:
                existing = e
                break
    item = {
        "id": (existing or {}).get("id") or str(uuid.uuid4()),
        "slug": clean,
        "kind": kind,
        "display": display,
        "metadata": metadata or {},
        "created_at": (existing or {}).get("created_at") or _now(),
        "updated_at": _now(),
    }
    _persist("entities", item)
    with _lock:
        _state["entities"] = [e for e in _state["entities"] if e.get("slug") != clean] + [item]
    return {"ok": True, "entity": item}


@mcp.tool()
def entity_neighborhood(slug: str, k: int = 12) -> dict:
    """Return an entity plus memories/decisions that reference its slug."""
    with _lock:
        entity = next((e for e in _state["entities"] if e.get("slug") == slug), None)
        memories = [
            _brief_memory(m)
            for m in _state["memories"]
            if slug in (m.get("entity_slugs") or [])
        ]
        decisions = [
            d for d in _state["decisions"]
            if slug in (d.get("entity_slugs") or [])
        ]
        return {
            "ok": entity is not None,
            "entity": entity,
            "memories": _recent(memories, k),
            "decisions": _recent(decisions, k),
        }


@mcp.tool()
def interaction_log(surface: str, summary: Optional[str] = None, metadata: Optional[dict[str, Any]] = None) -> dict:
    """Append a lightweight session/interaction marker."""
    item = {
        "id": str(uuid.uuid4()),
        "session_id": "render-live",
        "surface": surface,
        "summary": summary,
        "metadata": metadata or {},
        "started_at": _now(),
    }
    _persist("interactions", item)
    with _lock:
        _state["interactions"].append(item)
    return {"ok": True, "id": item["id"]}


# Load restored memory once per Render process.
load_state()

mcp_app = mcp.http_app(path="/", stateless_http=True)
app = FastAPI(title="ZAI Memory Hub — Free Restore", lifespan=mcp_app.lifespan)


@app.get("/health")
def health() -> dict:
    with _lock:
        return {
            "ok": True,
            "service": "zai-memory-hub",
            "storage": "private-github",
            "memories": len(_state["memories"]),
            "decisions": len(_state["decisions"]),
            "entities": len(_state["entities"]),
            "mcp_path": MCP_PREFIX,
        }


app.mount(MCP_PREFIX, mcp_app)
