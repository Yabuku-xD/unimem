from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .config import Settings, iso_after, now_iso, parse_iso
from .models import MemoryRecord
from .policy import contains_secret


SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('user', 'project', 'session')),
    project_id TEXT,
    session_id TEXT,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('semantic', 'episodic', 'procedural')),
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    evidence TEXT NOT NULL,
    confidence REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL,
    expires_at TEXT,
    supersedes TEXT,
    content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_scope_project
    ON memories(scope, project_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_memories_session
    ON memories(session_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_memories_hash
    ON memories(content_hash, scope, project_id, session_id, status);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    client TEXT,
    title TEXT,
    status TEXT NOT NULL CHECK (status IN ('active', 'closed', 'expired')),
    created_at TEXT NOT NULL,
    last_activity_at TEXT NOT NULL,
    ended_at TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_scope_status
    ON sessions(project_id, status, expires_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    trigger TEXT,
    reason TEXT,
    project_id TEXT,
    session_id TEXT,
    result_count INTEGER NOT NULL DEFAULT 0,
    estimated_tokens INTEGER NOT NULL DEFAULT 0,
    retrieval_performed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    evidence,
    content='memories',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, evidence)
    VALUES (new.rowid, new.content, new.evidence);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, evidence)
    VALUES ('delete', old.rowid, old.content, old.evidence);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, evidence)
    VALUES ('delete', old.rowid, old.content, old.evidence);
    INSERT INTO memories_fts(rowid, content, evidence)
    VALUES (new.rowid, new.content, new.evidence);
