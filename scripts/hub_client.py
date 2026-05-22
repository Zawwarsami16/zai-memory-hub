"""Stdlib-only MCP client for the ZAI Memory Hub — drop-in for plugins
and one-off scripts that want to push memories without pulling in a
larger SDK. Fail-soft: returns dicts, never raises.

Configuration (any one of):
  - env ZAI_HUB_URL_LOCAL (default: http://127.0.0.1:8765/mcp)
  - env ZAI_HUB_TOKEN (the bearer)
  - file ~/.config/zai-hub/token (the bearer)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional


_HUB_URL_DEFAULT = "http://127.0.0.1:8765/mcp"
_TOKEN_PATHS = [
    Path.home() / ".config" / "zai-hub" / "token",
    Path.home() / ".zai" / "auth" / "hub.token",  # ZAI runtime convention
]
_TIMEOUT = 30


def _hub_url() -> str:
    return os.environ.get("ZAI_HUB_URL_LOCAL") or _HUB_URL_DEFAULT


def _hub_token() -> Optional[str]:
    if t := os.environ.get("ZAI_HUB_TOKEN"):
        return t.strip()
    for p in _TOKEN_PATHS:
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or None
    return None


def _parse_sse(body: bytes) -> dict:
    for line in body.decode("utf-8", errors="replace").splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    continue
    return {}


def _post(url: str, payload: dict, headers: dict) -> tuple[int, dict, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
            sid = resp.headers.get("mcp-session-id", "") or resp.headers.get("Mcp-Session-Id", "")
            ctype = resp.headers.get("content-type", "")
            if "text/event-stream" in ctype:
                return resp.status, _parse_sse(raw), sid
            try:
                return resp.status, json.loads(raw.decode("utf-8")), sid
            except json.JSONDecodeError:
                return resp.status, {"raw": raw.decode("utf-8", errors="replace")}, sid
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        return e.code, {"error": body or str(e)}, ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": str(e)}, ""


def _call_tool(tool: str, arguments: dict) -> dict:
    token = _hub_token()
    if not token:
        return {"ok": False, "error": "no hub token (set ZAI_HUB_TOKEN or ~/.config/zai-hub/token)"}

    url = _hub_url()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "hub-client", "version": "0.1"}},
    }
    status, _resp, sid = _post(url, init_payload, headers)
    if status != 200 or not sid:
        return {"ok": False, "error": f"initialize failed (status={status})"}

    sess_headers = {**headers, "Mcp-Session-Id": sid}
    _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sess_headers)

    call_payload = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    status, resp, _ = _post(url, call_payload, sess_headers)
    if status != 200:
        return {"ok": False, "error": f"tools/call status={status}: {str(resp.get('error', ''))[:200]}"}
    result = resp.get("result") or {}
    if result.get("isError"):
        content = result.get("content", [])
        err = content[0].get("text", "") if content else "tool reported error"
        return {"ok": False, "error": err[:500]}
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        try:
            return {"ok": True, "result": json.loads(content[0]["text"])}
        except json.JSONDecodeError:
            return {"ok": True, "result": {"text": content[0]["text"]}}
    return {"ok": True, "result": result}


def push_research_full(title: str, body: str, tags: Optional[list[str]] = None,
                       importance: int = 4, attach_pdf: bool = True) -> dict:
    """Push a long-form memory with optional auto-rendered PDF. Fail-soft."""
    tags = list(tags or [])
    try:
        out = _call_tool("memory_add_full", {
            "title": title[:200], "body": body, "tags": tags,
            "importance": importance, "attach_pdf": attach_pdf,
        })
    except Exception as e:
        return {"ok": False, "error": f"client crashed: {e}"}
    if not out.get("ok"):
        print(f"[hub_client] push_research_full failed: {out.get('error')}", file=sys.stderr)
    return out


def push_memory_atomic(content: str, tags: Optional[list[str]] = None,
                       importance: int = 3, kind: str = "note") -> dict:
    """One-line atomic memory_add. Fail-soft."""
    try:
        return _call_tool("memory_add", {
            "content": content, "tags": list(tags or []),
            "importance": importance, "kind": kind,
        })
    except Exception as e:
        return {"ok": False, "error": str(e)}


__all__ = ["push_research_full", "push_memory_atomic"]
