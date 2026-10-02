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

## Multi-benchmark retrieval run

`benchmarks/measure_memory.py` streams four public corpora into the real SQLite store and reports evidence recall, latency, output tokens, and peak memory. Datasets live outside the repository (paths are the `DEFAULT_PATHS` in that script).

```bash
uv run --extra benchmark python benchmarks/measure_memory.py --lexical-only \
  --output artifacts/measure-memory-lexical.json
```

Full lexical run, 2026-10-02 ([artifact](../artifacts/measure-memory-lexical.json)). All hard gates passed: zero secret and cross-project leakage, sessions forgotten on close, at most 400 recall tokens, and identical CLI/MCP results.

| Corpus | Cases | Evidence | Recall @3 | Recall @10 |
|---|---:|---|---:|---:|
| LoCoMo | 1,982 | official dialog IDs | 0.50 | 0.65 |
| MemBench | 3,553 | official step IDs | 0.90 | 0.99 |
| MemoryAgentBench | 1,663 | derived answer-bearing chunks | 0.90 | 0.93 |
| BEAM 100K | 344 | official source chat IDs | 0.52 | 0.65 |
| Balanced mean | | | 0.70 | 0.80 |

Recall p95 was 114 ms on the largest corpus, output p95 was 163 tokens, and peak RSS was 290 MB.

The optional semantic path (`--extra semantic`, local FastEmbed, FTS-weighted reciprocal-rank fusion) was compared on an identical 25-case-per-corpus sample:

| Mode | Balanced @10 | Balanced @3 | Peak RSS | Wall time |
|---|---:|---:|---:|---:|
| Lexical | 0.75 | 0.57 | 107 MB | 5 s |
| Hybrid, BGE-small, FTS 2.0 : semantic 1.0 | 0.77 | 0.58 | 624 MB | 77 s |
| Hybrid, FTS 2.0 : semantic 0.5 | 0.76 | | 621 MB | 78 s |
| Hybrid, FTS 2.0 : semantic 2.0 | 0.73 | | | |

Hybrid helps BEAM and MemoryAgentBench but slightly hurts LoCoMo and MemBench, so lexical stays the default and semantic remains opt-in. Remaining misses are aggregation and multi-hop questions where the evidence is outside the top 10 for both rankers; write-time enrichment (below) closes part of that gap.

### Write-time enrichment

`unimem enrich` (`--extra enrich`, Apple Silicon) rewrites each stored memory once with a local model and indexes the result next to the original text. Recall stays a lexical query with no model call.

```bash
uv run --extra enrich --extra benchmark python benchmarks/measure_memory.py --lexical-only \
  --enrich-model mlx-community/LFM2.5-1.2B-Instruct-4bit \
  --membench /nonexistent --memoryagentbench --beam /nonexistent \
  --output artifacts/measure-memory-enriched-locomo.json
```

Full LoCoMo, 1,982 evidence questions ([artifact](../artifacts/measure-memory-enriched-locomo.json)):

| Mode | Recall @3 | Recall @10 | All evidence @10 | MRR @10 | Recall p95 |
|---|---:|---:|---:|---:|---:|
| Lexical | 0.50 | 0.65 | 0.56 | 0.67 | 3.4 ms |
| Enriched, column weight 1.0 | 0.58 | 0.76 | 0.68 | 0.62 | 6.2 ms |
| Enriched, column weight 0.5 | 0.60 | 0.78 | 0.69 | 0.65 | 5.9 ms |
| Enriched, column weight 0.33 | 0.61 | 0.77 | 0.68 | 0.67 | |
| **Enriched, column weight 0.25 (default)** | **0.61** | **0.76** | **0.67** | **0.68** | 5.7 ms |

The enrichment column's bm25 weight is `Database.fts_column_weights`. At 0.25 the generated text widens matching without outranking the original memory: it is the only setting that improved every LoCoMo category and did not lower any corpus on the 25-case sample ([artifact](../artifacts/measure-memory-enriched-sample.json): balanced @10 0.76 versus 0.75 lexical). Per category at the default, recall @10 moved 0.46 → 0.57 multi-hop, 0.74 → 0.80 temporal, 0.40 → 0.42 commonsense, 0.70 → 0.82 single-hop, and 0.66 → 0.81 adversarial.

Enrichment cost about 0.12 s per memory in 16-way batches with 0.94 GB peak RSS for the benchmark process; it is paid once per memory, never at recall. MemBench, MemoryAgentBench, and BEAM have only been measured enriched on the 25-case sample, not in full.

Model choice was made with a one-conversation probe (LoCoMo conversation 26, 197 questions, 419 turns, sequential generation). The probe scripts were not kept in the repository, so this table is a record rather than a reproducible result:

| Model (4-bit MLX unless noted) | Recall @3 | Recall @10 | Enrich time | Peak RSS |
|---|---:|---:|---:|---:|
| None | 0.52 | 0.69 | | |
| **LFM2.5 1.2B Instruct** | 0.62 | **0.83** | 115 s | 0.84 GB |
| Qwen3.5 2B | 0.67 | 0.79 | 216 s | 1.7 GB |
| Llama 3.2 1B Instruct | 0.64 | 0.81 | 134 s | 1.7 GB |
| Qwen3 1.7B | 0.61 | 0.82 | 188 s | 1.3 GB |
| LFM2.5 2.6B Turbo Brilliance (GGUF IQ4_XS) | 0.65 | 0.83 | 381 s | 2.2 GB |
| Sharp MiniCPM5 2B (GGUF Q4_K_S) | 0.59 | 0.80 | 184 s | 4.5 GB |
| Qwen3.5 0.8B | 0.59 | 0.77 | 112 s | 1.0 GB |
| Qwen2.5 0.5B Instruct | 0.59 | 0.75 | 93 s | 0.56 GB |
| Qwen3 0.6B | 0.56 | 0.75 | 97 s | 0.69 GB |
| Gemma 3 1B | 0.56 | 0.74 | 162 s | 2.0 GB |

No model beat LFM2.5 1.2B on recall while using less time and memory, so it is the default.

Limitations: these are retrieval metrics, not answer accuracy. MemoryAgentBench evidence is derived from answers appearing in context chunks. LongMemEval-V2 is inventory-only (451 questions) because the local data lacks the multimodal trajectories its evaluator needs. Published scores for other systems use LLM answer judges and are not directly comparable.


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
