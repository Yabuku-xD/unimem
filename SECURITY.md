# Security

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's [private vulnerability reporting](https://github.com/Yabuku-xD/unimem/security/advisories/new) instead of a public issue. Include the unimem version (`unimem --version`), the command or MCP call involved, and the smallest reproduction you can share. Do not attach real memory databases; they can contain personal or project information.

## What unimem stores and where

- All memory lives in one local SQLite file, `~/.unimem/unimem.db` by default (`UNIMEM_HOME` or `UNIMEM_DB` override it). Protect it like any other file in your home directory.
- unimem makes no network calls on its own. The optional `semantic` and `enrich` extras download model weights once from Hugging Face and then run locally.
- Writes are rejected when the content or evidence looks like a secret (API keys, tokens, private keys, passwords). The filter is pattern-based, so do not rely on it as the only control for sensitive data.
- `forget` marks a memory deleted so recall never returns it. The row stays in the database file; delete the database to remove data permanently.
- Project memories are keyed by Git identity and session memories expire. Recall filters by scope on every read, so one project's memories are not returned in another project.

## Supported versions

Security fixes are made on the latest release on `main`.
