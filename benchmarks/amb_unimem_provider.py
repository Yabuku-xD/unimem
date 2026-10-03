"""unimem provider for the Agent Memory Benchmark harness.

AMB (https://github.com/vectorize-io/agent-memory-benchmark) ingests dataset
documents into a memory provider, asks it for context per question, and has an
LLM answer and a second LLM judge. Copy this file to
`src/memory_bench/memory/unimem.py` in an AMB checkout and register
`UnimemMemoryProvider` in that package's REGISTRY; see benchmarks/README.md.

Environment:
    UNIMEM_SRC        path to this repository's `src` directory (required)
    UNIMEM_AMB_K      memories returned per question (default 30)
    UNIMEM_AMB_CHARS  characters kept from each returned memory (default 1500)
    UNIMEM_AMB_FULL   the best-ranked memories kept at UNIMEM_AMB_CHARS; the
                      rest are cut to UNIMEM_AMB_TAIL_CHARS (default: all full)
    UNIMEM_AMB_TAIL_CHARS  characters kept from lower-ranked memories (default 500)
    UNIMEM_AMB_ENRICH MLX model id; when set, memories are enriched after ingest
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from ..models import Document
from .base import MemoryProvider

MEMORY_CHARS = 4000
# Plain-text documents are cut into pieces of about this many characters.
TEXT_CHUNK_CHARS = 1200


def _turn_text(turn: object) -> str:
    if not isinstance(turn, dict):
        return str(turn)
    speaker = turn.get("role") or turn.get("speaker") or "user"
    text = turn.get("content") or turn.get("text") or ""
    caption = turn.get("blip_caption")
    if caption:
        text = f"{text} [shared image: {caption}]"
    return f"{speaker}: {text}"


def _pieces(content: str) -> list[str]:
    """One memory per conversation turn; plain text falls back to paragraphs."""
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return [_turn_text(turn) for turn in parsed]
    pieces: list[str] = []
    current = ""
    for paragraph in content.split("\n"):
        if current and len(current) + len(paragraph) > TEXT_CHUNK_CHARS:
            pieces.append(current)
            current = ""
        current = f"{current}\n{paragraph}".strip()
        while len(current) > MEMORY_CHARS:
            pieces.append(current[:MEMORY_CHARS])
            current = current[MEMORY_CHARS:]
    if current:
        pieces.append(current)
    return pieces


class UnimemMemoryProvider(MemoryProvider):
    name = "unimem"
    description = (
        "Local SQLite FTS5 memory. One memory per conversation turn, keyword recall "
        "with no model on the recall path, optional local write-time enrichment."
    )
    kind = "local"
    link = "https://github.com/Yabuku-xD/unimem"
    concurrency = 4

    def __init__(self) -> None:
        source = os.environ.get("UNIMEM_SRC")
        if not source:
            raise RuntimeError("set UNIMEM_SRC to the unimem repository's src directory")
        if source not in sys.path:
            sys.path.insert(0, source)
        self._k = int(os.environ.get("UNIMEM_AMB_K", "30"))
        self._chars = int(os.environ.get("UNIMEM_AMB_CHARS", "1500"))
        self._full = int(os.environ.get("UNIMEM_AMB_FULL", str(self._k)))
        self._tail_chars = int(os.environ.get("UNIMEM_AMB_TAIL_CHARS", "500"))
        self._enrich_model = os.environ.get("UNIMEM_AMB_ENRICH") or None
        self._database = None
        self._dates: dict[str, str] = {}
        self._dates_path: Path | None = None

    def prepare(self, store_dir: Path, unit_ids: set[str] | None = None, reset: bool = True) -> None:
        from unimem.config import Settings
        from unimem.db import Database

        home = Path(store_dir) / "unimem"
        if reset and home.exists():
            shutil.rmtree(home)
        home.mkdir(parents=True, exist_ok=True)
        self._database = Database(
            Settings.load(project_dir=Path.cwd(), project_id="amb", home=home)
        )
        self._database.initialize()
        self._dates_path = home / "document-dates.json"
        if self._dates_path.exists():
            self._dates = json.loads(self._dates_path.read_text(encoding="utf-8"))

    def ingest(self, documents: list[Document]) -> None:
        from unimem.db import MemoryError

        assert self._database is not None and self._dates_path is not None
        for document in documents:
            self._dates[document.id] = document.timestamp or ""
            for index, piece in enumerate(_pieces(document.content)):
                try:
                    self._database.add_memory(
                        content=piece[:MEMORY_CHARS],
                        scope="project",
                        lifecycle="episodic",
                        kind="fact",
                        # Document ids can contain any character; "\x1f" cannot occur in them.
                        source=f"{document.id}\x1f{index}"[:200],
                        evidence=(document.context or f"Benchmark document {document.id}.")[:4000],
                        project_id=f"amb:{document.user_id}",
                        confidence=1.0,
                    )
                except MemoryError:
                    # The store refuses empty and secret-looking content.
                    continue
        self._dates_path.write_text(json.dumps(self._dates), encoding="utf-8")
        if self._enrich_model:
            from unimem.enrich import LocalEnricher

            self._database.enrich_pending(LocalEnricher(self._enrich_model))

    def retrieve(
        self,
        query: str,
        k: int = 10,
        user_id: str | None = None,
        query_timestamp: str | None = None,
    ) -> tuple[list[Document], dict | None]:
        assert self._database is not None
        records = self._database.recall(
            query=query,
            project_id=f"amb:{user_id}",
            session_id=None,
            limit=self._k,
            scopes=("project",),
            semantic=False,
        )
        rows = []
        for rank, record in enumerate(records):
            document_id, _, index = record.source.partition("\x1f")
            # Full text for the best matches, a shorter excerpt for the long tail.
            chars = self._chars if rank < self._full else self._tail_chars
            rows.append(
                (self._dates.get(document_id, ""), document_id, int(index or 0), record.content[:chars])
            )
        # Chronological order lets the answer model reason about updates and dates.
        rows.sort(key=lambda row: (row[0], row[1], row[2]))
        documents = [
            Document(
                id=document_id,
                content=f"[{date or 'undated'}] {content}",
                user_id=user_id,
                timestamp=date or None,
                source_ids=[document_id],
            )
            for date, document_id, _, content in rows
        ]
        return documents, None
