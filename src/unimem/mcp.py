from __future__ import annotations

import json
import sys
from typing import Any

from .config import Settings
from .db import Database, MemoryError
from .policy import TRIGGER_TYPES, compact_recall_items, contains_secret, validate_recall
from .semantic import DEFAULT_SEMANTIC_MODEL

TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["recall", "remember", "status"],
            "description": "Use recall only after a missing-context trigger.",
        },
        "query": {"type": "string", "description": "Short task query for recall."},
        "trigger": {
            "type": "string",
            "enum": list(TRIGGER_TYPES),
            "description": "Why current context is insufficient.",
        },
        "evidence": {
            "type": "string",
            "description": "Concrete evidence supporting the trigger.",
        },
        "content": {"type": "string", "description": "Durable fact to remember."},
        "scope": {"type": "string", "enum": ["user", "project", "session"]},
        "lifecycle": {"type": "string", "enum": ["semantic", "episodic", "procedural"]},
        "kind": {"type": "string", "description": "preference, decision, fact, hypothesis, or procedure"},
        "session_id": {"type": "string"},
        "expires_at": {"type": "string"},
        "semantic": {"type": "boolean"},
        "semantic_model": {"type": "string"},
    },
    "required": ["action"],
    "additionalProperties": False,
}

TOOL_DESCRIPTION = (
    "Local lazy memory. Inspect repository and documentation first. "
    "Call recall only for prior-work references, missing constraints, handoffs, or conflicts; "
    "provide trigger and evidence. Store only durable claims with remember."
)


def tool_schema_bytes() -> int:
    return len(json.dumps(TOOL_SCHEMA, separators=(",", ":")).encode("utf-8"))


def _bounded_recall(items: list[Any]) -> tuple[list[dict[str, Any]], int, int]:
    return compact_recall_items(items)


def run_tool(arguments: dict[str, Any], settings: Settings) -> dict[str, Any]:
    database = Database(settings)
    database.initialize()
    semantic_model = str(arguments.get("semantic_model") or "").strip()
    use_semantic = bool(arguments.get("semantic")) or bool(semantic_model)
    action = str(arguments.get("action", ""))
    if use_semantic and action in {"recall", "remember"}:
        database.enable_semantic(
            model_name=semantic_model or DEFAULT_SEMANTIC_MODEL,
            embed_on_write=action == "remember",
        )
    if action == "status":
        return {"ok": True, **database.stats()}
    if action == "recall":
        trigger = str(arguments.get("trigger", ""))
        evidence = str(arguments.get("evidence", ""))
        query = str(arguments.get("query", "")).strip()
        try:
            validate_recall(trigger, evidence)
        except ValueError as error:
            database.audit_recall(
                action="recall_blocked",
                trigger=trigger or None,
                reason=str(error),
                project_id=settings.project_id,
                session_id=settings.session_id,
                result_count=0,
                estimated_tokens=0,
                retrieval_performed=False,
            )
            return {
                "ok": False,
                "error": {"code": "missing_trigger", "message": str(error)},
            }
        if not query:
            return {
                "ok": False,
                "error": {"code": "missing_query", "message": "recall requires a query"},
            }
        records = database.recall(
            query=query,
            project_id=settings.project_id,
            session_id=settings.session_id,
            limit=3,
            semantic=use_semantic,
        )
        items, count, estimated = _bounded_recall(records)
        database.audit_recall(
            action="recall",
            trigger=trigger,
            reason=evidence,
            project_id=settings.project_id,
            session_id=settings.session_id,
            result_count=count,
            estimated_tokens=estimated,
            retrieval_performed=True,
        )
        return {
            "ok": True,
            "retrieval_performed": True,
            "trigger": trigger,
            "items": items,
            "count": count,
            "estimated_tokens": estimated,
        }
    if action == "remember":
        content = str(arguments.get("content", ""))
        scope = str(arguments.get("scope", "project"))
        lifecycle = str(arguments.get("lifecycle", "semantic"))
        kind = str(arguments.get("kind", "fact"))
        source = "mcp"
        evidence = str(arguments.get("evidence", "")).strip()
        if len(evidence) < 8:
            return {
                "ok": False,
                "error": {"code": "missing_evidence", "message": "remember requires provenance evidence"},
            }
        if contains_secret(content) or contains_secret(evidence):
            return {
                "ok": False,
                "error": {"code": "secret_rejected", "message": "secrets must not be stored in memory"},
            }
        try:
            memory, created = database.add_memory(
                content=content,
                scope=scope,
                lifecycle=lifecycle,
                kind=kind,
                source=source,
                evidence=evidence,
                project_id=settings.project_id if scope == "project" else None,
                session_id=str(arguments.get("session_id") or settings.session_id or "")
                if scope == "session"
                else None,
                expires_at=str(arguments.get("expires_at") or "") or None,
            )
        except MemoryError as error:
            return {"ok": False, "error": {"code": "invalid_memory", "message": str(error)}}
        if use_semantic:
            database.embed_pending()
        return {"ok": True, "created": created, **memory.public_dict()}
    return {
        "ok": False,
        "error": {"code": "invalid_action", "message": "action must be recall, remember, or status"},
    }


def _tool_result(payload: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, separators=(",", ":"))}],
        "isError": error,
    }


def _handle(message: dict[str, Any], settings: Settings) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "unimem", "version": "0.1.0"},
            },
        }
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "unimem",
                        "description": TOOL_DESCRIPTION,
                        "inputSchema": TOOL_SCHEMA,
                    }
                ]
            },
        }
    if method == "tools/call":
        params = message.get("params", {})
        if params.get("name") != "unimem":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "unknown tool"},
            }
        try:
            payload = run_tool(dict(params.get("arguments", {})), settings)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_result(payload, error=not payload.get("ok", False)),
            }
        except Exception as error:  # pragma: no cover - defensive MCP boundary
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": _tool_result(
                    {"ok": False, "error": {"code": "internal", "message": str(error)}},
                    error=True,
                ),
            }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def serve(settings: Settings) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = _handle(message, settings)
        if response is not None:
            sys.stdout.write(json.dumps(response, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0
