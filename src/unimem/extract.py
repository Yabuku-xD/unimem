from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from .candidates import (
    ImplicitStatement,
    classify_implicit,
    record_implicit,
    same_statement,
)
from .config import Settings, iso_after
from .db import Database, MemoryError
from .policy import Candidate, candidate_to_dict, classify_candidate


def _split_segments(content: str) -> list[str]:
    without_code = re.sub(r"```.*?```", " ", content, flags=re.DOTALL)
    segments = re.split(r"(?<=[.!?])\s+|\n+", without_code)
    return [segment.strip() for segment in segments if segment.strip()]


def load_messages(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        messages: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if isinstance(value, dict):
                messages.append(value)
        return messages
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = None
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return [{"role": "user", "content": paragraph} for paragraph in text.split("\n\n") if paragraph.strip()]


def distill_messages(
    *,
    settings: Settings,
    database: Database,
    messages: list[dict[str, Any]],
    source: str,
    session_id: str | None = None,
    session_ttl_hours: float = 24.0,
    apply: bool = False,
) -> dict[str, Any]:
    if not math.isfinite(session_ttl_hours) or session_ttl_hours < 0:
        raise MemoryError("session_ttl_hours must be a finite non-negative number")
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    expires_at = iso_after(max(0.0, session_ttl_hours) * 60 * 60)

    for message in messages:
        role = str(message.get("role", "user")).lower()
        content = str(message.get("content", ""))
        if role == "tool":
            rejected.append({"content": content[:160], "reason": "tool_output"})
            continue
        if role != "user":
            rejected.append({"content": content[:160], "reason": "non_user_turn"})
            continue
        if "```" in content and not re.sub(r"```.*?```", " ", content, flags=re.DOTALL).strip():
            rejected.append({"content": content[:160], "reason": "code_only"})
            continue
        for segment in _split_segments(content):
            candidate_or_reason = classify_candidate(segment)
            if isinstance(candidate_or_reason, tuple):
                _, reason = candidate_or_reason
                implicit = classify_implicit(segment) if reason == "no_durable_cue" else None
                if isinstance(implicit, ImplicitStatement) and session_id:
                    # Kept as a candidate; it becomes a memory only if it recurs
                    # in another session.
                    sighting: dict[str, Any] = {"content": implicit.content, "status": "pending"}
                    if apply:
                        sighting = record_implicit(
                            database,
                            implicit,
                            session_id=session_id,
                            project_id=settings.project_id,
                            source=source,
                        )
                    candidates.append(sighting)
                    continue
                rejected.append({"content": segment[:160], "reason": reason})
                continue
            candidate: Candidate = candidate_or_reason
            if candidate.scope == "session" and not session_id:
                rejected.append({"content": segment[:160], "reason": "session_without_id"})
                continue
            record = {
                **candidate_to_dict(candidate),
                "source": source,
                "evidence": f"Extracted from {source}",
                "session_id": session_id if candidate.scope == "session" else None,
                "expires_at": expires_at if candidate.scope == "session" else None,
            }
            if apply:
                # The agent may already have saved this in its own words.
                known = database.recall(
                    query=candidate.content,
                    project_id=settings.project_id,
                    session_id=session_id,
                    limit=5,
                    scopes=(candidate.scope,),
                    semantic=False,
                )
                if any(
                    item.content != candidate.content and same_statement(item.content, candidate.content)
                    for item in known
                ):
                    rejected.append({"content": segment[:160], "reason": "already_known"})
                    continue
                try:
                    memory, created = database.add_memory(
                        content=candidate.content,
                        scope=candidate.scope,
                        lifecycle=candidate.lifecycle,
                        kind=candidate.kind,
                        source=source,
                        evidence=f"Extracted from {source}",
                        confidence=candidate.confidence,
                        project_id=settings.project_id if candidate.scope == "project" else None,
                        session_id=session_id if candidate.scope == "session" else None,
                        expires_at=expires_at if candidate.scope == "session" else None,
                    )
                    record["id"] = memory.id
                    record["created"] = created
                except MemoryError as error:
                    rejected.append({"content": segment[:160], "reason": str(error)})
                    continue
            accepted.append(record)

    return {
        "ok": True,
        "source": source,
        "applied": bool(apply),
        "accepted": accepted,
        "rejected": rejected,
        "candidates": candidates,
        "promoted_count": sum(1 for item in candidates if item.get("status") == "promoted"),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
    }
