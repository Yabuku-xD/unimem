"""Implicit preferences: remembered only after they recur in separate sessions.

The explicit extractor accepts statements with a clear durable cue ("always
use pnpm"). People also show preferences without one, by correcting the agent
("no, use pnpm") or repeating an instruction. A single correction may be a
one-off, so it is kept as a candidate that recall never returns. When a
matching statement appears in a second session it is promoted to a memory.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from .config import iso_after, now_iso
from .db import Database, MemoryError
from .policy import contains_secret, is_noise, strip_fenced_code

SESSIONS_TO_PROMOTE = 2
# Two statements match when most of the shorter one's words appear in the other.
MATCH_THRESHOLD = 0.66
MIN_SHARED_WORDS = 2
MAX_WORDS = 30
RETENTION_DAYS = 90

# Signs that the user is steering how work is done, without a durable cue.
IMPLICIT_CUES = re.compile(
    r"(?i)(^\s*(no|nope|wrong|actually|again)\b[,.!:]?\s+\w+"
    r"|\binstead\b"
    r"|\b(don'?t|do not|stop|quit|avoid|never)\s+\w+"
    r"|\bi (already )?(told|asked) you\b|\bas i said\b"
    r"|\bwe (always |never |don'?t |do not )?(use|run|write|keep|deploy|name|put)\b"
    r"|\bi (would )?(like|want) you to\b)"
)
# Words that tie a statement to the current moment or to something on screen.
CONTEXT_BOUND = re.compile(
    r"(?i)\b(it|this|that|these|those|here|there|yet|now|today|tonight|tomorrow|"
    r"for now|this time|just once|the other|the (first|second|third|last) one)\b"
)
NEGATION = re.compile(r"(?i)\b(don'?t|do not|stop|quit|avoid|never|not)\b")
LEADING_FILLER = re.compile(r"(?i)^\s*(no|nope|wrong|actually|again|please|ok(ay)?)\b[,.!:]?\s+")
PROJECT_WORDS = re.compile(r"(?i)\b(we|our|repo|repository|project|codebase)\b")
FILLER = {
    "no", "nope", "wrong", "actually", "instead", "again", "please", "just", "i", "told",
    "asked", "you", "as", "said", "the", "a", "an", "to", "of", "for", "in", "on", "we",
    "our", "and", "or", "is", "are", "be", "should", "must", "want", "like", "would",
    "already", "dont", "don", "t", "do", "not", "stop", "quit", "avoid", "never", "always",
    "so", "many", "much", "very", "too", "every",
}


@dataclass(frozen=True)
class ImplicitStatement:
    content: str
    tokens: frozenset[str]
    negative: bool
    project_worded: bool


def _stem(word: str) -> str:
    stemmed = re.sub(r"(ing|es|s|e)$", "", word)
    return stemmed if len(stemmed) >= 2 else word


def classify_implicit(text: str) -> ImplicitStatement | tuple[None, str]:
    cleaned = " ".join(strip_fenced_code(text).split()).strip()
    if not cleaned or is_noise(cleaned):
        return None, "noise"
    if contains_secret(cleaned):
        return None, "secret"
    if cleaned.endswith("?"):
        return None, "question"
    if len(cleaned.split()) > MAX_WORDS:
        return None, "too_long"
    stated = re.sub(r'"[^"]*"|`[^`]*`', " ", cleaned)
    if not IMPLICIT_CUES.search(stated):
        return None, "no_implicit_cue"
    if CONTEXT_BOUND.search(stated):
        return None, "context_bound"
    content = LEADING_FILLER.sub("", cleaned).strip()
    words = (
        word.strip("./-")
        for word in re.findall(r"[a-z0-9_@./-]+", content.lower().replace("'", ""))
    )
    tokens = frozenset(_stem(word) for word in words if word and word not in FILLER)
    if len(tokens) < 2:
        return None, "too_vague"
    content = content[0].upper() + content[1:]
    return ImplicitStatement(
        content=content[:300],
        tokens=tokens,
        negative=bool(NEGATION.search(stated)),
        project_worded=bool(PROJECT_WORDS.search(stated)),
    )


def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
    shared = len(left & right)
    if shared < MIN_SHARED_WORDS:
        return 0.0
    return shared / min(len(left), len(right))


def record_implicit(
    database: Database,
    statement: ImplicitStatement,
    *,
    session_id: str,
    project_id: str,
    source: str,
) -> dict[str, Any]:
    """Record one sighting; promote the candidate once enough sessions agree."""
    now = now_iso()
    with database.connection() as connection:
        connection.execute("DELETE FROM candidates WHERE status = 'pending' AND updated_at < ?", (iso_after(-RETENTION_DAYS * 86400),))
        connection.execute(
            "DELETE FROM candidate_sightings WHERE candidate_id NOT IN (SELECT id FROM candidates)"
        )
        match = None
        best = MATCH_THRESHOLD
        for row in connection.execute(
            "SELECT * FROM candidates WHERE negative = ?", (int(statement.negative),)
        ):
            score = _similarity(statement.tokens, frozenset(json.loads(row["tokens"])))
            if score >= best:
                match, best = row, score
        if match is None:
            candidate_id = f"cand_{uuid.uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO candidates (id, content, tokens, negative, status, memory_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)
                """,
                (candidate_id, statement.content, json.dumps(sorted(statement.tokens)), int(statement.negative), now, now),
            )
            status = "pending"
        else:
            candidate_id, status = match["id"], match["status"]
            connection.execute(
                "UPDATE candidates SET content = ?, updated_at = ? WHERE id = ?",
                (statement.content, now, candidate_id),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO candidate_sightings (candidate_id, session_id, project_id, project_worded, seen_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (candidate_id, session_id, project_id, int(statement.project_worded), now),
        )
        sightings = connection.execute(
            "SELECT session_id, project_id, project_worded FROM candidate_sightings WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchall()
    result = {"candidate_id": candidate_id, "content": statement.content, "sessions": len(sightings), "status": status}
    if status == "promoted" or len(sightings) < SESSIONS_TO_PROMOTE:
        return result

    projects = {row["project_id"] for row in sightings}
    # One project, or wording about "we"/"this repo", keeps the memory in the project.
    project_scoped = len(projects) == 1 or any(row["project_worded"] for row in sightings)
    try:
        memory, _ = database.add_memory(
            content=statement.content,
            scope="project" if project_scoped else "user",
            lifecycle="semantic",
            kind="preference",
            source="implicit-capture",
            evidence=f"Stated in {len(sightings)} separate sessions ({source}).",
            confidence=0.75,
            project_id=project_id if project_scoped else None,
        )
    except MemoryError as error:
        return {**result, "status": "rejected", "reason": str(error)}
    with database.connection() as connection:
        connection.execute(
            "UPDATE candidates SET status = 'promoted', memory_id = ?, updated_at = ? WHERE id = ?",
            (memory.id, now_iso(), candidate_id),
        )
    return {**result, "status": "promoted", "memory_id": memory.id, "scope": memory.scope}


def list_candidates(database: Database) -> list[dict[str, Any]]:
    database.initialize()
    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT c.id, c.content, c.status, c.memory_id, c.updated_at,
                   COUNT(s.session_id) AS sessions
            FROM candidates c LEFT JOIN candidate_sightings s ON s.candidate_id = c.id
            GROUP BY c.id ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]
