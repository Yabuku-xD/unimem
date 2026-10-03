# unimem

`unimem` is a local-first memory layer for coding agents. It keeps durable user, project, and session facts in one SQLite store and exposes one lazy memory tool to MCP clients. The normal path does not copy conversation logs into prompts, does not query memory for routine code work, and does not require an API key, vector database, container, or daemon.

## Guarantees

- **Lazy read path:** `recall` requires a missing-context trigger and concrete evidence. The deterministic `route` command returns `should_recall: false` for routine tasks without opening the database.
- **Bounded context:** recall returns at most three compact claims and reports an estimated token count. Raw transcripts and tool output are never returned as memory.
- **Strict scopes:** `user`, `project`, and `session` are separate. Project records are keyed by Git identity; session records are filtered by active session and expiry on every read.
- **Picky writes:** `distill` accepts explicit durable cues and rejects greetings, secrets, code blocks, tool output, and claims without a durable cue. Session hypotheses are kept only in the session lane.
- **Local operation:** standard-library Python, SQLite FTS5/BM25, and no external model or service.
- **Cross-client surface:** one stdio MCP tool plus generated skill guidance for Codex, Claude Code, Cursor, and generic `AGENTS.md` clients.

## Install

For a normal project environment:

```bash
uv sync
uv run unimem doctor
```

To install the CLI as a standalone uv tool from a checkout, or straight from GitHub:

```bash
uv tool install .
uv tool install git+https://github.com/Yabuku-xD/unimem
unimem --version
unimem doctor
```

The database is `~/.unimem/unimem.db` by default. Override it with `UNIMEM_HOME` or `UNIMEM_DB`.

## Initialize a project

Configure every supported client in one repository:

```bash
unimem init --client all
```

Or configure only the surface you use:

```bash
unimem init --client claude     # Claude Code
unimem init --client cursor     # Cursor
unimem init --client codex      # Codex
unimem init --client terminal   # terminal agents using AGENTS.md
```

`claude-code` is an alias for `claude`, and `agents` is the underlying name for `terminal`. To configure a different repository from anywhere:

```bash
unimem --project-dir /path/to/repo init --client cursor
```

Each command writes only the selected client surface plus the shared `AGENTS.md` pointer and skill guidance. All clients still use the same local database at `~/.unimem/unimem.db`.

This creates:

- `AGENTS.md` and the selected client's skill file;
- project MCP configuration only for the selected client;
- no memory content in generated prompt files.

The generated guidance tells agents to inspect the repository first and use memory only for prior-work references, missing constraints, handoffs, or conflicts.

## Store and recall

```bash
unimem remember "Always use pnpm for package management." \
  --scope user --lifecycle semantic --kind preference \
  --source user-statement --evidence "Explicit user instruction"

unimem remember "SQLite is the project store because it needs no service." \
  --scope project --lifecycle semantic --kind decision \
  --source architecture-decision --evidence "Recorded project decision"

unimem recall "package management" \
  --trigger explicit_reference \
  --evidence "User referenced a prior preference"
```

The trigger must be one of `explicit_reference`, `missing_context`, `cross_session`, `conflict`, or `memory_query`. Empty evidence is rejected.

## Sessions

```bash
unimem session start --title "debugging" --ttl-seconds 3600
UNIMEM_SESSION_ID=ses_... unimem remember \
  "Temporary hypothesis: the retry bug may be caused by cache." \
  --scope session --lifecycle episodic --kind hypothesis \
  --source session-note --evidence "Short-lived debugging hypothesis"
unimem session end ses_...
```

Session memories are unavailable after their expiry or when their session is closed. Expiry is checked in the read query, so cleanup timing cannot resurrect old hypotheses.

## Distill a transcript

`distill` accepts JSONL messages or a plain-text transcript and applies a conservative deterministic extractor:

```bash
unimem distill transcript.jsonl --apply --session-id ses_...
```

