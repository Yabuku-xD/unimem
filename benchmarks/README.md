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
  -o /tmp/unimem-longmemeval_s_cleaned.json
uv run python benchmarks/run_memory_quality.py \
  --longmemeval /tmp/unimem-longmemeval_s_cleaned.json \
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

| Corpus | Cases | Evidence | Recall @3 | Recall @10 | Before index tuning @3 / @10 |
|---|---:|---|---:|---:|---:|
| LoCoMo | 1,982 | official dialog IDs | 0.54 | 0.69 | 0.50 / 0.65 |
| MemBench | 3,553 | official step IDs | 0.92 | 0.98 | 0.90 / 0.99 |
| MemoryAgentBench | 1,664 | derived answer-bearing chunks | 0.90 | 0.93 | 0.90 / 0.93 |
| BEAM 100K | 354 | official source chat IDs | 0.50 | 0.67 | 0.52 / 0.65 |
| Balanced mean | | | 0.71 | 0.82 | 0.70 / 0.80 |

Recall p95 on the slowest corpus fell from 114 ms to 10 ms, output p95 was 163 tokens, and peak RSS was 333 MB for the benchmark process.

### Index tuning

Four index changes produced the right-hand improvement and are now the defaults in `src/unimem/db.py`:

- Porter stemming in the FTS5 tokenizer, so "suites" matches "suite".
- A larger stopword list, and prefix matching only for query terms of four or more characters.
- A `scope_key` index column, so a query is narrowed to the caller's scopes inside the index instead of scoring every project's rows. This is the latency win; the SQL scope filters remain authoritative.
- Evidence text weighted 0.5 and enrichment 0.25 against content 1.0 (`Database.fts_column_weights`).

Two ideas were measured and rejected:

- **Embedding hybrid on top of the tuned index.** BGE-small fused by reciprocal rank scored 0.59 / 0.79 on enriched LoCoMo against 0.62 / 0.79 for the keyword index alone, and embeddings alone scored 0.41 / 0.60. It stays opt-in. Before the tuning, on a 25-case sample, the hybrid had scored 0.77 balanced @10 against 0.75 keyword-only at six times the memory (`artifacts/measure-memory-hybrid-*.json`); the tuned keyword index now scores 0.79 on that sample.
- **Indexing neighbouring memories as context.** Two neighbours on each side raised enriched LoCoMo to 0.66 / 0.84 but lowered MemBench recall @3 from 0.92 to 0.82 and BEAM @3 from 0.50 to 0.30, because the extra text distorts BM25 length normalisation. A separate context index fused additively kept BEAM (0.42 / 0.62 in isolation, against 0.39 / 0.56) but still cost MemBench about six points @3. Not adopted.

### LongMemEval-S

LongMemEval-S (cleaned) stores one memory per turn, about 231,500 turns across 470 questions; the 30 abstention questions are skipped as in the official retrieval evaluation. The store's secret filter rejected 106 turns, which are counted and left out.

```bash
uv run --extra benchmark python benchmarks/measure_memory.py --lexical-only \
  --locomo /nonexistent --membench /nonexistent --memoryagentbench --beam /nonexistent \
  --longmemeval-s /tmp/unimem-longmemeval_s_cleaned.json \
  --output artifacts/measure-memory-longmemeval-s.json
```

Result ([artifact](../artifacts/measure-memory-longmemeval-s.json)), no model anywhere:

| Metric | Result |
|---|---:|
| Session recall @5 (five returned memories) | 0.960 |
| Session recall @10 (ten returned memories) | 0.974 |
| Session recall @5 (first five distinct sessions of twenty memories) | 0.970 |
| Recall latency p50 / p95 | 5 ms / 26 ms |
| Peak RSS | 42 MB |

By question type @5: knowledge-update 1.00, single-session-assistant 1.00, single-session-user 0.98, multi-session 0.98, temporal-reasoning 0.93, single-session-preference 0.80. The misses are relative-time questions ("two weeks ago") and preference questions whose evidence shares no vocabulary with the question. Enrichment was not run here: 231,500 turns at about 0.12 s each is roughly eight hours.

### Comparison with published retrieval results

Only retrieval-recall results that need no LLM are comparable to this benchmark. Figures for other systems are their own published numbers, not reproduced here.

| Benchmark and metric | unimem | Published |
|---|---:|---|
| LongMemEval-S session recall @5 | 0.960 (0.970 over distinct sessions) | MemPalace raw 0.966; MemPalace hybrid v4, held-out 450 questions, 0.984; SelRoute 0.920; BM25 0.862 |
| LoCoMo session recall @10, fractional | 0.899 keyword only, 0.912 enriched | MemPalace bge-large hybrid 0.924; MemPalace hybrid v5 0.889; Memori 0.820 |

