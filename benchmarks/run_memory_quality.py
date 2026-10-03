#!/usr/bin/env python3
"""Measure unimem memory quality against source-backed capabilities.

The runner uses the real unimem extraction, SQLite retrieval, CLI, and MCP-tool
code paths. It can optionally ingest public LongMemEval or LoCoMo files without
vendoring their datasets into this repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from unimem.config import Settings  # noqa: E402
from unimem.db import Database, MemoryError  # noqa: E402
from unimem.extract import distill_messages  # noqa: E402
from unimem.mcp import run_tool  # noqa: E402
from unimem.policy import classify_route, compact_recall_items  # noqa: E402


ROUTING_TARGET = 0.90
EXTRACTION_PRECISION_TARGET = 0.90
EXTRACTION_RECALL_TARGET = 0.80
FIXTURE_TOP1_TARGET = 0.80
LOCOMO_RECALL3_TARGET = 0.70
LOCOMO_RECALL10_TARGET = 0.90


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ratio(numerator: float, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def timed(call: Callable[[], Any]) -> tuple[Any, float]:
    start = time.perf_counter()
    value = call()
    return value, (time.perf_counter() - start) * 1000


def run_routing(cases: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    misses: list[dict[str, Any]] = []
    trigger_correct = 0
    trigger_total = 0
    for case in cases:
        decision = classify_route(case["task"])
        expected = bool(case["should_recall"])
        actual = decision.should_recall
        if expected and actual:
            tp += 1
        elif not expected and actual:
            fp += 1
        elif not expected and not actual:
            tn += 1
        else:
            fn += 1
        if expected:
            trigger_total += 1
            if decision.trigger == case["trigger"]:
                trigger_correct += 1
        if expected != actual or (expected and decision.trigger != case["trigger"]):
            misses.append(
                {
                    "id": case["id"],
                    "expected": {"should_recall": expected, "trigger": case["trigger"]},
                    "actual": {"should_recall": actual, "trigger": decision.trigger},
                }
            )
    metrics = prf(tp, fp, fn)
    metrics.update(
        {
            "accuracy": ratio(tp + tn, len(cases)),
            "trigger_accuracy": ratio(trigger_correct, trigger_total),
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
            "misses": misses,
        }
    )
    metrics["target"] = ROUTING_TARGET
    metrics["meets_target"] = metrics["f1"] >= ROUTING_TARGET
    return metrics


def run_extraction(
    cases: list[dict[str, Any]],
    *,
    database: Database,
    settings: Settings,
) -> dict[str, Any]:
    tp = fp = fn = 0
    scope_correct = kind_correct = lifecycle_correct = 0
    accepted_expected = 0
    secret_leakage = 0
    misses: list[dict[str, Any]] = []
    for case in cases:
        result = distill_messages(
            settings=settings,
            database=database,
            messages=[{"role": "user", "content": case["content"]}],
            source=f"quality:{case['id']}",
            session_id="ses_quality",
            apply=False,
        )
        accepted = result["accepted"]
        expected = bool(case["accept"])
        actual = bool(accepted)
        if expected and actual:
            tp += 1
            accepted_expected += 1
            record = accepted[0]
            scope_correct += int(record["scope"] == case.get("scope"))
            kind_correct += int(record["kind"] == case.get("kind"))
            lifecycle_correct += int(record["lifecycle"] == case.get("lifecycle"))
        elif not expected and actual:
            fp += 1
        elif expected and not actual:
            fn += 1
        for record in accepted:
            lowered = record["content"].lower()
            if "sk-" in lowered or "api_key=" in lowered or "token=" in lowered:
                secret_leakage += 1
        if expected != actual:
            misses.append(
                {
                    "id": case["id"],
                    "expected_accept": expected,
                    "actual_accept": actual,
                    "rejected_reason": result["rejected"][0]["reason"] if result["rejected"] else None,
                }
            )
    metrics = prf(tp, fp, fn)
    metrics.update(
        {
            "scope_accuracy": ratio(scope_correct, accepted_expected),
            "kind_accuracy": ratio(kind_correct, accepted_expected),
            "lifecycle_accuracy": ratio(lifecycle_correct, accepted_expected),
            "secret_leakage": secret_leakage,
            "misses": misses,
            "precision_target": EXTRACTION_PRECISION_TARGET,
            "recall_target": EXTRACTION_RECALL_TARGET,
            "meets_target": metrics["precision"] >= EXTRACTION_PRECISION_TARGET
            and metrics["recall"] >= EXTRACTION_RECALL_TARGET,
        }
    )
    return metrics


def seed_fixture_memories(database: Database) -> dict[str, str]:
    memories = {
        "user": "Use pnpm for package management.",
        "project_a": "Use SQLite for local memory.",
        "project_b": "Use Postgres for the search service.",
        "procedure": "Release runbook: run uv build and the E2E suite.",
    }
    database.add_memory(
        content=memories["user"],
        scope="user",
        lifecycle="semantic",
        kind="preference",
        source="quality:user",
        evidence="User preference fixture.",
        confidence=0.95,
    )
    database.add_memory(
        content=memories["project_a"],
        scope="project",
        lifecycle="semantic",
        kind="decision",
        source="quality:project-a",
        evidence="Project A decision fixture.",
        project_id="benchmark:project-a",
        confidence=0.95,
    )
    database.add_memory(
        content=memories["project_b"],
        scope="project",
        lifecycle="semantic",
        kind="decision",
        source="quality:project-b",
        evidence="Project B decision fixture.",
        project_id="benchmark:project-b",
        confidence=0.95,
    )
    database.add_memory(
        content=memories["procedure"],
        scope="project",
        lifecycle="procedural",
        kind="procedure",
        source="quality:procedure",
        evidence="Repeated release procedure fixture.",
        project_id="benchmark:project-a",
        confidence=0.95,
    )
    old, _ = database.add_memory(
        content="API rate limit is 100 requests per minute.",
        scope="project",
        lifecycle="semantic",
        kind="fact",
        source="quality:old-limit",
        evidence="Superseded limit fixture.",
        project_id="benchmark:project-a",
    )
    database.add_memory(
        content="API rate limit is 200 requests per minute.",
        scope="project",
        lifecycle="semantic",
        kind="fact",
        source="quality:new-limit",
        evidence="Current limit fixture.",
        project_id="benchmark:project-a",
        supersedes=old.id,
    )
    database.add_memory(
        content="Knowledge update fixture: use the new authentication adapter.",
        scope="project",
        lifecycle="semantic",
        kind="fact",
        source="quality:test-time-learning",
        evidence="New information learned during the benchmark.",
        project_id="benchmark:project-a",
    )
    return memories


def run_fixture_retrieval(
    cases: list[dict[str, Any]],
    *,
    database: Database,
) -> dict[str, Any]:
    project_for_expected = {
        "Use SQLite for local memory.": "benchmark:project-a",
        "Use Postgres for the search service.": "benchmark:project-b",
    }
    top1 = 0
    recall3 = 0
    foreign_project_hits = 0
    recalls: list[float] = []
    misses: list[dict[str, Any]] = []
    special_results: dict[str, bool] = {}
    for case in cases:
        project_id = project_for_expected.get(case["expected"], "benchmark:project-a")
        records, latency = timed(
            lambda: database.recall(
                query=case["query"],
                project_id=project_id,
                session_id=None,
                limit=5,
            )
        )
        recalls.append(latency)
        contents = [record.content for record in records]
        foreign_project_hits += sum(
            1
            for record in records
            if record.project_id not in {None, project_id}
        )
        if contents and contents[0] == case["expected"]:
            top1 += 1
        if case["id"] == "current-rate-limit":
            special_results["knowledge_update_current_first"] = bool(
                contents and contents[0] == case["expected"]
            )
        if case["id"] == "test-time-learning":
            special_results["test_time_learning_recalled"] = case["expected"] in contents[:3]
        if case["expected"] in contents[:3]:
            recall3 += 1
        else:
            misses.append({"id": case["id"], "expected": case["expected"], "actual": contents})
    return {
        "top1_accuracy": ratio(top1, len(cases)),
        "recall_at_3": ratio(recall3, len(cases)),
        "foreign_project_hits": foreign_project_hits,
        "latency_ms": {
            "p50": percentile(recalls, 0.5),
            "p95": percentile(recalls, 0.95),
        },
        "target": FIXTURE_TOP1_TARGET,
        "meets_target": ratio(top1, len(cases)) >= FIXTURE_TOP1_TARGET,
        "misses": misses,
        **special_results,
    }


def run_lifecycle(database: Database) -> dict[str, Any]:
    session = database.start_session(
        project_id="benchmark:project-a",
        client="memory-quality",
        title="lifecycle",
        ttl_seconds=3600,
    )
    database.add_memory(
        content="Temporary lifecycle hypothesis should disappear after close.",
        scope="session",
        lifecycle="episodic",
        kind="hypothesis",
        source="quality:lifecycle",
        evidence="Session lifecycle fixture.",
        session_id=session.id,
    )
    before = database.recall(
        query="lifecycle hypothesis disappear",
        project_id="benchmark:project-a",
        session_id=session.id,
        limit=5,
    )
    before_target = any("Temporary lifecycle hypothesis" in record.content for record in before)
    database.end_session(session.id)
    after = database.recall(
        query="lifecycle hypothesis disappear",
        project_id="benchmark:project-a",
        session_id=session.id,
        limit=5,
    )
    after_target = any("Temporary lifecycle hypothesis" in record.content for record in after)
    return {
        "active_target_visible": before_target,
        "closed_target_visible": after_target,
        "forgetting_rate": 1.0 if before_target and not after_target else 0.0,
    }


def run_capacity(database: Database, *, count: int, queries: int) -> dict[str, Any]:
    project_id = "benchmark:capacity"
    insert_latencies: list[float] = []
    start = time.perf_counter()
    for index in range(count):
        _, latency = timed(
            lambda index=index: database.add_memory(
                content=f"Capacity token {index} describes local retrieval scale.",
                scope="project",
                lifecycle="semantic",
                kind="fact",
                source=f"quality:capacity:{index}",
                evidence="Capacity benchmark fixture.",
                project_id=project_id,
            )
        )
        insert_latencies.append(latency)
    total_insert_seconds = time.perf_counter() - start
    if database.semantic_embedder:
        database.embed_pending()

    query_latencies: list[float] = []
    estimated_tokens: list[int] = []
    for index in range(queries):
        records, latency = timed(
            lambda index=index: database.recall(
                query=f"Capacity token {index % count}",
                project_id=project_id,
                session_id=None,
                limit=10,
            )
        )
        _, _, estimated = compact_recall_items(records)
        query_latencies.append(latency)
        estimated_tokens.append(estimated)
    return {
        "memory_count": count,
        "query_count": queries,
        "insert_seconds": total_insert_seconds,
        "inserts_per_second": count / total_insert_seconds if total_insert_seconds else 0.0,
        "insert_latency_ms": {
            "p50": percentile(insert_latencies, 0.5),
            "p95": percentile(insert_latencies, 0.95),
        },
        "recall_latency_ms": {
            "p50": percentile(query_latencies, 0.5),
            "p95": percentile(query_latencies, 0.95),
        },
        "estimated_output_tokens": {
            "p50": percentile([float(value) for value in estimated_tokens], 0.5),
            "p95": percentile([float(value) for value in estimated_tokens], 0.95),
            "max": max(estimated_tokens, default=0),
        },
    }


def run_parity(
    *,
    database: Database,
    settings: Settings,
    home: Path,
    semantic_model: str | None,
) -> dict[str, Any]:
    query = "package manager preference"
    db_records = database.recall(
        query=query,
        project_id="benchmark:project-a",
        session_id=None,
        limit=3,
    )
    mcp_payload = run_tool(
        {
            "action": "recall",
            "query": query,
            "trigger": "memory_query",
            "evidence": "Quality benchmark parity query.",
            "semantic": bool(semantic_model),
            "semantic_model": semantic_model,
        },
        settings,
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    command = [
        sys.executable,
        "-m",
        "unimem",
        "--home",
        str(home),
        "--project-id",
        "benchmark:project-a",
        "recall",
        query,
        "--trigger",
        "memory_query",
        "--evidence",
        "Quality benchmark parity query.",
        "--json",
    ]
    if semantic_model:
        command.extend(["--semantic-model", semantic_model])
    result = subprocess.run(command, text=True, capture_output=True, check=False, env=env)
    try:
        cli_payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        cli_payload = {}
    db_items, _, _ = compact_recall_items(db_records)
    db_ids = [item["id"] for item in db_items]
    mcp_ids = [item["id"] for item in mcp_payload.get("items", [])]
    cli_ids = [item["id"] for item in cli_payload.get("items", [])]
    equal = db_ids == mcp_ids == cli_ids
    return {
        "database_ids": db_ids,
        "mcp_ids": mcp_ids,
        "cli_ids": cli_ids,
        "equal": equal,
        "cli_returncode": result.returncode,
    }


def iter_locomo_turns(sample: dict[str, Any]) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    conversation = sample.get("conversation", {})
    for key, value in conversation.items():
        if not key.startswith("session_") or key.endswith("_date_time"):
            continue
        if not isinstance(value, list):
            continue
        session_date = str(conversation.get(f"{key}_date_time", "")).strip()
        for turn in value:
            if not isinstance(turn, dict):
                continue
            dia_id = str(turn.get("dia_id", "")).strip()
            text = str(turn.get("text", "")).strip()
            speaker = str(turn.get("speaker", "")).strip()
            metadata = [
                str(turn.get("blip_caption", "")).strip(),
                str(turn.get("query", "")).strip(),
            ]
            context = " ".join(part for part in [text, *metadata] if part)
            if dia_id and text:
                turns.append(
                    {"dia_id": dia_id, "content": f"{speaker}: {context}", "date": session_date}
                )
    return turns


def parse_locomo_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%I:%M %p on %d %B, %Y").date()
    except ValueError:
        return None


class CachedEnricher:
    """Disk cache around LocalEnricher so benchmark reruns skip generation."""

    def __init__(self, enricher: Any, cache_path: Path):
        self._enricher = enricher
        self.model_name = enricher.model_name
        self.cache_path = cache_path
        self._cache: dict[str, str] = (
            json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        )
        self.generated = 0
        self.generation_seconds = 0.0

    def enrich_many(self, items: list[Any]) -> list[str]:
        from unimem.enrich import build_prompt

        keys = [
            hashlib.sha256(f"{self.model_name}\n{build_prompt(item)}".encode()).hexdigest()
            for item in items
        ]
        missing = [index for index, key in enumerate(keys) if key not in self._cache]
        if missing:
            start = time.perf_counter()
            texts = self._enricher.enrich_many([items[index] for index in missing])
            self.generation_seconds += time.perf_counter() - start
            self.generated += len(missing)
            for index, text in zip(missing, texts):
                self._cache[keys[index]] = text
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache), encoding="utf-8")
        return [self._cache[key] for key in keys]


def make_enricher(model: str | None, cache_dir: Path) -> CachedEnricher | None:
    if not model:
        return None
    from unimem.enrich import LocalEnricher

    safe = model.replace("/", "--")
    return CachedEnricher(LocalEnricher(model), cache_dir / f"enrich-{safe}.json")


def run_locomo(
    path: Path,
    *,
    database: Database,
    limit: int | None = None,
    enricher: Any = None,
) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    categories = {
        1: "multi_hop",
        2: "temporal",
        3: "commonsense",
        4: "single_hop",
        5: "adversarial",
    }
    total_questions = evidence_questions = 0
    hit3 = hit10 = full10 = session_hit10 = 0
    session_fraction_sum = 0.0
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    by_category: dict[str, dict[str, int]] = {}
    miss_examples: list[dict[str, Any]] = []

    for sample in data:
        sample_id = str(sample["sample_id"])
        project_id = f"locomo:{sample_id}"
        source_for_dia = {}
        dates: dict[str, date | None] = {}
        for turn in iter_locomo_turns(sample):
            source = f"locomo:{sample_id}:{turn['dia_id']}"
            source_for_dia[turn["dia_id"]] = source
            dates[source] = parse_locomo_date(turn["date"])
            database.add_memory(
                content=turn["content"][:4000],
                scope="project",
                lifecycle="episodic",
                kind="fact",
                source=source,
                evidence=f"LoCoMo dialog {turn['dia_id']}.",
                project_id=project_id,
                confidence=1.0,
            )
        if database.semantic_embedder:
            database.embed_pending()
        if enricher:
            database.enrich_pending(enricher, observed_on=lambda row: dates.get(row["source"]))

        for qa in sample.get("qa", []):
            total_questions += 1
            evidence = [str(item) for item in qa.get("evidence") or []]
            category = categories.get(int(qa.get("category", 0)), "unknown")
            stats = by_category.setdefault(category, {"questions": 0, "evidence_questions": 0, "hit3": 0, "hit10": 0, "full10": 0})
            stats["questions"] += 1
            if not evidence:
                # Published session-recall scores count these as 1.0.
                session_fraction_sum += 1.0
                continue
            evidence_questions += 1
            stats["evidence_questions"] += 1
            expected = [source_for_dia[item] for item in evidence if item in source_for_dia]
            records, latency = timed(
                lambda: database.recall(
                    query=str(qa.get("question", "")),
                    project_id=project_id,
                    session_id=None,
                    limit=10,
                    scopes=("project",),
                )
            )
            latencies.append(latency)
            returned = [record.source for record in records]
            ranks = [returned.index(source) + 1 for source in expected if source in returned]
            if any(rank <= 3 for rank in ranks):
                hit3 += 1
                stats["hit3"] += 1
            if ranks:
                hit10 += 1
                stats["hit10"] += 1
                reciprocal_ranks.append(1.0 / min(ranks))
            if ranks and len(ranks) == len(expected):
                full10 += 1
                stats["full10"] += 1
            # Dialog IDs look like "D3:5"; the prefix is the LoCoMo session.
            evidence_sessions = {item.split(":")[0] for item in evidence}
            returned_sessions = {source.rsplit(":", 2)[1] for source in returned}
            if evidence_sessions & returned_sessions:
                session_hit10 += 1
            session_fraction_sum += len(evidence_sessions & returned_sessions) / len(evidence_sessions)
            if not ranks or min(ranks) > 3:
                if len(miss_examples) < 20:
                    miss_examples.append(
                        {
                            "question": qa.get("question"),
                            "category": category,
                            "expected_sources": expected,
                            "returned_sources": returned,
                            "expected_ranks": ranks,
                        }
                    )
            if limit and evidence_questions >= limit:
                break
        if limit and evidence_questions >= limit:
            break

    for stats in by_category.values():
        denominator = stats["evidence_questions"]
        stats["recall_at_3"] = ratio(stats["hit3"], denominator)
        stats["recall_at_10"] = ratio(stats["hit10"], denominator)
        stats["full_recall_at_10"] = ratio(stats["full10"], denominator)
    return {
        "dataset": "LoCoMo",
        "dataset_url": "https://github.com/snap-research/LoCoMo",
        "dataset_license": "CC BY-NC 4.0",
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "samples": len(data),
        "questions": total_questions,
        "evidence_questions": evidence_questions,
        "evidence_recall_at_3": ratio(hit3, evidence_questions),
        "evidence_recall_at_10": ratio(hit10, evidence_questions),
        "full_evidence_recall_at_10": ratio(full10, evidence_questions),
        "session_recall_at_10": ratio(session_hit10, evidence_questions),
        # Mean fraction of evidence sessions among the sessions of the ten
        # returned memories, over all questions: the formula other systems
        # publish for session-granularity LoCoMo recall.
        "session_fractional_recall_at_10": ratio(session_fraction_sum, total_questions),
        "mrr_at_10": sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
        },
        "by_category": by_category,
        "miss_examples": miss_examples,
        "targets": {
            "evidence_recall_at_3": LOCOMO_RECALL3_TARGET,
            "evidence_recall_at_10": LOCOMO_RECALL10_TARGET,
        },
        "meets_targets": ratio(hit3, evidence_questions) >= LOCOMO_RECALL3_TARGET
        and ratio(hit10, evidence_questions) >= LOCOMO_RECALL10_TARGET,
        "scope": "retrieval-only; not answer-generation accuracy",
    }


def iter_longmemeval(path: Path) -> Iterator[dict[str, Any]]:
    """Stream questions so the 265 MB file never sits in memory at once."""
    try:
        import ijson
    except ImportError:
        yield from json.loads(path.read_text(encoding="utf-8"))
        return
    with path.open("rb") as handle:
        yield from ijson.items(handle, "item")


def run_longmemeval(path: Path, *, database: Database, limit: int | None = None) -> dict[str, Any]:
    """Session-evidence recall with one memory per turn.

    Follows the official retrieval evaluation in skipping abstention
    questions, which have no evidence location. A question counts as a hit
    when any evidence session appears among the sessions of the returned
    memories.
    """
    counts = {"hit5": 0, "hit10": 0, "distinct5": 0}
    by_type: dict[str, dict[str, int]] = {}
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    evaluated = abstention_skipped = turns = rejected_turns = 0
    for item in iter_longmemeval(path):
        question_id = str(item["question_id"])
        if question_id.endswith("_abs"):
            abstention_skipped += 1
            continue
        if limit and evaluated >= limit:
            break
        project_id = f"longmemeval:{question_id}"
        for session_index, session_id_value in enumerate(item.get("haystack_session_ids", [])):
            session_id = str(session_id_value)
            for turn_index, turn in enumerate(item.get("haystack_sessions", [])[session_index]):
                try:
                    database.add_memory(
                        content=f"{turn.get('role', 'user')}: {turn.get('content', '')}"[:4000],
                        scope="project",
                        lifecycle="episodic",
                        kind="fact",
                        source=f"lme|{question_id}|{session_id}|{turn_index}",
                        evidence=f"LongMemEval session {session_id}.",
                        project_id=project_id,
                        confidence=1.0,
                    )
                    turns += 1
                except MemoryError:
                    # The store refuses secret-looking or empty turns.
                    rejected_turns += 1
        if database.semantic_embedder:
            database.embed_pending()
        records, latency = timed(
            lambda: database.recall(
                query=str(item["question"]),
                project_id=project_id,
                session_id=None,
                limit=20,
                scopes=("project",),
            )
        )
        latencies.append(latency)
        returned = [record.source.split("|")[2] for record in records]
        expected = {str(value) for value in item.get("answer_session_ids", [])}
        distinct = list(dict.fromkeys(returned))
        hits = {
            "hit5": bool(expected & set(returned[:5])),
            "hit10": bool(expected & set(returned[:10])),
            "distinct5": bool(expected & set(distinct[:5])),
        }
        stats = by_type.setdefault(
            str(item.get("question_type", "unknown")),
            {"questions": 0, "hit5": 0, "hit10": 0, "distinct5": 0},
        )
        stats["questions"] += 1
        for key, hit in hits.items():
            counts[key] += hit
            stats[key] += hit
        ranks = [index + 1 for index, session in enumerate(returned[:10]) if session in expected]
        if ranks:
            reciprocal_ranks.append(1.0 / ranks[0])
        evaluated += 1
    for stats in by_type.values():
        stats["session_recall_at_5"] = ratio(stats["hit5"], stats["questions"])
        stats["session_recall_at_10"] = ratio(stats["hit10"], stats["questions"])
    return {
        "dataset": "LongMemEval-S (cleaned)",
        "dataset_url": "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned",
        "dataset_license": "MIT",
        "evaluated": evaluated,
        "abstention_skipped": abstention_skipped,
        "turns_stored": turns,
        "turns_rejected_by_store": rejected_turns,
        "session_recall_at_5": ratio(counts["hit5"], evaluated),
        "session_recall_at_10": ratio(counts["hit10"], evaluated),
        "session_recall_at_5_distinct_sessions": ratio(counts["distinct5"], evaluated),
        "session_mrr_at_10": sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95)},
        "by_type": by_type,
        "scope": (
            "retrieval-only, one memory per turn; session_recall_at_5 uses the five "
            "returned memories, the distinct-sessions variant the first five distinct "
            "sessions among twenty returned memories"
        ),
    }


def main() -> int:
    started_at = now_iso()
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "benchmarks/quality_cases.json")
    parser.add_argument("--locomo", type=Path)
    parser.add_argument("--longmemeval", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--capacity", type=int, default=1000)
    parser.add_argument("--capacity-queries", type=int, default=50)
    parser.add_argument("--semantic-model")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/memory-quality.json")
    args = parser.parse_args()

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="unimem-quality-") as temp:
        home = Path(temp)
        settings = Settings.load(
            project_dir=ROOT,
            project_id="benchmark:project-a",
            home=home,
        )
        database = Database(settings)
        database.initialize()
        semantic_model = args.semantic_model or None
        if semantic_model:
            database.enable_semantic(model_name=semantic_model, embed_on_write=False)
        seed_fixture_memories(database)
        if semantic_model:
            database.embed_pending()

        routing = run_routing(cases["routing"])
        extraction = run_extraction(cases["extraction"], database=database, settings=settings)
        fixture_retrieval = run_fixture_retrieval(cases["retrieval"], database=database)
        lifecycle = run_lifecycle(database)
        capacity = run_capacity(database, count=args.capacity, queries=args.capacity_queries)
        parity = run_parity(
            database=database,
            settings=settings,
            home=home,
            semantic_model=semantic_model,
        )
        locomo = (
            run_locomo(args.locomo, database=database, limit=args.limit)
            if args.locomo
            else {"status": "not_run", "reason": "LoCoMo dataset path not provided"}
        )
        longmemeval = (
            run_longmemeval(args.longmemeval, database=database, limit=args.limit)
            if args.longmemeval
            else {"status": "not_run", "reason": "LongMemEval dataset path not provided"}
        )

    hard_gates = {
        "secret_leakage": extraction["secret_leakage"] == 0,
        "scope_leakage": extraction["scope_accuracy"] == 1.0
        and fixture_retrieval["foreign_project_hits"] == 0,
        "session_forgetting": lifecycle["forgetting_rate"] == 1.0,
        "recall_token_budget": capacity["estimated_output_tokens"]["max"] <= 400,
        "cli_mcp_parity": parity["equal"],
    }
    quality_targets = {
        "routing": routing["meets_target"],
        "extraction": extraction["meets_target"],
        "fixture_retrieval": fixture_retrieval["meets_target"],
        "knowledge_update": fixture_retrieval.get("knowledge_update_current_first", False),
        "test_time_learning": fixture_retrieval.get("test_time_learning_recalled", False),
        "locomo": locomo.get("meets_targets", True),
    }
    payload = {
        "passed": all(hard_gates.values()) and all(quality_targets.values()),
        "command": "uv run python benchmarks/run_memory_quality.py "
        + shlex.join(sys.argv[1:]),
        "started_at": started_at,
        "finished_at": now_iso(),
        "python": sys.version,
        "hard_gates": hard_gates,
        "quality_targets": quality_targets,
        "metrics": {
            "routing": routing,
            "extraction": extraction,
            "fixture_retrieval": fixture_retrieval,
            "knowledge_update": {
                "current_first": fixture_retrieval.get("knowledge_update_current_first", False),
                "target": True,
            },
            "test_time_learning": {
                "recalled": fixture_retrieval.get("test_time_learning_recalled", False),
                "target": True,
            },
            "lifecycle": lifecycle,
            "capacity_and_efficiency": capacity,
            "cli_mcp_parity": parity,
            "semantic": {
                "enabled": bool(semantic_model),
                "model": semantic_model,
            },
            "locomo": locomo,
            "longmemeval": longmemeval,
        },
        "source_alignment": {
            "LongMemEval": ["information extraction", "multi-session reasoning", "temporal reasoning", "knowledge updates", "abstention"],
            "MemoryAgentBench": ["accurate retrieval", "test-time learning", "long-range understanding", "conflict resolution"],
            "MemBench": ["effectiveness", "efficiency", "capacity"],
            "Harness the Memory": ["performance", "efficiency", "regime-aware retrieval"],
        },
        "limitations": [
            "LoCoMo and LongMemEval results measure evidence retrieval, not model-generated answer accuracy.",
            "MemoryAgentBench's full incremental agent harness requires external model APIs and is represented here by controlled local capability cases.",
            "Latency and capacity figures depend on the local Python and SQLite runtime.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": payload["passed"],
                "output": str(args.output),
                "hard_gates": hard_gates,
                "quality_targets": quality_targets,
            }
        )
    )
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
