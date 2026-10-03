# Contributing

## Setup

unimem uses [uv](https://docs.astral.sh/uv/) and Python 3.11 or newer. The core package has no runtime dependencies.

```bash
uv sync
```

## Checks

Run these before opening a pull request; CI runs the same commands on Linux and macOS.

```bash
uvx ruff check src tests benchmarks
uv run python tests/e2e_unimem.py
uv run python benchmarks/run_memory_quality.py
uv build
```

`tests/e2e_unimem.py` drives the real CLI and MCP server in temporary homes and writes its evidence to `artifacts/e2e-unimem.json`. Prefer extending it over adding isolated unit tests.

## Retrieval changes

Changes to ranking, tokenization, or recall budgets must be measured on the public corpora before and after, with the commands in [benchmarks/README.md](benchmarks/README.md). Keep a change only if it improves the target metric without lowering another corpus, and record the numbers and any rejected alternatives there.

## Schema changes

The database migrates in place when it is opened (`Database.initialize`). A schema change must upgrade an existing database created by the previous release without data loss; check this by creating a database with the previous release and opening it with your branch.

## Commits

Use conventional commit subjects (`feat:`, `fix:`, `perf:`, `docs:`, `test:`, `chore:`), one logical change per commit.
