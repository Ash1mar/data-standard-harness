"""Loopback-only HTTP/SSE adapter. The FastAPI process owns business state.

This module deliberately does not import backend.tools. Business results and
human decisions keep their existing semantics; see docs/deferred-fixes.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx


class BackendError(RuntimeError):
    pass


def parse_sse(text: str) -> list[dict]:
    events = []
    name, data = "message", []
    for line in [*text.replace("\r\n", "\n").split("\n"), ""]:
        if not line:
            if data:
                try:
                    value = json.loads("\n".join(data))
                except json.JSONDecodeError as exc:
                    raise BackendError("Backend returned malformed SSE JSON") from exc
                events.append({"event": name, "data": value})
            name, data = "message", []
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    return events


def validate_standard_id(value: str) -> str:
    if not re.fullmatch(r"STD-B-[A-Za-z0-9_-]{1,100}", value):
        raise ValueError("Use the exact STD-B-* ID returned by generate_standard")
    return value


def summarize_event(event: dict) -> dict:
    name, data = event["event"], event["data"]
    if not isinstance(data, dict):
        return {"event": name}
    # Full per-field results remain in the web application and backend outputs.
    keys = {
        "req_parsed": ["target_object_label", "target_objects", "file_count", "degraded"],
        "req_complete": ["attr_count", "ambiguity_count", "degraded"],
        "gap_score": ["completeness_score", "status_dist", "template_field_total"],
        "gap_complete": ["status_dist", "blocking", "note"],
        "match_library": ["source", "class_count", "attribute_count", "note"],
        "match_complete": ["verdict_dist", "retrieval"],
        "syn_overview": ["field_completeness", "source_dist", "complete", "gate"],
        "syn_commit": ["ok", "complete", "attributes", "error"],
        "syn_complete": ["complete", "export_allowed", "gate", "missing_fields"],
        "std_created": ["standard_id", "class_count", "attribute_count"],
        "std_complete": ["standard_id", "source_path", "quality", "usability", "incomplete_fields"],
        "graph_complete": ["candidate_count", "stats"],
        "extract_complete": ["candidate_count", "count", "low_confidence_count"],
        "parse_complete": ["mode"],
    }.get(name, [])
    return {"event": name, "data": {k: data[k] for k in keys if k in data}}


class BackendClient:
    def __init__(self, base_url: str | None = None, audit_dir: Path | None = None, transport=None):
        self.base_url = (base_url or os.getenv("STANDARD_BACKEND_URL", "http://127.0.0.1:8123")).rstrip("/")
        parsed = urlparse(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise ValueError("STANDARD_BACKEND_URL must be an HTTP loopback origin")
        self.audit_dir = audit_dir or Path(os.getenv("STANDARD_MCP_AUDIT_DIR", str(Path(__file__).resolve().parent.parent / ".runtime/mcp")))
        self.timeout = float(os.getenv("STANDARD_BACKEND_TIMEOUT", "300"))
        self._lock = asyncio.Lock()
        self._transport = transport

    async def request(self, path: str, body: dict | None = None, *, method: str = "POST",
                      params: dict | None = None, terminal: str | None = None,
                      download: bool = False) -> dict:
        started = time.monotonic()
        call_id = uuid.uuid4().hex
        try:
            async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout, trust_env=False,
                                         transport=self._transport) as client:
                response = await client.request(method, path, json=body if method != "GET" else None, params=params)
        except httpx.TimeoutException as exc:
            self._audit(call_id, path, "unknown", started)
            raise BackendError("Backend timed out; execution may continue. Inspect artifacts before retrying.") from exc
        except httpx.HTTPError as exc:
            self._audit(call_id, path, "connection_error", started)
            raise BackendError("Cannot reach the local backend; start it in live mode first.") from exc
        if response.is_error:
            self._audit(call_id, path, "http_error", started)
            try:
                error = response.json().get("error", "")
            except (ValueError, AttributeError):
                error = ""
            raise BackendError(f"Backend HTTP {response.status_code}: {str(error)[:500]}")
        if download:
            if "spreadsheetml" not in response.headers.get("content-type", ""):
                raise BackendError("Expected XLSX response")
            self.audit_dir.mkdir(parents=True, exist_ok=True)
            target = self.audit_dir / f"export-{call_id}.xlsx"
            target.write_bytes(response.content)
            result = {"saved_file": str(target), "bytes": len(response.content),
                      "sha256": hashlib.sha256(response.content).hexdigest()}
        elif "text/event-stream" in response.headers.get("content-type", ""):
            events = parse_sse(response.text)
            for event in events:
                data = event["data"]
                if (event["event"].endswith("_failed") or event["event"] == "error"
                        or (isinstance(data, dict) and data.get("ok") is False)):
                    raise BackendError(f"Backend stage failed: {event['event']}: {str(data)[:500]}")
            if terminal and not any(e["event"] == terminal for e in events):
                raise BackendError(f"Incomplete backend stream: missing {terminal}; inspect before retrying")
            result = {"events": [summarize_event(e) for e in events if e["event"] in {
                "req_parsed", "req_complete", "gap_score", "gap_complete", "match_library", "match_complete",
                "syn_overview", "syn_commit", "syn_complete", "std_created", "std_complete", "graph_complete",
                "extract_complete", "parse_complete"}], "event_count": len(events)}
        else:
            try:
                result = response.json()
            except ValueError as exc:
                raise BackendError("Expected JSON or SSE from backend") from exc
            if not isinstance(result, dict):
                raise BackendError("Expected a backend result object")
            if result.get("ok") is False or result.get("error"):
                raise BackendError(str(result.get("error") or result.get("reason") or "Backend failed")[:500])
        self._audit(call_id, path, "complete", started)
        return {"call_id": call_id, **result}

    def _audit(self, call_id: str, path: str, status: str, started: float):
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        with (self.audit_dir / "calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"call_id": call_id, "endpoint": path, "status": status,
                                     "elapsed_ms": round((time.monotonic() - started) * 1000),
                                     "time": time.time()}) + "\n")

    async def require_live(self):
        status = await self.request("/api/integration/status", method="GET")
        if not status.get("live_mode") or status.get("replay_effective"):
            raise BackendError("Start the backend with STANDARD_EXECUTION_MODE=live; MCP refuses replay mode")
        return status

    async def stage(self, path: str, body: dict | None = None, **kwargs) -> dict:
        async with self._lock:
            await self.require_live()
            return await self.request(path, body, **kwargs)
