# Changelog

## 0.2.0 — 2026-10-02

### Added

- `unimem enrich`: optional write-time enrichment with a local MLX model (default LFM2.5 1.2B Instruct, Apple Silicon). Each memory is rewritten once into standalone facts, questions, and keywords that are indexed next to the original text; recall never calls a model.
- Optional local semantic recall (`--extra semantic`, FastEmbed) fused with keyword recall by weighted reciprocal rank.
- `unimem --version`.
- Benchmarks: LongMemEval-S session recall, LoCoMo session-level recall, a multi-corpus runner (LoCoMo, MemBench, MemoryAgentBench, BEAM), and a provider for the open Agent Memory Benchmark harness. Results and caveats are in `benchmarks/README.md`.
- CI on Linux and macOS, `LICENSE`, `SECURITY.md`, and `CONTRIBUTING.md`.

### Changed

- The keyword index uses Porter stemming, a larger stopword list, and prefix matching only for terms of four or more characters. LoCoMo evidence recall rose from 0.50 / 0.65 to 0.54 / 0.69 (@3 / @10) with no model.
- Recall narrows to the caller's scopes inside the full-text index, cutting recall p95 on the largest benchmark corpus from 114 ms to 10 ms.
- The library recall ceiling is 200 memories for callers that manage their own context budget. The CLI and MCP tool still return at most 20 and bound output tokens.

### Upgrading

Existing databases migrate when first opened: new columns are added, scope keys are backfilled, and the full-text index is rebuilt. No data is removed.

## 0.1.0 — 2026-10-01

- Initial release: local SQLite store with user, project, and session scopes; lazy, trigger-gated recall; conservative transcript distillation; one MCP tool; setup for Codex, Claude Code, Cursor, and `AGENTS.md` clients; uv-based install.
