# Changelog

## 0.2.0 (2026-10-02)

### Added

- `unimem enrich`: optional write-time enrichment with a local MLX model (default LFM2.5 1.2B Instruct, Apple Silicon). Each memory is rewritten once into standalone facts, questions, and keywords that are indexed next to the original text; recall never calls a model.
- Optional local semantic recall (`--extra semantic`, FastEmbed) fused with keyword recall by weighted reciprocal rank.
- `unimem --version`.
- `unimem init` sets up every supported tool once for your user account: Claude Code, the Claude Desktop app, Codex (CLI, IDE extension, and the ChatGPT / Codex desktop app), Cursor, Pi, Hermes Agent, and other Agent Skills clients. `--client` picks one tool, and `--project` writes repository config to share with a team. Existing config files are merged, never overwritten, and unreadable ones are left alone.
- Automatic sessions and capture: `unimem init` installs session-start and session-end hooks for Claude Code, Codex, Cursor, Pi, and Hermes Agent. A tool session opens a unimem session; when it ends, durable facts the user stated are distilled from the transcript and the session is closed. Hooks print nothing into the prompt and can be skipped with `--no-hooks`.
- The extractor ignores durable cues that appear only inside quotes or backticks.
- Implicit preferences: corrections and repeated instructions without an explicit cue ("no, use pnpm") are kept as candidates that recall never returns, and are promoted to a memory once a matching statement appears in a second session. `unimem candidates` lists them.
- Concurrent sessions in one project stay separate: a session hook records its process ancestry, and later calls from the same tool process use that session.
- One user-wide server still scopes project memories: it follows Claude Code's `CLAUDE_PROJECT_DIR`, the directory it starts in, or a `project_dir` argument on the MCP tool.
- `install.sh` installs uv when needed, unimem, and optionally the local model in one step; `unimem enrich --download` fetches the model ahead of time.
- Benchmarks: LongMemEval-S session recall, LoCoMo session-level recall, a multi-corpus runner (LoCoMo, MemBench, MemoryAgentBench, BEAM), and a provider for the open Agent Memory Benchmark harness. Results and caveats are in `benchmarks/README.md`.
- CI on Linux and macOS, `LICENSE`, `SECURITY.md`, and `CONTRIBUTING.md`.

### Changed

- The keyword index uses Porter stemming, a larger stopword list, and prefix matching only for terms of four or more characters. LoCoMo evidence recall rose from 0.50 / 0.65 to 0.54 / 0.69 (@3 / @10) with no model.
- Recall narrows to the caller's scopes inside the full-text index, cutting recall p95 on the largest benchmark corpus from 114 ms to 10 ms.
- The library recall ceiling is 200 memories for callers that manage their own context budget. The CLI and MCP tool still return at most 20 and bound output tokens.

### Upgrading

Existing databases migrate when first opened: new columns are added, scope keys are backfilled, and the full-text index is rebuilt. No data is removed.

## 0.1.0 (2026-10-01)

- Initial release: local SQLite store with user, project, and session scopes; lazy, trigger-gated recall; conservative transcript distillation; one MCP tool; setup for Codex, Claude Code, Cursor, and `AGENTS.md` clients; uv-based install.
