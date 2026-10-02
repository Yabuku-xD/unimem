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

For a normal installation:

```bash
python3 -m pip install .
unimem doctor
```

From this checkout without installing:

```bash
PYTHONPATH=src python3 -m unimem doctor
```

The database is `~/.unimem/unimem.db` by default. Override it with `UNIMEM_HOME` or `UNIMEM_DB`.

## Initialize a project

Run this from the repository you want to configure:

```bash
unimem init --client all
```

This creates:

- `AGENTS.md`, `.agents/skills/unimem/SKILL.md`, and client-specific skill files;
- project MCP configuration for Claude Code, Cursor, and Codex;
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
python3 tests/e2e_unimem.py
```

It writes [`artifacts/e2e-unimem.json`](artifacts/e2e-unimem.json) and covers routine routing, trigger gating, scope isolation, session expiry, extraction filtering, output budgets, MCP parity, and local operation.

On the development machine used for this build, `doctor`, `route`, and a bounded `recall` each stayed at roughly 29-30 MB maximum resident memory and completed in about 0.1 seconds. Exact figures vary by Python runtime and database size.

## Limitations

The extractor is intentionally high-precision and rule-based: it captures explicit durable claims but will miss implicit preferences that need a model-based editor. The default retrieval index is lexical FTS5/BM25; semantic embeddings can be added later without changing the scope or lifecycle contract.

The no-routine-call requirement is enforced by the router, the tool contract, and generated client guidance. A specific coding host can still decide to call a tool incorrectly, so acceptance for a new host should include a trace showing zero `unimem` calls on routine turns and one bounded call on a missing-context turn.
