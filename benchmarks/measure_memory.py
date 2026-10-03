#!/usr/bin/env python3
"""Reproducible composite measurement for unimem memory quality and overhead."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import shlex
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for import_root in (ROOT, SRC):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from benchmarks.adapters import (  # noqa: E402
    DatasetBundle,
    RetrievalCase,
    load_beam_dataset,
    load_membench_dataset,
    load_memoryagentbench_dataset,
)
from benchmarks.run_memory_quality import (  # noqa: E402
    compact_recall_items,
    make_enricher,
    now_iso,
    percentile,
    ratio,
    run_capacity,
    run_extraction,
    run_fixture_retrieval,
    run_lifecycle,
    run_locomo,
    run_longmemeval,
    run_parity,
    run_routing,
    seed_fixture_memories,
    timed,
)
from unimem.config import Settings  # noqa: E402
from unimem.db import Database  # noqa: E402

DEFAULT_PATHS = {
    "locomo": Path("/tmp/unimem-locomo10.json"),
    "membench": Path("/tmp/unimem-membench-source/MemData"),
    "memoryagentbench": [
        Path("/tmp/unimem-memoryagentbench-retrieval.parquet"),
        Path("/tmp/unimem-memoryagentbench-ttl.parquet"),
        Path("/tmp/unimem-memoryagentbench-lru.parquet"),
        Path("/tmp/unimem-memoryagentbench-conflict.parquet"),
    ],
    "beam": Path("/tmp/unimem-beam-100k.parquet"),
    "longmemeval_s": Path("/tmp/unimem-longmemeval_s_cleaned.json"),
    "longmemeval_v2_questions": Path("/tmp/unimem-lme-v2-questions.jsonl"),
    "longmemeval_v2_evidence": Path("/tmp/unimem-lme-v2-small.json"),
}


def existing_path(path: Path | None) -> Path | None:
    return path if path and path.exists() else None


def max_rss_mb() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024) if sys.platform == "darwin" else value / 1024


def load_cases(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_track(
    database: Database,
    bundle: DatasetBundle,
    *,
    semantic: bool,
    limit: int,
    enricher: Any = None,
) -> dict[str, Any]:
    info = bundle.info
    resolved_cases: list[RetrievalCase] = []
    attempted_records = ingested_records = 0
    rejected_sources: list[dict[str, str]] = []
    rejected_records = 0
    ingestion_excluded = 0
    generated_cases = 0

    for batch in bundle.batches:
        expected_sources = {
            source for case in batch.cases for source in case.expected_sources
        }
        source_aliases: dict[str, str] = {}
        for source, content in batch.records:
            attempted_records += 1
            try:
                record, _ = database.add_memory(
                    content=content[:4000],
                    scope="project",
                    lifecycle="episodic",
                    kind="fact",
                    source=source[:200],
                    evidence=f"{info.name} benchmark evidence record.",
                    project_id=batch.project_id,
                    confidence=1.0,
                )
            except Exception as error:
                rejected_records += 1
                if len(rejected_sources) < 20:
                    rejected_sources.append({"source": source, "error": str(error)})
                continue
            ingested_records += 1
            if source in expected_sources:
                source_aliases[source] = record.source
        for case in batch.cases:
            generated_cases += 1
            if any(source not in source_aliases for source in case.expected_sources):
                ingestion_excluded += 1
                continue
            resolved_cases.append(
                RetrievalCase(
                    case_id=case.case_id,
                    project_id=case.project_id,
                    category=case.category,
                    query=case.query,
                    expected_sources=tuple(
                        dict.fromkeys(
                            source_aliases[source] for source in case.expected_sources
                        )
                    ),
                )
            )
        if limit and len(resolved_cases) >= limit:
            break
    if database.semantic_embedder:
        database.embed_pending()
    if enricher:
        database.enrich_pending(enricher)

    hit3 = hit10 = full3 = full10 = 0
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    estimated_tokens: list[int] = []
    by_category: dict[str, dict[str, int]] = {}
    miss_examples: list[dict[str, Any]] = []

    for case in resolved_cases[:limit or len(resolved_cases)]:
        stats = by_category.setdefault(
            case.category,
            {"cases": 0, "hit3": 0, "hit10": 0, "full3": 0, "full10": 0},
        )
        stats["cases"] += 1
        records, latency = timed(
            lambda: database.recall(
                query=case.query,
                project_id=case.project_id,
                session_id=None,
                limit=10,
                scopes=("project",),
                semantic=semantic,
            )
        )
        returned = [record.source for record in records]
        expected = case.expected_sources
        ranks = [returned.index(source) + 1 for source in expected if source in returned]
        _, _, token_estimate = compact_recall_items(records)
        latencies.append(latency)
        estimated_tokens.append(token_estimate)
        if any(rank <= 3 for rank in ranks):
            hit3 += 1
            stats["hit3"] += 1
        if ranks:
            hit10 += 1
            stats["hit10"] += 1
            reciprocal_ranks.append(1.0 / min(ranks))
        if ranks and len(ranks) == len(expected):
            if any(rank <= 3 for rank in ranks):
                full3 += 1
                stats["full3"] += 1
            full10 += 1
            stats["full10"] += 1
        if not ranks or min(ranks) > 3:
            if len(miss_examples) < 20:
                miss_examples.append(
                    {
                        "case_id": case.case_id,
                        "category": case.category,
                        "query": case.query[:500],
                        "expected_sources": list(expected),
                        "returned_sources": returned,
                        "expected_ranks": ranks,
                    }
                )

    evaluated = sum(stats["cases"] for stats in by_category.values())
    for stats in by_category.values():
        denominator = stats["cases"]
        stats["recall_at_3"] = ratio(stats["hit3"], denominator)
        stats["recall_at_10"] = ratio(stats["hit10"], denominator)
        stats["full_recall_at_10"] = ratio(stats["full10"], denominator)
    return {
        "dataset": info.name,
        "evidence_kind": info.evidence_kind,
        "dataset_paths": list(info.dataset_paths),
        "dataset_sha256": info.dataset_sha256,
        "corpus_records": attempted_records,
        "ingested_records": ingested_records,
        "rejected_records": rejected_records,
        "rejected_record_examples": rejected_sources[:10],
        "cases": generated_cases,
        "evaluated": evaluated,
        "excluded_cases": 0,
        "ingestion_excluded_cases": ingestion_excluded,
        "evidence_recall_at_3": ratio(hit3, evaluated),
        "evidence_recall_at_10": ratio(hit10, evaluated),
        "full_evidence_recall_at_3": ratio(full3, evaluated),
        "full_evidence_recall_at_10": ratio(full10, evaluated),
        "mrr_at_10": sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "latency_ms": {
            "p50": percentile(latencies, 0.5),
            "p95": percentile(latencies, 0.95),
        },
        "estimated_output_tokens": {
            "p50": percentile([float(value) for value in estimated_tokens], 0.5),
            "p95": percentile([float(value) for value in estimated_tokens], 0.95),
            "max": max(estimated_tokens, default=0),
        },
        "by_category": by_category,
        "miss_examples": miss_examples,
        "notes": info.notes,
    }


def longmemeval_v2_inventory(
    questions_path: Path | None,
    evidence_path: Path | None,
) -> dict[str, Any]:
    if not questions_path or not evidence_path:
        return {
            "status": "not_run",
            "reason": "LongMemEval-V2 question/evidence files were not provided",
        }
    questions = [
        json.loads(line)
        for line in questions_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    question_ids = {str(item["id"]) for item in questions}
    evidence_ids = {str(key) for key in evidence}
    return {
        "status": "inventory_only",
        "dataset": "LongMemEval-V2",
        "dataset_url": "https://github.com/xiaowu0162/LongMemEval-V2",
        "dataset_license": "MIT",
        "questions": len(questions),
        "evidence_cases": len(evidence),
        "question_evidence_id_overlap": len(question_ids & evidence_ids),
        "dataset_sha256": {
            str(questions_path): hashlib.sha256(questions_path.read_bytes()).hexdigest(),
            str(evidence_path): hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        },
        "reason": (
            "Official evaluation needs multimodal web-agent trajectories/screenshots; "
            "the local checkout contains question and evidence-step metadata only."
        ),
    }


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=ROOT / "benchmarks/quality_cases.json")
    parser.add_argument("--locomo", type=Path, default=DEFAULT_PATHS["locomo"])
    parser.add_argument("--membench", type=Path, default=DEFAULT_PATHS["membench"])
    parser.add_argument(
        "--memoryagentbench",
        type=Path,
        nargs="*",
        default=DEFAULT_PATHS["memoryagentbench"],
    )
    parser.add_argument("--beam", type=Path, default=DEFAULT_PATHS["beam"])
    parser.add_argument(
        "--longmemeval-s",
        type=Path,
        help="LongMemEval-S cleaned JSON (about 247,000 turns; off unless given)",
    )
    parser.add_argument(
        "--longmemeval-v2-questions",
        type=Path,
        default=DEFAULT_PATHS["longmemeval_v2_questions"],
    )
    parser.add_argument(
        "--longmemeval-v2-evidence",
        type=Path,
        default=DEFAULT_PATHS["longmemeval_v2_evidence"],
    )
    parser.add_argument("--semantic-model", default="BAAI/bge-base-en-v1.5")
    parser.add_argument("--lexical-only", action="store_true")
    parser.add_argument(
        "--enrich-model",
        help="Enrich ingested memories with this local MLX model before recall",
    )
    parser.add_argument(
        "--enrich-cache-dir",
        type=Path,
        default=Path("/tmp/unimem-enrich-cache"),
        help="Reuse model outputs across runs (keyed by model and prompt)",
    )
    parser.add_argument("--fts-weight", type=float, default=2.0)
    parser.add_argument("--semantic-weight", type=float, default=1.0)
    parser.add_argument("--rrf-k", type=int, default=60)
    parser.add_argument(
        "--fts-column-weights",
        type=float,
        nargs=3,
        metavar=("CONTENT", "EVIDENCE", "ENRICHMENT"),
        help="bm25 column weights (default: the Database defaults)",
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--capacity", type=int, default=1000)
    parser.add_argument("--capacity-queries", type=int, default=50)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/measure-memory.json")
    args = parser.parse_args()

    started_at = now_iso()
    wall_start = time.perf_counter()
    cpu_start = time.process_time()
    cases = load_cases(args.cases)
    locomo_path = existing_path(args.locomo)
    membench_path = existing_path(args.membench)
    memoryagentbench_paths = [existing_path(path) for path in args.memoryagentbench]
    memoryagentbench_paths = [path for path in memoryagentbench_paths if path]
    beam_path = existing_path(args.beam)
    longmemeval_questions = existing_path(args.longmemeval_v2_questions)
    longmemeval_evidence = existing_path(args.longmemeval_v2_evidence)

    with tempfile.TemporaryDirectory(prefix="unimem-measure-") as temp:
        home = Path(temp)
        settings = Settings.load(
            project_dir=ROOT,
            project_id="benchmark:project-a",
            home=home,
        )
        database = Database(settings)
        database.initialize()
        semantic_model = None if args.lexical_only else args.semantic_model or None
        if semantic_model:
            database.enable_semantic(model_name=semantic_model, embed_on_write=False)
        enricher = make_enricher(args.enrich_model, args.enrich_cache_dir)
        database.hybrid_fts_weight = args.fts_weight
        database.hybrid_semantic_weight = args.semantic_weight
        database.hybrid_rrf_k = args.rrf_k
        if args.fts_column_weights:
            database.fts_column_weights = tuple(args.fts_column_weights)
        seed_fixture_memories(database)
        if semantic_model:
            database.embed_pending()

        routing = run_routing(cases["routing"])
        extraction = run_extraction(cases["extraction"], database=database, settings=settings)
        fixture_retrieval = run_fixture_retrieval(cases["retrieval"], database=database)
        lifecycle = run_lifecycle(database)
        capacity = run_capacity(
            database,
            count=args.capacity,
            queries=args.capacity_queries,
        )
        parity = run_parity(
            database=database,
            settings=settings,
            home=home,
            semantic_model=semantic_model,
        )
        locomo = (
            run_locomo(locomo_path, database=database, limit=args.limit, enricher=enricher)
            if locomo_path
            else {"status": "not_run", "reason": "LoCoMo dataset path not provided"}
        )
        longmemeval_s = (
            run_longmemeval(args.longmemeval_s, database=database, limit=args.limit)
            if args.longmemeval_s
            else {"status": "not_run", "reason": "--longmemeval-s not provided"}
        )
        public_results: dict[str, dict[str, Any]] = {}
        if membench_path:
            bundle = load_membench_dataset(membench_path, limit=args.limit)
            public_results[bundle.info.name] = evaluate_track(
                database,
                bundle,
                semantic=bool(semantic_model),
                limit=args.limit or 0,
                enricher=enricher,
            )
            del bundle
        if memoryagentbench_paths:
            bundle = load_memoryagentbench_dataset(
                memoryagentbench_paths,
                limit=args.limit,
            )
            public_results[bundle.info.name] = evaluate_track(
                database,
                bundle,
                semantic=bool(semantic_model),
                limit=args.limit or 0,
                enricher=enricher,
            )
            del bundle
        if beam_path:
            bundle = load_beam_dataset(beam_path, limit=args.limit)
            public_results[bundle.info.name] = evaluate_track(
                database,
                bundle,
                semantic=bool(semantic_model),
                limit=args.limit or 0,
                enricher=enricher,
            )
            del bundle

    hard_gates = {
        "secret_leakage": extraction["secret_leakage"] == 0,
        "project_leakage": fixture_retrieval["foreign_project_hits"] == 0,
        "session_forgetting_rate": lifecycle["forgetting_rate"] == 1.0,
        "max_recall_tokens": capacity["estimated_output_tokens"]["max"] <= 400,
        "cli_mcp_parity": parity["equal"],
    }
    retrieval_values = [
        float(value)
        for value in [
            locomo.get("evidence_recall_at_10"),
            *(result.get("evidence_recall_at_10") for result in public_results.values()),
        ]
        if isinstance(value, (int, float))
    ]
    recall3_values = [
        float(value)
        for value in [
            locomo.get("evidence_recall_at_3"),
            *(result.get("evidence_recall_at_3") for result in public_results.values()),
        ]
        if isinstance(value, (int, float))
    ]
    balanced_public_retrieval = mean(retrieval_values)
    balanced_public_retrieval_at_3 = mean(recall3_values)
    max_output_tokens = max(
        [capacity["estimated_output_tokens"]["max"]]
        + [
            result["estimated_output_tokens"]["max"]
            for result in public_results.values()
        ]
    )
    latency_p95 = max(
        [capacity["recall_latency_ms"]["p95"]]
        + [result["latency_ms"]["p95"] for result in public_results.values()]
    )

    payload = {
        "passed": all(hard_gates.values()) and balanced_public_retrieval >= 0.95,
        "command": "uv run --extra semantic --extra benchmark python "
        + shlex.join([str(Path("benchmarks") / "measure_memory.py"), *sys.argv[1:]]),
        "started_at": started_at,
        "finished_at": now_iso(),
        "python": sys.version,
        "enrichment": {
            "enabled": enricher is not None,
            "model": args.enrich_model,
            "generated": enricher.generated if enricher else 0,
            "generation_seconds": enricher.generation_seconds if enricher else 0.0,
        },
        "fts_column_weights": list(database.fts_column_weights),
        "semantic": {
            "enabled": bool(semantic_model),
            "model": semantic_model,
            "fusion": {
                "fts_weight": args.fts_weight,
                "semantic_weight": args.semantic_weight,
                "rrf_k": args.rrf_k,
            },
        },
        "primary": {
            "name": "balanced_public_retrieval",
            "value": balanced_public_retrieval,
            "target": 0.95,
            "direction": "maximize",
        },
        "hard_gates": hard_gates,
        "diagnostics": {
            "balanced_public_retrieval_at_3": balanced_public_retrieval_at_3,
            "routing_f1": routing["f1"],
            "extraction_f1": extraction["f1"],
            "recall_latency_p95_ms": latency_p95,
            "estimated_output_tokens_p95": capacity["estimated_output_tokens"]["p95"],
            "max_recall_tokens": max_output_tokens,
            "max_rss_mb": max_rss_mb(),
            "cpu_seconds": time.process_time() - cpu_start,
            "wall_seconds": time.perf_counter() - wall_start,
        },
        "metrics": {
            "routing": routing,
            "extraction": extraction,
            "fixture_retrieval": fixture_retrieval,
            "lifecycle": lifecycle,
            "capacity_and_efficiency": capacity,
            "cli_mcp_parity": parity,
            "locomo": locomo,
            "longmemeval_s": longmemeval_s,
            "public_retrieval": public_results,
            "longmemeval_v2": longmemeval_v2_inventory(
                longmemeval_questions,
                longmemeval_evidence,
            ),
        },
        "limitations": [
            "LoCoMo, MemBench, and BEAM tracks measure evidence retrieval rather than answer generation.",
            "MemoryAgentBench evidence is derived from accepted answers appearing in context chunks.",
            "LongMemEval-V2 is inventory-only until multimodal trajectory data is available.",
            "Published system scores are not directly comparable unless model, judge, corpus, and budget match.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "passed": payload["passed"],
                "output": str(args.output),
                "balanced_public_retrieval": balanced_public_retrieval,
                "hard_gates": hard_gates,
            }
        )
    )
    return 0 if all(hard_gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