END;
"""


class MemoryError(Exception):
    """A user-correctable memory operation error."""


@dataclass(frozen=True)
class SessionRecord:
    id: str
    project_id: str
    client: str | None
    title: str | None
    status: str
    created_at: str
    last_activity_at: str
    ended_at: str | None
    expires_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "SessionRecord":
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            client=row["client"],
            title=row["title"],
            status=row["status"],
            created_at=row["created_at"],
            last_activity_at=row["last_activity_at"],
            ended_at=row["ended_at"],
            expires_at=row["expires_at"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "client": self.client,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "ended_at": self.ended_at,
            "expires_at": self.expires_at,
        }


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_home()
        self.fts_enabled = False

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.settings.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            try:
                connection.executescript(FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS memories_fts_fallback "
                    "(rowid INTEGER PRIMARY KEY, content TEXT, evidence TEXT)"
                )
            self.expire_sessions(connection)

    def expire_sessions(self, connection: sqlite3.Connection | None = None) -> int:
        def run(conn: sqlite3.Connection) -> int:
            now = now_iso()
            result = conn.execute(
                """
                UPDATE sessions
                SET status = 'expired', ended_at = COALESCE(ended_at, ?), last_activity_at = ?
                WHERE status = 'active' AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now, now, now),
            )
            return result.rowcount

        if connection is not None:
            return run(connection)
        with self.connection() as conn:
            return run(conn)

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(" ".join(content.split()).encode("utf-8")).hexdigest()

    @staticmethod
    def _active_memory_sql(alias: str = "memories") -> str:
        now = now_iso()
        return f"""
            {alias}.status = 'active'
            AND ({alias}.expires_at IS NULL OR {alias}.expires_at > '{now}')
            AND (
                {alias}.scope != 'session'
                OR EXISTS (
                    SELECT 1 FROM sessions s
                    WHERE s.id = {alias}.session_id
                      AND s.status = 'active'
                      AND (s.expires_at IS NULL OR s.expires_at > '{now}')
                )
            )
        """

    def add_memory(
        self,
        *,
        content: str,
        scope: str,
        lifecycle: str,
        kind: str,
        source: str,
        evidence: str,
        confidence: float = 0.8,
        project_id: str | None = None,
        session_id: str | None = None,
        expires_at: str | None = None,
        supersedes: str | None = None,
    ) -> tuple[MemoryRecord, bool]:
        normalized = " ".join(content.split()).strip()
        if not normalized:
            raise MemoryError("memory content cannot be empty")
        if len(normalized) > 4000:
            raise MemoryError("memory content exceeds 4000 characters")
        if scope not in {"user", "project", "session"}:
            raise MemoryError("scope must be user, project, or session")
        if lifecycle not in {"semantic", "episodic", "procedural"}:
            raise MemoryError("invalid lifecycle")
        if kind not in {"preference", "decision", "fact", "hypothesis", "procedure", "note"}:
            raise MemoryError("invalid memory kind")
        if not source.strip() or len(source.strip()) > 200:
            raise MemoryError("source must be 1-200 characters")
        if len(evidence.strip()) < 8 or len(evidence.strip()) > 4000:
            raise MemoryError("evidence must be 8-4000 characters")
        if contains_secret(normalized) or contains_secret(evidence):
            raise MemoryError("secrets must not be stored in memory")
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise MemoryError("confidence must be between 0 and 1")
        if expires_at:
            try:
                expires_at = parse_iso(expires_at).isoformat()
            except (TypeError, ValueError) as error:
                raise MemoryError("expires_at must be an ISO-8601 timestamp") from error
        if scope == "project" and not project_id:
            raise MemoryError("project memories require a project id")
        if scope == "session" and not session_id:
            raise MemoryError("session memories require a session id")

        self.initialize()
        now = now_iso()
        content_hash = self._content_hash(normalized)
        with self.connection() as connection:
            self.expire_sessions(connection)
            if scope == "session":
                session_row = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if not session_row:
                    raise MemoryError("session does not exist")
                session = SessionRecord.from_row(session_row)
                if session.status != "active":
                    raise MemoryError("cannot write to an inactive session")
                expires_at = expires_at or session.expires_at or iso_after(24 * 60 * 60)
                if session.expires_at and parse_iso(expires_at) and parse_iso(expires_at) > parse_iso(session.expires_at):
                    expires_at = session.expires_at
            else:
                session_id = None

            existing = connection.execute(
                """
                SELECT * FROM memories
                WHERE content_hash = ?
                  AND scope = ?
                  AND COALESCE(project_id, '') = COALESCE(?, '')
                  AND COALESCE(session_id, '') = COALESCE(?, '')
                  AND status = 'active'
                ORDER BY created_at DESC LIMIT 1
                """,
                (content_hash, scope, project_id, session_id),
            ).fetchone()
            if existing:
                updated = dict(existing)
                updated["last_confirmed_at"] = now
                updated["updated_at"] = now
                updated["confidence"] = max(float(existing["confidence"]), confidence)
                if expires_at:
                    updated["expires_at"] = expires_at
                connection.execute(
                    """
                    UPDATE memories
                    SET last_confirmed_at = ?, updated_at = ?, confidence = ?, expires_at = ?
                    WHERE id = ?
                    """,
                    (
                        updated["last_confirmed_at"],
                        updated["updated_at"],
                        updated["confidence"],
                        updated["expires_at"],
                        existing["id"],
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM memories WHERE id = ?", (existing["id"],)
                ).fetchone()
                return MemoryRecord.from_row(row), False

            memory_id = f"mem_{uuid.uuid4().hex[:16]}"
            connection.execute(
                """
                INSERT INTO memories (
                    id, scope, project_id, session_id, lifecycle, kind, content, source,
                    evidence, confidence, status, created_at, updated_at, last_confirmed_at,
                    expires_at, supersedes, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    scope,
                    project_id,
                    session_id,
                    lifecycle,
                    kind,
                    normalized,
                    source.strip(),
                    evidence.strip(),
                    confidence,
                    now,
                    now,
                    now,
                    expires_at,
                    supersedes,
                    content_hash,
                ),
            )
            if supersedes:
                connection.execute(
                    "UPDATE memories SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, supersedes),
                )
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
            return MemoryRecord.from_row(row), True

    def get_memory(self, memory_id: str) -> MemoryRecord | None:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT * FROM memories WHERE id = ? AND {self._active_memory_sql()}",
                (memory_id,),
            ).fetchone()
            return MemoryRecord.from_row(row) if row else None

    def forget_memory(self, memory_id: str) -> bool:
        self.initialize()
        with self.connection() as connection:
            result = connection.execute(
                "UPDATE memories SET status = 'deleted', updated_at = ? WHERE id = ? AND status != 'deleted'",
                (now_iso(), memory_id),
            )
            return result.rowcount > 0

    def _search_rows(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        project_id: str,
        session_id: str | None,
        limit: int,
        scopes: tuple[str, ...],
    ) -> list[sqlite3.Row]:
        scope_clauses: list[str] = []
        scope_args: list[str] = []
        if "user" in scopes:
            scope_clauses.append("m.scope = 'user'")
        if "project" in scopes:
            scope_clauses.append("(m.scope = 'project' AND m.project_id = ?)")
            scope_args.append(project_id)
        if "session" in scopes and session_id:
            scope_clauses.append("(m.scope = 'session' AND m.session_id = ?)")
            scope_args.append(session_id)
        if not scope_clauses:
            return []

        terms = re.findall(r"[A-Za-z0-9_]+", query)
        fts_query = " OR ".join(f'"{term}"' for term in terms)
        active_sql = self._active_memory_sql("m")
        scope_sql = " OR ".join(scope_clauses)
        if self.fts_enabled and fts_query:
            try:
                return list(
                    connection.execute(
                        f"""
                        SELECT m.*, bm25(memories_fts) AS rank
                        FROM memories m
                        JOIN memories_fts ON m.rowid = memories_fts.rowid
                        WHERE memories_fts MATCH ?
                          AND {active_sql}
                          AND ({scope_sql})
                        ORDER BY
                            CASE m.scope WHEN 'user' THEN 0 WHEN 'project' THEN 1 ELSE 2 END,
                            rank ASC,
                            m.updated_at DESC
                        LIMIT ?
                        """,
                        (fts_query, *scope_args, limit),
                    ).fetchall()
                )
            except sqlite3.OperationalError:
                pass

        like = f"%{' '.join(terms)}%"
        return list(
            connection.execute(
                f"""
                SELECT m.*, 1.0 AS rank
                FROM memories m
                WHERE {active_sql}
                  AND ({scope_sql})
                  AND (m.content LIKE ? OR m.evidence LIKE ?)
                ORDER BY
                    CASE m.scope WHEN 'user' THEN 0 WHEN 'project' THEN 1 ELSE 2 END,
                    m.updated_at DESC
                LIMIT ?
                """,
                (*scope_args, like, like, limit),
            ).fetchall()
        )

    def recall(
        self,
        *,
        query: str,
        project_id: str,
        session_id: str | None,
        limit: int = 3,
        scopes: tuple[str, ...] = ("user", "project", "session"),
    ) -> list[MemoryRecord]:
        self.initialize()
        limit = max(1, min(int(limit), 20))
        with self.connection() as connection:
            self.expire_sessions(connection)
            rows = self._search_rows(
                connection,
                query=query,
                project_id=project_id,
                session_id=session_id,
                limit=limit * 4,
                scopes=scopes,
            )
        return [MemoryRecord.from_row(row) for row in rows[:limit]]

    def list_memories(self, *, project_id: str, session_id: str | None = None) -> list[MemoryRecord]:
        self.initialize()
        with self.connection() as connection:
            self.expire_sessions(connection)
            rows = connection.execute(
                f"""
                SELECT * FROM memories
                WHERE {self._active_memory_sql()}
                  AND (scope = 'user' OR (scope = 'project' AND project_id = ?)
                       OR (scope = 'session' AND session_id = ?))
                ORDER BY updated_at DESC
                """,
                (project_id, session_id or ""),
            ).fetchall()
        return [MemoryRecord.from_row(row) for row in rows]

    def start_session(
        self,
        *,
        project_id: str,
        client: str | None = None,
        title: str | None = None,
        ttl_seconds: float = 24 * 60 * 60,
    ) -> SessionRecord:
        self.initialize()
        session_id = f"ses_{uuid.uuid4().hex[:16]}"
        now = now_iso()
        if not math.isfinite(ttl_seconds):
            raise MemoryError("ttl_seconds must be finite")
        expires_at = iso_after(max(0.0, ttl_seconds))
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO sessions
                (id, project_id, client, title, status, created_at, last_activity_at, ended_at, expires_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?, NULL, ?)
                """,
                (session_id, project_id, client, title, now, now, expires_at),
            )
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return SessionRecord.from_row(row)

    def end_session(self, session_id: str, *, status: str = "closed") -> SessionRecord | None:
        if status not in {"closed", "expired"}:
            raise MemoryError("session status must be closed or expired")
        self.initialize()
        now = now_iso()
        with self.connection() as connection:
            result = connection.execute(
                """
                UPDATE sessions
                SET status = ?, ended_at = ?, last_activity_at = ?, expires_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (status, now, now, now, session_id),
            )
            if result.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return SessionRecord.from_row(row)

    def get_session(self, session_id: str) -> SessionRecord | None:
        self.initialize()
        with self.connection() as connection:
            self.expire_sessions(connection)
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return SessionRecord.from_row(row) if row else None

    def consolidate(self, *, project_id: str, session_id: str | None = None) -> int:
        self.initialize()
        now = now_iso()
        superseded = 0
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memories
                WHERE {self._active_memory_sql()}
                  AND (scope = 'user' OR (scope = 'project' AND project_id = ?)
                       OR (scope = 'session' AND session_id = ?))
                ORDER BY content_hash, created_at
                """,
                (project_id, session_id or ""),
            ).fetchall()
            seen: dict[tuple[str, str, str, str], str] = {}
            for row in rows:
                key = (
                    row["content_hash"],
                    row["scope"],
                    row["project_id"] or "",
                    row["session_id"] or "",
                )
                if key in seen:
                    connection.execute(
                        "UPDATE memories SET status = 'superseded', supersedes = ?, updated_at = ? WHERE id = ?",
                        (seen[key], now, row["id"]),
                    )
                    superseded += 1
                else:
                    seen[key] = row["id"]
        return superseded

    def audit_recall(
        self,
        *,
        action: str,
        trigger: str | None,
        reason: str,
        project_id: str,
        session_id: str | None,
        result_count: int,
        estimated_tokens: int,
        retrieval_performed: bool,
    ) -> None:
        self.initialize()
        with self.connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_log
                (action, trigger, reason, project_id, session_id, result_count,
                 estimated_tokens, retrieval_performed, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action,
                    trigger,
                    reason,
                    project_id,
                    session_id,
                    result_count,
                    estimated_tokens,
                    int(retrieval_performed),
                    now_iso(),
                ),
            )

    def list_audit(self, *, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audit_log
                WHERE project_id = ? OR project_id IS NULL
                ORDER BY id DESC LIMIT ?
                """,
                (project_id, max(1, min(limit, 500))),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "action": row["action"],
                "trigger": row["trigger"],
                "reason": row["reason"],
                "project_id": row["project_id"],
                "session_id": row["session_id"],
                "result_count": row["result_count"],
                "estimated_tokens": row["estimated_tokens"],
                "retrieval_performed": bool(row["retrieval_performed"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def stats(self) -> dict[str, Any]:
        self.initialize()
        with self.connection() as connection:
            counts = connection.execute(
                """
                SELECT scope, lifecycle, COUNT(*) AS count
                FROM memories
                WHERE """ + self._active_memory_sql() + """
                GROUP BY scope, lifecycle
                """
            ).fetchall()
            sessions = connection.execute(
                "SELECT status, COUNT(*) AS count FROM sessions GROUP BY status"
            ).fetchall()
        return {
            "database": str(self.settings.db_path),
            "project_id": self.settings.project_id,
            "fts5": self.fts_enabled,
            "memory_counts": {
                f"{row['scope']}/{row['lifecycle']}": row["count"] for row in counts
            },
            "session_counts": {row["status"]: row["count"] for row in sessions},
            "external_api_calls": 0,
            "daemon_required": False,
        }
