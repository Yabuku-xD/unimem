<div align="center">

<h1>unimem</h1>

<p>One local memory for all your coding agents. It stays quiet until an agent actually needs to remember something.</p>

[![CI](https://github.com/Yabuku-xD/unimem/actions/workflows/ci.yml/badge.svg)](https://github.com/Yabuku-xD/unimem/actions/workflows/ci.yml)
![Version](https://img.shields.io/badge/version-0.2.0-blue?style=flat-square)
![Python](https://img.shields.io/badge/python-3.11%2B-blue?style=flat-square)
![Platforms](https://img.shields.io/badge/platforms-macOS%20%7C%20Linux-lightgrey?style=flat-square)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

</div>

unimem gives Claude Code, Codex, Cursor, and terminal agents one shared memory on your own computer. It remembers your preferences, your project's decisions, and short-lived notes from the current task, all in one SQLite file. Agents only look things up when they are missing context, so ordinary coding turns cost nothing extra. No account, API key, or background service is involved.

## Install

unimem runs on macOS and Linux.

Open Terminal and paste this line:

```bash
curl -LsSf https://raw.githubusercontent.com/Yabuku-xD/unimem/main/install.sh | sh
```

The installer sets up [uv](https://docs.astral.sh/uv/) if you don't have it, installs unimem as its own isolated app, and checks that it runs. It doesn't touch your system Python. When it finishes, open a new Terminal window so the `unimem` command is found.

If you downloaded the project as a ZIP instead, unzip it, open Terminal in that folder, and run:

```bash
sh install.sh
```

### The optional local model

On a Mac with Apple Silicon (M1 or newer), the installer asks whether you also want the local model. It's [LFM2.5 1.2B Instruct](https://huggingface.co/mlx-community/LFM2.5-1.2B-Instruct-4bit), a 660 MB download that runs entirely on your Mac. unimem uses it to rewrite each saved memory into plain facts and likely questions, which helps it find memories when you phrase things differently. On the LoCoMo benchmark it raised recall from 69% to 79%.

You don't need the model; unimem works fully without it. To answer the question in advance:

```bash
sh install.sh --with-model   # install unimem and download the model now
sh install.sh --no-model     # install unimem only
```

You can add the model later by running the installer again with `--with-model`. The model never runs during normal recall. It only runs when you type `unimem enrich`, then it exits and frees the memory.

### Check that it worked

```bash
unimem --version
unimem doctor
```

`doctor` prints where your memory file lives and whether the local model runtime is installed.

## Connect your coding tools

Connect everything at once, from any folder:

```bash
unimem init
```

That's a one-time setup for your user account. Every session in every project, and in your home folder, then shares the same memory file, `~/.unimem/unimem.db`. Something saved in Codex is there in Claude, Cursor, and the rest.

To connect only some tools, run the command for each one you use:

| Tool | Command |
|---|---|
| Claude Code | `unimem init --client claude` |
| Claude Desktop app | `unimem init --client claude-desktop` |
| Codex (CLI, IDE extension, and the ChatGPT / Codex desktop app) | `unimem init --client codex` |
| Cursor | `unimem init --client cursor` |
| Pi | `unimem init --client pi` |
| Hermes Agent | `unimem init --client hermes` |
| Other agents that load skills from `~/.agents/skills` | `unimem init --client agents` |

Restart the tool afterwards so it loads unimem. In Hermes you can run `/reload-mcp` instead.

Here is what each command writes, all inside your home folder:

- Claude Code adds unimem to `~/.claude.json`, a skill to `~/.claude/skills/`, and hooks to `~/.claude/settings.json`.
- Claude Desktop adds unimem to `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS.
- Codex adds unimem to `~/.codex/config.toml`, which the CLI, the IDE extension, and the desktop app share, and a skill to `~/.agents/skills/`.
- Cursor adds unimem to `~/.cursor/mcp.json` and a skill to `~/.agents/skills/`.
- Pi adds unimem to `~/.pi/agent/mcp.json` and a skill to `~/.agents/skills/`.
- Hermes adds unimem under `mcp_servers` in `~/.hermes/config.yaml` and a skill to `~/.hermes/skills/`.

None of these files ever receive your memories. unimem adds only its own entry and leaves the rest of each file as it was. If a config file can't be read, it stops with an error rather than editing it.

Project memories still stay with their project. Claude Code tells unimem which project is open, the other tools start it inside the project, and the skill asks the agent to pass the workspace path when it can.

### Automatic sessions and capture

`unimem init` also adds two small hooks to each tool that supports them. You don't have to do anything during a session.

- When a session starts, unimem opens a matching session of its own. Short-lived notes the agent saves go there.
- When the session ends, unimem reads that session's transcript on your computer and keeps only the durable things you stated, such as "from now on always use pnpm" or "we decided to use Postgres for this project". Then it closes the session, and its short-lived notes stop coming back.

The hooks never add anything to the prompt, and the transcript itself is not stored. Assistant replies, tool output, code, secrets, and text you only quoted are ignored.

| Tool | Where the hooks go | Good to know |
|---|---|---|
| Claude Code | `~/.claude/settings.json` | works straight away |
| Codex | `~/.codex/hooks.json` | run `/hooks` in Codex once and trust the two new hooks |
| Cursor | `~/.cursor/hooks.json` | |
| Pi | `~/.pi/agent/extensions/unimem.ts` | |
| Hermes Agent | `hooks:` in `~/.hermes/config.yaml` | Hermes asks for consent the first time each hook runs |
| Claude Desktop app | none | the app has no hooks, so the agent saves memories itself |

To skip the hooks, run `unimem init --no-hooks`. Each hook run is logged to `~/.unimem/hooks.log`.

### Sharing the setup with a team

To commit the configuration to a repository so everyone who clones it gets unimem, run this inside the project instead:

```bash
unimem init --client claude --project
```

`--project` works for `claude`, `codex`, `cursor`, `agents`, and `all`. It writes `.mcp.json`, `.codex/config.toml`, `.cursor/mcp.json`, project skills, and a short pointer in `AGENTS.md`. Hooks are only set up per user, not per project. Codex reads a project's `.codex/config.toml` only after you trust the project.

## Use it

Most of the time your agent calls unimem for you. You can also use it directly.

Save a preference that applies everywhere, and a decision for the current project:

```bash
unimem remember "Always use pnpm for package management." \
  --scope user --kind preference --evidence "Explicit user instruction"

unimem remember "SQLite is the project store because it needs no service." \
  --scope project --kind decision --evidence "Recorded project decision"
```

Look something up. Recall asks for a reason, so agents can't query memory out of habit:

```bash
unimem recall "package manager" \
  --trigger explicit_reference --evidence "User referenced a prior preference"
```

The trigger is one of `explicit_reference`, `missing_context`, `cross_session`, `conflict`, or `memory_query`.

Keep a short-lived note for one task. It disappears when the session ends or expires:

```bash
unimem session start --title "debugging" --ttl-seconds 3600
UNIMEM_SESSION_ID=ses_... unimem remember "The retry bug may come from the cache." \
  --scope session --kind hypothesis --evidence "Debugging hypothesis"
unimem session end ses_...
```

Pull durable facts out of a chat transcript. Greetings, secrets, code output, and guesses are rejected:

```bash
unimem distill transcript.jsonl --apply
```

With the local model installed, improve how well saved memories can be found:

```bash
unimem enrich
```

`unimem forget <id>` hides a memory from recall, and `unimem audit` lists every lookup with its reason.

## How it behaves

- Memories live in one of three places. User memories follow you across projects. Project memories belong to one Git repository. Session memories expire with the task.
- Routine work triggers no lookups. The `route` command decides whether a task needs memory without opening the database.
- A lookup returns at most three short memories, about 400 tokens. Raw transcripts are never returned.
- Writes that look like API keys, tokens, or passwords are refused.
- Everything runs locally on Python's standard library and SQLite. The optional model and embeddings download once, then run offline.

## How well it remembers

unimem was measured on the public benchmarks memory products report. The full numbers, methods, and caveats are in [benchmarks/README.md](benchmarks/README.md).

| Benchmark | unimem | For comparison |
|---|---|---|
| LongMemEval-S, finding the right conversation in the top 5, no LLM | 96.0% | MemPalace 96.6%, BM25 86.2% |
| LoCoMo, finding the right conversation in the top 10, no LLM | 89.9% (91.2% with the local model) | MemPalace 88.9% to 92.4% |
| LongMemEval-S, answers judged correct, with Gemini answering | 91.5% (estimate) | Hindsight 94.6%, hybrid search 74.0% |
| LoCoMo, answers judged correct, with Gemini answering | 88.7% (estimate) | Hindsight 92.0%, cognee 80.3% |

The answer-accuracy rows come from the open [Agent Memory Benchmark](https://github.com/vectorize-io/agent-memory-benchmark) and from partial runs, which stopped when the model quota ran out. unimem used about half of Hindsight's context per question on LongMemEval and under a third on LoCoMo. A typical lookup takes a few milliseconds.

## Questions

**Does unimem send my data anywhere?**
No. It makes no network calls of its own. The memory file stays on your computer at `~/.unimem/unimem.db`. Set `UNIMEM_HOME` or `UNIMEM_DB` to keep it somewhere else.

**Do I need the local model?**
No. Without it, unimem uses keyword search with word stemming, which already scores well on the benchmarks above. The model helps most when you phrase a question differently from how the memory was written.

**How do I uninstall it?**

```bash
uv tool uninstall unimem
rm -rf ~/.unimem
rm -rf ~/.cache/huggingface/hub/models--mlx-community--LFM2.5-1.2B-Instruct-4bit
```

The second line deletes your memories; the third deletes the local model.

## Development

```bash
uv sync
uv run python tests/e2e_unimem.py
```

[CONTRIBUTING.md](CONTRIBUTING.md) lists every check CI runs. [CHANGELOG.md](CHANGELOG.md) has release notes and database migration notes. Report security problems as described in [SECURITY.md](SECURITY.md). The research behind the design is in [MEMORY_SYSTEM_RESEARCH.md](MEMORY_SYSTEM_RESEARCH.md).

## License

[MIT](LICENSE)