Sources: [MemPalace BENCHMARKS.md](https://github.com/MemPalace/mempalace/blob/develop/benchmarks/BENCHMARKS.md), [MemPalace discussion 747](https://github.com/MemPalace/mempalace/discussions/747), [SelRoute](https://arxiv.org/abs/2604.02431).

Read the comparison with these differences in mind:

- The published systems return five or ten whole sessions. unimem returns five or ten single turns inside a 400-token budget, and a session counts when one of its turns is returned. Several turns from one session can occupy the returned slots, which is why the distinct-session figure is higher.
- The LoCoMo figure uses the published formula: the mean fraction of evidence sessions retrieved, with questions that have no evidence counted as 1.0 (`session_fractional_recall_at_10`).
- On LongMemEval-S unimem is ahead of MemPalace's raw mode only on the distinct-session reading, and behind its tuned hybrid on either reading. On LoCoMo it is ahead of hybrid v5 and Memori and behind the bge-large hybrid.
- Answer-accuracy leaderboards (Mem0, Zep, Letta, and the LLM-reranked MemPalace results) use an LLM to answer and to judge, and are not comparable.

### Write-time enrichment

`unimem enrich` (`--extra enrich`, Apple Silicon) rewrites each stored memory once with a local model and indexes the result next to the original text. Recall stays a lexical query with no model call.

```bash
uv run --extra enrich --extra benchmark python benchmarks/measure_memory.py --lexical-only \
  --enrich-model mlx-community/LFM2.5-1.2B-Instruct-4bit \
  --membench /nonexistent --memoryagentbench --beam /nonexistent \
  --output artifacts/measure-memory-enriched-locomo.json
```

Full LoCoMo, 1,982 evidence questions ([artifact](../artifacts/measure-memory-enriched-locomo.json)):

| Mode | Recall @3 | Recall @10 | All evidence @10 | Session recall @10, fractional | Recall p95 |
|---|---:|---:|---:|---:|---:|
| Keyword only | 0.54 | 0.69 | 0.59 | 0.899 | 2.0 ms |
| Enriched | 0.62 | 0.79 | 0.69 | 0.912 | 2.4 ms |

Per category, enrichment moved recall @10 from 0.63 to 0.72 multi-hop, 0.75 to 0.81 temporal, 0.45 to 0.49 commonsense, 0.72 to 0.83 single-hop, and 0.69 to 0.81 adversarial. The 0.70 / 0.90 dialog-level targets are still unmet.

The enrichment column's bm25 weight was swept before the index tuning, on the earlier tokenizer: weights 1.0, 0.5, 0.33, and 0.25 gave recall @3 / @10 of 0.58 / 0.76, 0.60 / 0.78, 0.61 / 0.77, and 0.61 / 0.76, with ranking quality best at 0.25. On the tuned index 0.25 and 0.4 are within noise of each other and 0.15 and 0.6 are worse.

Enrichment cost about 0.12 s per memory in 16-way batches with 0.94 GB peak RSS for the benchmark process; it is paid once per memory, never at recall. MemBench, MemoryAgentBench, and BEAM have only been measured enriched on a 25-case sample ([artifact](../artifacts/measure-memory-enriched-sample.json)). On that sample enrichment scored 0.76 balanced @10 against 0.79 keyword-only ([artifact](../artifacts/measure-memory-lexical-sample.json)): BEAM fell from 0.84 to 0.72, three cases of 25. That is too small to settle, so whether enrichment helps outside LoCoMo is open.

Model choice was made with a one-conversation probe (LoCoMo conversation 26, 197 questions, 419 turns, sequential generation, earlier tokenizer). The probe scripts were not kept in the repository, so this table is a record rather than a reproducible result:

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

## Answer accuracy with an LLM (Agent Memory Benchmark)

The leaderboards that memory vendors publish score answers, not retrieval: a model answers each question from the memory system's context and a second model judges the answer. To compare on that axis under a neutral, published protocol, unimem runs inside the open [Agent Memory Benchmark](https://github.com/vectorize-io/agent-memory-benchmark) (AMB) harness, which also hosts results for Hindsight, cognee, and a hybrid-search baseline. AMB fixes the datasets, the answer prompts, and the judge prompts per dataset; LoCoMo is judged with the Mem0 CORRECT/WRONG prompt.

[`amb_unimem_provider.py`](amb_unimem_provider.py) is the unimem provider. It stores one memory per conversation turn and returns the top `UNIMEM_AMB_K` memories, oldest first, each prefixed with its session date. Recall stays a keyword query; the LLM only answers and judges.

```bash
git clone https://github.com/vectorize-io/agent-memory-benchmark amb && cd amb
cp ../unimem/benchmarks/amb_unimem_provider.py src/memory_bench/memory/unimem.py
# register it: add `from .unimem import UnimemMemoryProvider` and
# `"unimem": UnimemMemoryProvider` to src/memory_bench/memory/__init__.py
export UNIMEM_SRC=$PWD/../unimem/src UNIMEM_AMB_K=200 UNIMEM_AMB_FULL=40
export OPENAI_BASE_URL=http://127.0.0.1:10100/v1 OPENAI_API_KEY=local
export OMB_ANSWER_LLM=openai OMB_ANSWER_MODEL=google-antigravity/gemini-3.8-flash
export OMB_JUDGE_LLM=openai OMB_JUDGE_MODEL=google-antigravity/gemini-3.8-flash
export GEMINI_API_KEY=unused   # the AMB CLI requires the variable even when Gemini is not used
uv run --python 3.12 amb run --dataset longmemeval --split s --memory unimem --name unimem
```

The models here are reached through a local OpenAI-compatible proxy; any OpenAI-compatible endpoint works. That proxy ignores `response_format` for Gemini, so AMB's OpenAI adapter was patched locally to ask for the JSON object in the prompt, parse it tolerantly, and retry malformed replies. The patch changes how replies are read, not the prompts or the scoring.

**Recall budget.** The provider returns 200 memories per question: the 40 best-ranked (60 on LoCoMo) in full, up to 1,500 characters, and the rest as 500-character excerpts (`UNIMEM_AMB_FULL`, `UNIMEM_AMB_TAIL_CHARS`). On the pilot questions this beat both a flat 100 memories and a flat 200 shorter memories. Multi-session went from 10/17 to 13/17, LoCoMo multi-hop from 26/40 to 30/40, and single-hop from 31/40 to 36/40, with single-session-assistant unchanged. Choosing the budget on questions that are also in the final run is a small selection bias.

**Result, 2026-10-02** ([artifact](../artifacts/amb-gemini.json)). Gemini 3.8 Flash answers and judges. Both runs stopped early when the provider quota ran out; each question type ran its first questions in dataset order.

| Benchmark | Answered | Accuracy on answered | Weighted by question-type size | Mean context |
|---|---:|---:|---:|---:|
| LongMemEval-S | 298 of 500 | 93.6% | 91.5% | 21.5K tokens |
| LoCoMo (categories 1-4) | 724 of 1,540 | 85.1% | 88.7% | 10.0K tokens |

By type, LongMemEval: knowledge-update 56/56, single-session-user 62/63, single-session-assistant 51/54, temporal-reasoning 49/53, single-session-preference 27/30 (complete), multi-session 34/42. LoCoMo: open-domain 188/200, temporal 223/256, single-hop 141/172, multi-hop 64/96 (complete).

The weighted figure applies each question type's accuracy to its share of the full benchmark. It corrects for multi-session, the weakest type, being under-sampled, but it is still an estimate from partial runs.

Published AMB results, all answered and judged with Gemini, from the AMB results manifest:

| System | LongMemEval-S | LoCoMo | Context, LME / LoCoMo |
|---|---:|---:|---:|
| Hindsight | 94.6% | 92.0% | 43.6K / 36.2K tokens |
| cognee | | 80.3% (152 questions) | 14.7K |
| hybrid-search (Qdrant) | 74.0% | 79.1% | 23.2K / 22.2K |
| **unimem, this run** | **91.5% (est.)** | **88.7% (est.)** | **21.5K / 10.0K** |

unimem is ahead of cognee and hybrid-search, and about 3 points behind Hindsight on both, using half of Hindsight's context on LongMemEval and under a third on LoCoMo. The answer model differs from AMB's published runs (Gemini 3.8 Flash against the Gemini version AMB used), and these are partial runs, so the gaps are indicative. Vendor self-reports outside AMB (Mem0 94.4% / 92.5%, Mastra 94.87%, OMEGA 95.4% on LongMemEval) use other answer models, judges, and protocols and are not comparable.

An earlier partial run with MiMo v2.6 Pro answering and Step-5 judging, at 100 memories per question, scored 89.3% on 487 LongMemEval questions and 84.4% on 1,122 LoCoMo questions. It ended when the MiMo quota ran out.


## Source audit

Re-check every URL cited by the research report with:

```bash
uv run python benchmarks/check_sources.py
```

The result is written to [`artifacts/source-check.json`](../artifacts/source-check.json).