Accepted claims are limited to explicit durable cues. The command reports accepted and rejected candidates, including rejection reasons. It does not persist the input transcript.

## Enrich the index (optional, Apple Silicon)

`enrich` runs a small local model once over memories that have not been enriched yet. For each memory it writes standalone facts with names and absolute dates, the questions the memory answers, and related keywords, and adds that text to the keyword index. Recall still returns only the original memory, so output size and recall latency do not change.

```bash
uv sync --extra enrich
uv run unimem enrich            # LFM2.5 1.2B Instruct, 4-bit MLX
uv run unimem enrich --limit 200
```

The model loads only for the duration of the command and exits with it, so there is no daemon and no cost on routine turns. Relative dates ("yesterday", "last year") are resolved in code from each memory's date before the model sees them. Run it after `distill --apply`, from a scheduled job, or whenever convenient; unenriched memories remain searchable by their original text. Pass `--model` to try another MLX model; memories enriched by a different model are re-enriched. On LoCoMo this raised evidence recall @10 from 0.69 to 0.79; see [`benchmarks/README.md`](benchmarks/README.md).

## MCP clients

`unimem init` writes the local MCP command into the supported client configuration. The server exposes one tool, `unimem`, with three actions:

- `recall`: bounded, trigger-gated retrieval;
- `remember`: durable write;
- `status`: local counts and runtime contract.

The tool description and schema are intentionally small. Clients that support deferred tool discovery can defer the tool until the missing-context branch fires.

## Audit and diagnostics

```bash
unimem audit --json
unimem doctor --json
```

Every successful or blocked retrieval is audited. `doctor` reports the local database, FTS5 availability, external API count, daemon requirement, resident instruction bytes, and MCP schema bytes.

## Verification

The reproducible end-to-end acceptance run is:

```bash
uv run python tests/e2e_unimem.py
```

It writes [`artifacts/e2e-unimem.json`](artifacts/e2e-unimem.json) and covers routine routing, trigger gating, scope isolation, session expiry, extraction filtering, output budgets, MCP parity, and local operation.

For memory-quality metrics and public-corpus retrieval tests:

```bash
uv run python benchmarks/run_memory_quality.py
uv run python benchmarks/check_sources.py
```

The benchmark methodology, real LoCoMo command, source mapping, and current limitations are documented in [`benchmarks/README.md`](benchmarks/README.md).

On the development machine used for this build, `doctor`, `route`, and a bounded `recall` each stayed at roughly 29-30 MB maximum resident memory and completed in about 0.1 seconds. Exact figures vary by Python runtime and database size.

## Toolchain

uv is the primary development and installation workflow. It gives reproducible environments through `uv.lock`, fast command execution through `uv run`, and standalone CLI installation through `uv tool install`.

The tradeoffs are small but real: contributors need uv installed, `uv.lock` must be refreshed when Python constraints change, and `uv tool install` may require adding its tool directory to `PATH`. The package still uses standard `pyproject.toml` metadata, so downstream packaging tools remain compatible with the project.

## Limitations

The extractor is intentionally high-precision and rule-based: it captures explicit durable claims but will miss implicit preferences that need a model-based editor. The default retrieval index is lexical FTS5/BM25 with Porter stemming; local embeddings (`--extra semantic`) and write-time enrichment are opt-in and do not change the scope or lifecycle contract.

The no-routine-call requirement is enforced by the router, the tool contract, and generated client guidance. A specific coding host can still decide to call a tool incorrectly, so acceptance for a new host should include a trace showing zero `unimem` calls on routine turns and one bounded call on a missing-context turn.

## Project

- [CHANGELOG.md](CHANGELOG.md) lists changes per release, including database migrations.
- [CONTRIBUTING.md](CONTRIBUTING.md) has the development setup and the checks CI runs.
- [SECURITY.md](SECURITY.md) explains how to report a vulnerability and what unimem stores.
- Licensed under the [MIT License](LICENSE).
