"""Session hooks that coding tools run when a session starts or ends.

`session-start` opens a unimem session for the tool's session, so short-lived
notes have somewhere to live. `session-end` distills durable facts from the
transcript, then closes the session so its notes stop being recalled.

Hooks never write to stdout: several tools add a hook's stdout to the model's
context, and unimem must not put anything in the prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .extract import distill_messages

HOOK_CLIENT_PREFIX = "hook:"
# Transcripts can be large; only the tail is scanned, enough for a long session.
MAX_TRANSCRIPT_BYTES = 8 * 1024 * 1024
# Text injected by the tools themselves rather than typed by the user.
SYNTHETIC_PREFIXES = (
    "<environment_context",
    "<user_instructions",
    "<turn_aborted",
    "<system-reminder",
    "<command-name",
    "<local-command",
    "# AGENTS.md instructions",
    "Caveat: The messages below",
)


def normalize_payload(raw: dict[str, Any], overrides: dict[str, str | None]) -> dict[str, str | None]:
    """Reduce each tool's hook payload to the three fields unimem needs."""
    roots = raw.get("workspace_roots")
    cwd = (
        overrides.get("cwd")
        or raw.get("cwd")
        or (roots[0] if isinstance(roots, list) and roots else None)
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )
    session_id = overrides.get("session_id") or raw.get("session_id") or raw.get("conversation_id")
    transcript = (
        overrides.get("transcript")
        or raw.get("transcript_path")
        or os.environ.get("CURSOR_TRANSCRIPT_PATH")
    )
    return {
        "cwd": str(cwd),
        "session_id": str(session_id) if session_id else None,
        "transcript": str(transcript) if transcript else None,
    }


def unimem_session_id(client: str, client_session_id: str) -> str:
    digest = hashlib.sha256(f"{client}\0{client_session_id}".encode()).hexdigest()[:16]
    return f"ses_{digest}"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in {"text", "input_text", "output_text"}:
                parts.append(str(part.get("text") or ""))
        return "\n".join(parts)
    return ""


def _message_from(entry: dict[str, Any]) -> dict[str, str] | None:
    """Find a role and text in one transcript entry, whichever tool wrote it.

    Claude Code and Pi nest the message under `message`, Codex under `payload`,
    Cursor puts `role` beside `message`; plain `{role, content}` also works.
    """
    if entry.get("isMeta") or entry.get("isSidechain"):
        return None
    holder = entry
    for key in ("message", "payload"):
        if isinstance(entry.get(key), dict):
            holder = entry[key]
            break
    role = holder.get("role") or entry.get("role")
    if role not in {"user", "assistant"}:
        return None
    text = _text_of(holder.get("content")).strip()
    if not text or text.startswith(SYNTHETIC_PREFIXES):
        return None
    # Cursor wraps typed text in <user_query> tags.
    text = text.replace("<user_query>", "").replace("</user_query>", "").strip()
    return {"role": str(role), "content": text}


def read_transcript(path: Path) -> list[dict[str, str]]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > MAX_TRANSCRIPT_BYTES:
            handle.seek(size - MAX_TRANSCRIPT_BYTES)
            handle.readline()  # drop the partial first line
        text = handle.read().decode("utf-8", errors="replace")
    messages: list[dict[str, str]] = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            message = _message_from(entry)
            if message:
                messages.append(message)
    return messages


def read_hermes_session(session_id: str) -> list[dict[str, str]]:
    """Hermes keeps transcripts in its own SQLite store instead of a file."""
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    store = home / "state.db"
    if not store.exists():
        return []
    try:
        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=2)
        try:
            rows = connection.execute(
                "SELECT role, content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id",
                (session_id,),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []
    return [{"role": role, "content": content} for role, content in rows if content]


def session_start(settings: Settings, *, client: str, payload: dict[str, str | None]) -> dict[str, Any]:
    if not payload["session_id"]:
        return {"ok": False, "reason": "no session id in hook payload"}
    session_id = unimem_session_id(client, payload["session_id"])
    Database(settings).ensure_session(
        session_id, project_id=settings.project_id, client=f"{HOOK_CLIENT_PREFIX}{client}"
    )
    # Claude Code sources this file before each Bash command.
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as handle:
            handle.write(f"export UNIMEM_SESSION_ID={session_id}\n")
    return {"ok": True, "session_id": session_id}


def session_end(settings: Settings, *, client: str, payload: dict[str, str | None]) -> dict[str, Any]:
    if not payload["session_id"]:
        return {"ok": False, "reason": "no session id in hook payload"}
    session_id = unimem_session_id(client, payload["session_id"])
    database = Database(settings)
    transcript = Path(payload["transcript"]) if payload["transcript"] else None
    if transcript and transcript.is_file():
        messages = read_transcript(transcript)
    elif client == "hermes":
        messages = read_hermes_session(payload["session_id"])
    else:
        messages = []
    accepted = 0
    if messages:
        # A session that was never opened (hook added mid-session) still gets captured.
        database.ensure_session(
            session_id, project_id=settings.project_id, client=f"{HOOK_CLIENT_PREFIX}{client}"
        )
        result = distill_messages(
            settings=settings,
            database=database,
            messages=messages,
            source=f"{client}-session",
            session_id=session_id,
            apply=True,
        )
        accepted = int(result["accepted_count"])
    database.end_session(session_id)
    return {"ok": True, "session_id": session_id, "messages": len(messages), "accepted": accepted}
