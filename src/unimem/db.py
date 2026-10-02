from __future__ import annotations

import hashlib
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Iterator

from .config import Settings, iso_after, now_iso, parse_iso
from .models import MemoryRecord
from .policy import contains_secret
from .semantic import DEFAULT_SEMANTIC_MODEL, LocalSemanticEmbedder

if TYPE_CHECKING:
    from .enrich import LocalEnricher


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
    content_hash TEXT NOT NULL,
    enrichment TEXT NOT NULL DEFAULT '',
    enrichment_model TEXT
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

CREATE TABLE IF NOT EXISTS memory_embeddings (
    memory_id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    embedding BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    evidence,
    enrichment,
    content='memories',
    content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(rowid, content, evidence, enrichment)
    VALUES (new.rowid, new.content, new.evidence, new.enrichment);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, evidence, enrichment)
    VALUES ('delete', old.rowid, old.content, old.evidence, old.enrichment);
END;
CREATE TRIGGER IF NOT EXISTS memories_fts_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, content, evidence, enrichment)
    VALUES ('delete', old.rowid, old.content, old.evidence, old.enrichment);
    INSERT INTO memories_fts(rowid, content, evidence, enrichment)
    VALUES (new.rowid, new.content, new.evidence, new.enrichment);
END;
"""

FTS_OBJECTS = ("memories_fts_ai", "memories_fts_ad", "memories_fts_au")

SEARCH_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "did",
    "do",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "would",
}


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
        self.semantic_embedder: LocalSemanticEmbedder | None = None
        self.semantic_embed_on_write = True
        self.hybrid_fts_weight = 2.0
        self.hybrid_semantic_weight = 1.0
        self.hybrid_rrf_k = 60
        # bm25 column weights for (content, evidence, enrichment). Enrichment is
        # down-weighted so generated text widens matching without outranking
        # the original memory; measured in benchmarks/README.md.
        self.fts_column_weights = (1.0, 1.0, 0.25)

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
            self._migrate_enrichment_columns(connection)
            try:
                self._migrate_fts(connection)
                connection.executescript(FTS_SCHEMA)
                self.fts_enabled = True
            except sqlite3.OperationalError:
                self.fts_enabled = False
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS memories_fts_fallback "
                    "(rowid INTEGER PRIMARY KEY, content TEXT, evidence TEXT)"
                )
            self.expire_sessions(connection)

    @staticmethod
    def _migrate_enrichment_columns(connection: sqlite3.Connection) -> None:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(memories)")}
        if "enrichment" not in columns:
            connection.execute(
                "ALTER TABLE memories ADD COLUMN enrichment TEXT NOT NULL DEFAULT ''"
            )
        if "enrichment_model" not in columns:
            connection.execute("ALTER TABLE memories ADD COLUMN enrichment_model TEXT")

    @staticmethod
    def _migrate_fts(connection: sqlite3.Connection) -> None:
        """Rebuild an FTS index created before the enrichment column existed."""
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memories_fts'"
        ).fetchone()
        if not exists:
            return
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(memories_fts)")}
        if "enrichment" in columns:
            return
        for trigger in FTS_OBJECTS:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("DROP TABLE memories_fts")
        connection.executescript(FTS_SCHEMA)
        connection.execute("INSERT INTO memories_fts(memories_fts) VALUES ('rebuild')")

    def enrich_pending(
        self,
        enricher: "LocalEnricher",
        *,
        limit: int | None = None,
        observed_on: Callable[[sqlite3.Row], date | None] | None = None,
        fetch_size: int = 64,
    ) -> int:
        """Enrich active memories not yet indexed with the enricher's model.

        Generation happens outside any open transaction so concurrent clients
        are not blocked while the model runs.
        """
        from .enrich import EnrichmentInput

        def created_on(row: sqlite3.Row) -> date | None:
            created = parse_iso(row["created_at"])
            return created.date() if created else None

        self.initialize()
        observed = observed_on or created_on
        enriched = 0
        while limit is None or enriched < limit:
            size = fetch_size if limit is None else min(fetch_size, limit - enriched)
            with self.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT rowid, * FROM memories
                    WHERE {self._active_memory_sql()}
                      AND (enrichment_model IS NULL OR enrichment_model != ?)
                    ORDER BY rowid
                    LIMIT ?
                    """,
                    (enricher.model_name, size),
                ).fetchall()
                inputs = []
                for row in rows:
                    context = connection.execute(
                        """
                        SELECT content FROM memories
                        WHERE scope = ? AND IFNULL(project_id, '') = ?
                          AND IFNULL(session_id, '') = ? AND rowid < ?
                        ORDER BY rowid DESC LIMIT 2
                        """,
                        (row["scope"], row["project_id"] or "", row["session_id"] or "", row["rowid"]),
                    ).fetchall()
                    inputs.append(
                        EnrichmentInput(
                            message=row["content"],
                            context=tuple(item["content"] for item in reversed(context)),
                            observed_on=observed(row),
                        )
                    )
            if not rows:
                break
            texts = enricher.enrich_many(inputs)
            with self.connection() as connection:
                connection.executemany(
                    "UPDATE memories SET enrichment = ?, enrichment_model = ? WHERE id = ?",
                    [(text, enricher.model_name, row["id"]) for row, text in zip(rows, texts)],
                )
            enriched += len(rows)
        return enriched

    def enable_semantic(
        self,
        *,
        model_name: str = DEFAULT_SEMANTIC_MODEL,
        cache_dir: str | None = None,
        embed_on_write: bool = True,
    ) -> dict[str, Any]:
        self.semantic_embedder = LocalSemanticEmbedder(model_name=model_name, cache_dir=cache_dir)
        self.semantic_embed_on_write = embed_on_write
        return {"enabled": True, "model": model_name}

    def _store_embedding(
        self,
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        content: str,
        content_hash: str,
    ) -> None:
        if not self.semantic_embedder or not self.semantic_embed_on_write:
            return
        embedding = self.semantic_embedder.embed_text(content)
        connection.execute(
            """
            INSERT INTO memory_embeddings
            (memory_id, model, dimensions, embedding, content_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                model = excluded.model,
                dimensions = excluded.dimensions,
                embedding = excluded.embedding,
                content_hash = excluded.content_hash,
                created_at = excluded.created_at
            """,
            (
                memory_id,
                self.semantic_embedder.model_name,
                len(embedding) // 4,
                embedding,
                content_hash,
                now_iso(),
            ),
        )

    def embed_pending(self, *, batch_size: int = 32) -> int:
        self.initialize()
        if not self.semantic_embedder:
            raise MemoryError("semantic embeddings are not enabled")
        embedded = 0
        while True:
            with self.connection() as connection:
                rows = connection.execute(
                    f"""
                    SELECT m.id, m.content, m.content_hash
                    FROM memories m
                    LEFT JOIN memory_embeddings e ON e.memory_id = m.id
                    WHERE {self._active_memory_sql("m")}
                      AND (
                          e.memory_id IS NULL
                          OR e.content_hash != m.content_hash
                          OR e.model != ?
                      )
                    ORDER BY m.created_at
                    LIMIT ?
                    """,
                    (self.semantic_embedder.model_name, batch_size),
                ).fetchall()
                if not rows:
                    break
                vectors = self.semantic_embedder.embed_many([row["content"] for row in rows])
                for row, embedding in zip(rows, vectors):
                    connection.execute(
                        """
                        INSERT INTO memory_embeddings
                        (memory_id, model, dimensions, embedding, content_hash, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(memory_id) DO UPDATE SET
                            model = excluded.model,
                            dimensions = excluded.dimensions,
                            embedding = excluded.embedding,
                            content_hash = excluded.content_hash,
                            created_at = excluded.created_at
                        """,
                        (
                            row["id"],
                            self.semantic_embedder.model_name,
                            len(embedding) // 4,
                            embedding,
                            row["content_hash"],
                            now_iso(),
                        ),
                    )
                embedded += len(rows)
        return embedded

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
                self._store_embedding(
                    connection,
                    memory_id=existing["id"],
                    content=normalized,
                    content_hash=content_hash,
                )
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
            self._store_embedding(
                connection,
                memory_id=memory_id,
                content=normalized,
                content_hash=content_hash,
            )
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

        terms = [
            term.lower()
            for term in re.findall(r"[A-Za-z0-9_]+", query)
            if term.lower() not in SEARCH_STOPWORDS
        ] or [term.lower() for term in re.findall(r"[A-Za-z0-9_]+", query)]
        fts_query = " OR ".join(f"{term}*" for term in terms)
        active_sql = self._active_memory_sql("m")
        scope_sql = " OR ".join(scope_clauses)
        if self.fts_enabled and fts_query:
            try:
                return list(
                    connection.execute(
                        f"""
                        SELECT m.*, bm25(memories_fts, ?, ?, ?) AS rank
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
                        (*self.fts_column_weights, fts_query, *scope_args, limit),
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
                  AND (m.content LIKE ? OR m.evidence LIKE ? OR m.enrichment LIKE ?)
                ORDER BY
                    CASE m.scope WHEN 'user' THEN 0 WHEN 'project' THEN 1 ELSE 2 END,
                    m.updated_at DESC
                LIMIT ?
                """,
                (*scope_args, like, like, like, limit),
            ).fetchall()
        )

    def _semantic_rows(
        self,
        connection: sqlite3.Connection,
        *,
        query: str,
        project_id: str,
        session_id: str | None,
        limit: int,
        scopes: tuple[str, ...],
    ) -> list[tuple[sqlite3.Row, float]]:
        if not self.semantic_embedder:
            return []
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
        cursor = connection.execute(
            f"""
            SELECT m.*, e.embedding
            FROM memories m
            JOIN memory_embeddings e ON e.memory_id = m.id
            WHERE {self._active_memory_sql("m")}
              AND e.model = ?
              AND ({' OR '.join(scope_clauses)})
            """,
            [self.semantic_embedder.model_name, *scope_args],
        )
        ranked = self.semantic_embedder.rank(
            query,
            ((row["id"], row["embedding"]) for row in cursor),
            limit=limit,
        )
        if not ranked:
            return []
        placeholders = ", ".join("?" for _ in ranked)
        rows = connection.execute(
            f"SELECT * FROM memories WHERE id IN ({placeholders})",
            [memory_id for memory_id, _ in ranked],
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        return [(by_id[memory_id], score) for memory_id, score in ranked if memory_id in by_id]

    def _fuse_rows(
        self,
        lexical_rows: list[sqlite3.Row],
        semantic_rows: list[tuple[sqlite3.Row, float]],
        limit: int,
    ) -> list[sqlite3.Row]:
        if not semantic_rows:
            return lexical_rows[:limit]
        lexical_ids = [row["id"] for row in lexical_rows]
        semantic_ids = [row["id"] for row, _ in semantic_rows]
        scores: dict[str, float] = {}
        rows: dict[str, sqlite3.Row] = {}
        for rank, memory_id in enumerate(lexical_ids):
            scores[memory_id] = scores.get(memory_id, 0.0) + self.hybrid_fts_weight / (
                self.hybrid_rrf_k + rank + 1
            )
        for rank, (row, _) in enumerate(semantic_rows):
            scores[row["id"]] = scores.get(row["id"], 0.0) + self.hybrid_semantic_weight / (
                self.hybrid_rrf_k + rank + 1
            )
        for row in lexical_rows:
            rows[row["id"]] = row
        for row, _ in semantic_rows:
            rows[row["id"]] = row
        ordered = sorted(scores, key=lambda memory_id: (-scores[memory_id], memory_id))
        return [rows[memory_id] for memory_id in ordered[:limit]]

    def recall(
        self,
        *,
        query: str,
        project_id: str,
        session_id: str | None,
        limit: int = 3,
        scopes: tuple[str, ...] = ("user", "project", "session"),
        semantic: bool = True,
    ) -> list[MemoryRecord]:
        self.initialize()
        limit = max(1, min(int(limit), 20))
        with self.connection() as connection:
            self.expire_sessions(connection)
            lexical_rows = self._search_rows(
                connection,
                query=query,
                project_id=project_id,
                session_id=session_id,
                limit=max(limit * 8, 50),
                scopes=scopes,
            )
            semantic_rows = (
                self._semantic_rows(
                    connection,
                    query=query,
                    project_id=project_id,
                    session_id=session_id,
                    limit=max(limit * 8, 50),
                    scopes=scopes,
                )
                if semantic
                else []
            )
            rows = self._fuse_rows(lexical_rows, semantic_rows, limit)
        return [MemoryRecord.from_row(row) for row in rows]

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
            semantic_count = connection.execute(
                "SELECT COUNT(*) AS count FROM memory_embeddings"
            ).fetchone()["count"]
            enriched = connection.execute(
                "SELECT enrichment_model AS model, COUNT(*) AS count FROM memories "
                "WHERE enrichment_model IS NOT NULL AND " + self._active_memory_sql() + " "
                "GROUP BY enrichment_model"
            ).fetchall()
        return {
            "database": str(self.settings.db_path),
            "project_id": self.settings.project_id,
            "fts5": self.fts_enabled,
            "semantic_enabled": self.semantic_embedder is not None,
            "semantic_model": self.semantic_embedder.model_name if self.semantic_embedder else None,
            "semantic_embeddings": semantic_count,
            "enriched_memories": {row["model"]: row["count"] for row in enriched},
            "memory_counts": {
                f"{row['scope']}/{row['lifecycle']}": row["count"] for row in counts
            },
            "session_counts": {row["status"]: row["count"] for row in sessions},
            "external_api_calls": 0,
            "daemon_required": False,
        }
