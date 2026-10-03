"""Streaming dataset adapters for retrieval-only memory benchmark tracks."""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RetrievalCase:
    case_id: str
    project_id: str
    category: str
    query: str
    expected_sources: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalBatch:
    project_id: str
    records: tuple[tuple[str, str], ...]
    cases: tuple[RetrievalCase, ...]


@dataclass(frozen=True)
class DatasetInfo:
    name: str
    evidence_kind: str
    dataset_paths: tuple[str, ...]
    dataset_sha256: dict[str, str]
    notes: str


@dataclass
class DatasetBundle:
    info: DatasetInfo
    batches: Iterable[RetrievalBatch]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _message_content(message: dict[str, Any]) -> str:
    parts = [
        _text(message.get("user_message")),
        _text(message.get("assistant_message")),
        _text(message.get("message")),
        _text(message.get("user")),
        _text(message.get("agent")),
        _text(message.get("role")),
        _text(message.get("content")),
        _text(message.get("time")),
        _text(message.get("time_anchor")),
        _text(message.get("place")),
        _text(message.get("rel")),
        _text(message.get("attr")),
        _text(message.get("value")),
    ]
    return " | ".join(part for part in parts if part)


def _flatten_source_ids(value: Any) -> list[str]:
    if isinstance(value, dict):
        values: list[Any] = []
        for nested in value.values():
            values.extend(nested if isinstance(nested, list) else [nested])
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    return [str(item) for item in values if item is not None]


def _parquet_rows(path: Path, *, batch_size: int = 1) -> Iterator[dict[str, Any]]:
    try:
        import duckdb
    except ImportError as error:
        raise RuntimeError("parquet benchmark adapters require the benchmark extra") from error
    connection = duckdb.connect()
    try:
        cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
        columns = [item[0] for item in cursor.description or []]
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                yield dict(zip(columns, row, strict=True))
    finally:
        connection.close()


def _membench_trajectory_batch(
    *,
    agent_kind: str,
    category: str,
    trajectory: dict[str, Any],
) -> RetrievalBatch | None:
    tid = _text(trajectory.get("tid")) or "unknown"
    project_id = f"membench:{agent_kind}:{category}:{tid}"
    records: list[tuple[str, str]] = []
    expected: list[str] = []
    nested = trajectory.get("message_list", [])
    first = nested[0] if nested else None
    is_session_nested = isinstance(first, list)

    if is_session_nested:
        for session_index, session in enumerate(nested):
            for turn_index, message in enumerate(session):
                if not isinstance(message, dict):
                    continue
                step_id = message.get("sid", turn_index)
                source = f"{project_id}:{session_index}:{step_id}"
                records.append((source, _message_content(message)))
        for target in trajectory.get("QA", {}).get("target_step_id", []):
            if isinstance(target, list) and len(target) == 2:
                expected.append(f"{project_id}:{target[0]}:{target[1]}")
            elif isinstance(target, int) and target < len(nested):
                expected.extend(
                    f"{project_id}:{target}:{message.get('sid', turn_index)}"
                    for turn_index, message in enumerate(nested[target])
                    if isinstance(message, dict)
                )
    else:
        for turn_index, message in enumerate(nested):
            if not isinstance(message, dict):
                continue
            step_id = message.get("mid", turn_index)
            source = f"{project_id}:{step_id}"
            records.append((source, _message_content(message)))
        expected.extend(
            f"{project_id}:{target}"
            for target in trajectory.get("QA", {}).get("target_step_id", [])
            if isinstance(target, int)
        )

    record_sources = {source for source, _ in records}
    expected_unique = tuple(dict.fromkeys(source for source in expected if source in record_sources))
    if not expected_unique:
        return None
    qa = trajectory.get("QA", {})
    query = " | ".join(
        part for part in [_text(qa.get("question")), _text(qa.get("time"))] if part
    )
    return RetrievalBatch(
        project_id=project_id,
        records=tuple(records),
        cases=(
            RetrievalCase(
                case_id=f"{agent_kind}:{category}:{tid}:{qa.get('qid', 'qa')}",
                project_id=project_id,
                category=f"{agent_kind}/{category}",
                query=query,
                expected_sources=expected_unique,
            ),
        ),
    )


def load_membench_dataset(root: Path, *, limit: int | None = None) -> DatasetBundle:
    """Stream MemBench FirstAgent/ThirdAgent data and target step IDs."""
    paths = tuple(sorted(root.glob("*Agent/*.json")))
    if not paths:
        raise FileNotFoundError(f"no MemBench JSON files found under {root}")
    info = DatasetInfo(
        name="MemBench",
        evidence_kind="official target step IDs",
        dataset_paths=tuple(str(path) for path in paths),
        dataset_sha256={str(path): sha256_file(path) for path in paths},
        notes="Retrieval-only adapter for MemBench; multiple-choice answer accuracy is not measured.",
    )

    def batches() -> Iterator[RetrievalBatch]:
        try:
            import ijson
        except ImportError as error:
            raise RuntimeError("MemBench adapters require the benchmark extra") from error
        emitted = 0
        for path in paths:
            agent_kind = path.parent.name
            category = path.stem
            with path.open("rb") as handle:
                for trajectory in ijson.items(handle, "events.item"):
                    batch = _membench_trajectory_batch(
                        agent_kind=agent_kind,
                        category=category,
                        trajectory=trajectory,
                    )
                    if batch:
                        yield batch
                        emitted += 1
                        if limit and emitted >= limit:
                            return

    return DatasetBundle(info=info, batches=batches())


