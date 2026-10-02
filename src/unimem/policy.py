from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from .models import RouteDecision


TRIGGER_TYPES = (
    "explicit_reference",
    "missing_context",
    "cross_session",
    "conflict",
    "memory_query",
)

SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
)

NOISE_EXACT = {
    "hi",
    "hi there",
    "hello",
    "hey",
    "thanks",
    "thank you",
    "ok",
    "okay",
    "sounds good",
    "got it",
    "done",
}

USER_CUES = re.compile(
    r"(?i)\b((?:always|never)\s+(?:use|run|choose|prefer|keep|write|store|commit)|prefer(?:red)?|from now on|for all projects|my default|remember that|please use)\b"
)
PROJECT_CUES = re.compile(
    r"(?i)\b(for this (?:repo|project)|we decided|decision|architecture|our convention|must use|project store|because it needs|repository)\b"
)
SESSION_CUES = re.compile(
    r"(?i)\b(maybe|might be|hypothesis|for now|temporary|today|tomorrow|next step|test later|debugging hypothesis)\b"
)
PROCEDURE_CUES = re.compile(
    r"(?i)\b(runbook|checklist|workflow|procedure|repeatable)\b"
)

ROUTE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "explicit_reference",
        re.compile(
            r"(?i)\b(last time|previously|we previously|we decided|as agreed|you said|our convention|from earlier|prior work|what did we|why did we)\b"
        ),
        "The request refers to prior work or a prior decision.",
    ),
    (
        "memory_query",
        re.compile(r"(?i)\b(memory|memories|recall|remembered|preference|preferences)\b"),
        "The request explicitly asks about stored memory.",
    ),
    (
        "cross_session",
        re.compile(
            r"(?i)\b(resume|continue from|handoff|across sessions|another agent|pick up where|previous session)\b"
        ),
        "The request crosses a session or agent boundary.",
    ),
    (
        "conflict",
        re.compile(r"(?i)\b(conflict|contradict|contradictory|still failing|failed again|regression)\b"),
        "Current evidence is conflicting or a prior failure may explain the task.",
    ),
    (
        "missing_context",
        re.compile(
            r"(?i)\b(unknown|not sure|cannot find|can't find|where is|how do we|which command|owner|constraint|external dependency|missing context)\b"
        ),
        "Repository and documentation inspection may not contain the needed context.",
    ),
)


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def compact_recall_items(records: Sequence[Any]) -> tuple[list[dict[str, Any]], int, int]:
    selected: list[dict[str, Any]] = []
    for record in records[:10]:
        if len(selected) >= 3:
            break
        for content_limit in (320, 240, 160, 96, 48):
            content = record.content
            truncated = len(content) > content_limit
            if truncated:
                content = content[: max(0, content_limit - 3)].rstrip() + "..."
            item = record.recall_dict()
            item["content"] = content
            item["truncated"] = truncated
            candidate = [*selected, item]
            if estimate_tokens(json.dumps(candidate, separators=(",", ":"))) <= 400:
                selected = candidate
                break
    estimated = estimate_tokens(json.dumps(selected, separators=(",", ":")))
    return selected, len(selected), estimated


def contains_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def is_noise(text: str) -> bool:
    normalized = " ".join(text.lower().strip().split())
    return normalized in NOISE_EXACT or len(normalized) <= 3


def strip_fenced_code(text: str) -> str:
    return re.sub(r"```.*?```", " ", text, flags=re.DOTALL)


def classify_route(task: str) -> RouteDecision:
    text = task.strip()
    if not text:
        return RouteDecision(False, None, "Empty task is self-contained.", False)
    for trigger, pattern, reason in ROUTE_RULES:
        if pattern.search(text):
            return RouteDecision(True, trigger, reason, False)
    return RouteDecision(
        False,
        None,
        "Routine task; inspect repository and documentation before considering memory.",
        False,
    )


def validate_recall(trigger: str, evidence: str) -> None:
    if trigger not in TRIGGER_TYPES:
        raise ValueError("trigger must identify a genuine missing-context condition")
    cleaned = evidence.strip()
    if len(cleaned) < 8:
        raise ValueError("evidence must explain why context is missing")
    if contains_secret(cleaned):
        raise ValueError("evidence must not contain secrets")


@dataclass(frozen=True)
class Candidate:
    content: str
    scope: str
    lifecycle: str
    kind: str
    confidence: float
    reason: str


def classify_candidate(text: str) -> Candidate | tuple[None, str]:
    cleaned = " ".join(strip_fenced_code(text).split()).strip()
    if not cleaned:
        return None, "empty"
    if contains_secret(cleaned):
        return None, "secret"
    if is_noise(cleaned):
        return None, "noise"
    if len(cleaned) > 800:
        return None, "too_long"

    session = bool(SESSION_CUES.search(cleaned))
    user = bool(USER_CUES.search(cleaned))
    project = bool(PROJECT_CUES.search(cleaned))
    procedure = bool(PROCEDURE_CUES.search(cleaned))

    if not (user or project or session or procedure):
        return None, "no_durable_cue"

    if session and not user and not project:
        scope = "session"
        lifecycle = "episodic"
        kind = "hypothesis"
        confidence = 0.72
    elif project:
        scope = "project"
        lifecycle = "procedural" if procedure else "semantic"
        kind = "procedure" if procedure else ("decision" if re.search(r"(?i)decid", cleaned) else "fact")
        confidence = 0.88
    elif user:
        scope = "user"
        lifecycle = "procedural" if procedure else "semantic"
        kind = "preference" if re.search(r"(?i)prefer|always|never|default", cleaned) else "fact"
        confidence = 0.9
    else:
        scope = "project"
        lifecycle = "procedural"
        kind = "procedure"
        confidence = 0.78

    clipped = cleaned[:400].rstrip()
    return Candidate(clipped, scope, lifecycle, kind, confidence, "explicit durable cue")


def candidate_to_dict(candidate: Candidate) -> dict[str, Any]:
    return {
        "content": candidate.content,
        "scope": candidate.scope,
        "lifecycle": candidate.lifecycle,
        "kind": candidate.kind,
        "confidence": candidate.confidence,
        "reason": candidate.reason,
    }
