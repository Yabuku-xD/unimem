# Memory quality benchmark

This benchmark turns the research report's evaluation dimensions into reproducible tests against the real `unimem` implementation.

## What it measures

- **Routing:** whether routine work avoids recall and missing-context work selects the right trigger.
- **Extraction:** precision, recall, scope, kind, and lifecycle classification on durable versus noisy claims.
- **Scoped retrieval:** top-1 and recall@3 over controlled user/project records, including project isolation.
- **Learning and updates:** test-time writes and current-value-first retrieval after supersession.
- **Forgetting:** session recall before and after closure.
- **Capacity and efficiency:** insertion throughput, recall latency, and bounded output tokens.
- **Cross-surface parity:** identical IDs from the database, CLI, and MCP-tool implementation.
- **Public-corpus retrieval:** LoCoMo evidence-ID recall and, optionally, LongMemEval session recall.

The public-corpus tracks measure retrieval evidence only. They do not claim answer-generation accuracy because the official full evaluators use model-generated answers and LLM judges.

## Run the offline capability suite

```bash
uv run python benchmarks/run_memory_quality.py
```

This writes [`artifacts/memory-quality.json`](../artifacts/memory-quality.json).

## Run the real LoCoMo corpus

LoCoMo is licensed CC BY-NC 4.0 and is not vendored into this repository. Download it for noncommercial evaluation, then run:

```bash
curl -fsSL https://raw.githubusercontent.com/snap-research/LoCoMo/main/data/locomo10.json \
  -o /tmp/locomo10.json
uv run python benchmarks/run_memory_quality.py \
  --locomo /tmp/locomo10.json \
  --capacity 1000 \
  --capacity-queries 50
```

The dataset contains 10 conversations, 1,986 questions, and evidence dialog IDs. The runner reports evidence recall by LoCoMo category and preserves a sample of misses in the artifact.

## Run LongMemEval retrieval

LongMemEval is MIT-licensed. Its cleaned LongMemEval-S file is large, so it is also downloaded outside the repository:

```bash
curl -fsSL \
  https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
  -o /tmp/longmemeval_s_cleaned.json
uv run python benchmarks/run_memory_quality.py \
  --longmemeval /tmp/longmemeval_s_cleaned.json \
  --limit 50
```

[LongMemEval-V2](https://github.com/xiaowu0162/LongMemEval-V2) now targets multimodal web-agent trajectories, but this runner uses the established LongMemEval session-evidence format for direct comparison.

## Current LoCoMo result

The run on 2026-10-02 used the official `locomo10.json`, SHA-256 `79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4`.

| Metric | Result | Target |
|---|---:|---:|
| Evidence recall @3 | 0.50 | 0.70 |
| Evidence recall @10 | 0.65 | 0.90 |
| Full evidence recall @10 | 0.56 | Report only |
| MRR @10 | 0.67 | Report only |

Category recall @10 was 0.74 temporal, 0.70 single-hop, 0.66 adversarial, 0.46 multi-hop, and 0.40 commonsense. The shortfall is concentrated in semantic and multi-evidence reasoning. Increasing `top-k` alone would inflate context without fixing ranking.

## Source-derived acceptance mapping

| Research source | Dimension | Benchmark track |
|---|---|---|
| Self-RAG / Adaptive-RAG | Retrieval only when needed | Routing |
| Codex / Claude Code memory docs | Selective, secret-aware writes | Extraction |
| LongMemEval | Extraction, multi-session, temporal, updates, abstention | Controlled cases and LoCoMo/LongMemEval retrieval |
| MemoryAgentBench | Retrieval, learning, long-range understanding, conflict resolution | Fixture retrieval, updates, forgetting |
| MemBench | Effectiveness, efficiency, capacity | Capability suite and capacity track |
| Harness the Memory | Performance and efficiency across regimes | Routing, retrieval, latency, token output |

## Source audit

Re-check every URL cited by the research report with:

```bash
uv run python benchmarks/check_sources.py
```

The result is written to [`artifacts/source-check.json`](../artifacts/source-check.json).