def _context_chunks(context: str) -> list[tuple[str, str]]:
    heading = re.compile(
        r"(?im)^(?P<label>(?:document|dialogue|session)\s+\d+)\s*:\s*$"
    )
    matches = list(heading.finditer(context))
    chunks: list[tuple[str, str]] = []
    if matches:
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(context)
            content = context[start:end].strip()
            if content:
                chunks.append(
                    (f"chunk:{match.group('label').lower().replace(' ', '-')}", content)
                )
        return chunks

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", context) if part.strip()]
    grouped: list[str] = []
    current: list[str] = []
    current_size = 0
    for paragraph in paragraphs:
        current.append(paragraph)
        current_size += len(paragraph)
        if current_size >= 1200:
            grouped.append("\n\n".join(current))
            current = []
            current_size = 0
    if current:
        grouped.append("\n\n".join(current))
    return [(f"chunk:{index:04d}", content) for index, content in enumerate(grouped)]


def _contains_answer(content: str, answer: str) -> bool:
    normalized = " ".join(answer.split())
    if not normalized or len(normalized) > 300:
        return False
    pattern = re.escape(normalized)
    if re.fullmatch(r"[A-Za-z0-9_-]+", normalized):
        pattern = rf"\b{pattern}\b"
    return re.search(pattern, content, flags=re.IGNORECASE) is not None


def load_memoryagentbench_dataset(
    paths: Iterable[Path],
    *,
    limit: int | None = None,
) -> DatasetBundle:
    """Stream answer-bearing evidence chunks from MemoryAgentBench contexts."""
    ordered_paths = tuple(sorted(Path(path) for path in paths))
    if not ordered_paths:
        raise FileNotFoundError("no MemoryAgentBench parquet files provided")
    info = DatasetInfo(
        name="MemoryAgentBench",
        evidence_kind="derived answer-bearing context chunks",
        dataset_paths=tuple(str(path) for path in ordered_paths),
        dataset_sha256={str(path): sha256_file(path) for path in ordered_paths},
        notes=(
            "Retrieval proxy derived from accepted answers appearing in context chunks; "
            "official answer-generation accuracy is not measured."
        ),
    )

    def batches() -> Iterator[RetrievalBatch]:
        emitted = 0
        for path in ordered_paths:
            category = path.stem.removeprefix("unimem-memoryagentbench-")
            for row_index, row in enumerate(_parquet_rows(path)):
                project_id = f"memoryagentbench:{category}:{row_index}"
                chunks = _context_chunks(_text(row.get("context")))
                records = tuple(
                    (f"{project_id}:{chunk_id}", content) for chunk_id, content in chunks
                )
                record_sources = {source for source, _ in records}
                qa_ids = (row.get("metadata") or {}).get("qa_pair_ids") or []
                cases: list[RetrievalCase] = []
                for question_index, question in enumerate(row.get("questions") or []):
                    answers = row.get("answers", [])[question_index]
                    answer_values = answers if isinstance(answers, list) else [answers]
                    answer_values = [_text(answer) for answer in answer_values if _text(answer)]
                    expected = tuple(
                        source
                        for source, content in records
                        if any(_contains_answer(content, answer) for answer in answer_values)
                    )
                    expected = tuple(source for source in expected if source in record_sources)
                    if not expected:
                        continue
                    qa_id = (
                        qa_ids[question_index]
                        if question_index < len(qa_ids)
                        else question_index
                    )
                    cases.append(
                        RetrievalCase(
                            case_id=f"{category}:{row_index}:{qa_id}",
                            project_id=project_id,
                            category=category,
                            query=_text(question),
                            expected_sources=expected,
                        )
                    )
                if not cases:
                    continue
                yield RetrievalBatch(
                    project_id=project_id,
                    records=records,
                    cases=tuple(cases),
                )
                emitted += len(cases)
                if limit and emitted >= limit:
                    return

    return DatasetBundle(info=info, batches=batches())


def load_beam_dataset(path: Path, *, limit: int | None = None) -> DatasetBundle:
    """Stream BEAM probing questions with official source chat message IDs."""
    info = DatasetInfo(
        name="BEAM",
        evidence_kind="official source chat message IDs",
        dataset_paths=(str(path),),
        dataset_sha256={str(path): sha256_file(path)},
        notes="Retrieval-only adapter for BEAM probing questions with source_chat_ids.",
    )

    def batches() -> Iterator[RetrievalBatch]:
        emitted = 0
        for row in _parquet_rows(path):
            conversation_id = _text(row.get("conversation_id"))
            project_id = f"beam:{conversation_id}"
            records: list[tuple[str, str]] = []
            for session in row.get("chat") or []:
                for message in session:
                    if not isinstance(message, dict):
                        continue
                    message_id = _text(message.get("id")) or _text(message.get("index"))
                    records.append(
                        (
                            f"{project_id}:{message_id}",
                            _message_content(message),
                        )
                    )
            record_sources = {source for source, _ in records}
            try:
                probing = ast.literal_eval(_text(row.get("probing_questions")))
            except (SyntaxError, ValueError):
                continue
            cases: list[RetrievalCase] = []
            for category, items in probing.items():
                for item_index, item in enumerate(items):
                    source_values = _flatten_source_ids(item.get("source_chat_ids"))
                    expected = tuple(
                        source
                        for source in dict.fromkeys(
                            f"{project_id}:{source}" for source in source_values
                        )
                        if source in record_sources
                    )
                    if not expected:
                        continue
                    cases.append(
                        RetrievalCase(
                            case_id=f"{conversation_id}:{category}:{item_index}",
                            project_id=project_id,
                            category=category,
                            query=_text(item.get("question")),
                            expected_sources=expected,
                        )
                    )
            if not cases:
                continue
            yield RetrievalBatch(
                project_id=project_id,
                records=tuple(records),
                cases=tuple(cases),
            )
            emitted += len(cases)
            if limit and emitted >= limit:
                return

    return DatasetBundle(info=info, batches=batches())
