# Memory System Research

Research date: 2026-10-01
Source re-check: 2026-10-02

Scope: current coding-agent memory products, shared local memory systems, and primary research on memory retrieval, extraction, context overhead, and evaluation.

## Executive finding

No turnkey product I found satisfies all of the hard requirements at the same time:

1. No conversational-log injection on routine turns.
2. No memory retrieval call or latency on routine turns.
3. Cross-tool use across Claude Code, Cursor, terminal agents, and other clients.
4. User/project/session scoping with selective extraction.
5. No paid memory service or model API, minimal maintenance, and low local resource use.

The conflict is architectural. A system can preload memory into every prompt, expose memory as a tool and let the model decide when to call it, or hide the decision in the host. The first two approaches each violate one of the stated overhead requirements. The third approach, a host-level router that detects a missing-context condition and invokes memory only then, is what the requirements really describe. It is not provided as a complete cross-client product today.

The closest practical foundation is `memories.sh`: it has a local SQLite store, local embeddings, FTS5/BM25 search, global and Git-project scopes, explicit session lifecycle, and generated configs for many coding tools. Its current defaults are not compliant: its generated harness tells agents to call `get_context` at task start, its `get_context` tool says to use it at the start of tasks, and its generated instruction files include all non-path-scoped rule/decision/fact records. Those defaults must be changed to a trigger-based read policy and a small resident rule set.

Memorix is the strongest alternative when cross-agent support, especially Hermes Agent, and project-safe curation matter more than simplicity. It explicitly supports Claude Code, Codex, Cursor, Windsurf, Gemini CLI, OpenCode, Hermes Agent, and others; it uses SQLite plus local text search and can work without model keys. It still recommends a project-context call at task start and exposes a substantial MCP tool surface, so it also needs a lazy-read policy and a small tool profile.

If the requirement is relaxed from "zero retrieval calls" to "retrieve only when needed," `memories.sh`, Memorix, or Basic Memory become viable. If the requirement is relaxed from cross-tool portability to the best automatic extraction, Codex local memories and Claude Code auto memory are substantially more turnkey. If paid infrastructure and operational work are acceptable, Mem0 and Zep/Graphiti offer stronger extraction and temporal modeling, but their defaults use external LLM/embedding APIs and/or database services.

## 1. Requirements as testable contracts

The requirements should be treated as measurable contracts rather than product labels.

| Requirement | Test that would prove it | Failure to watch for |
|---|---|---|
| No conversation-log injection | On a routine local-code turn, the prompt contains zero raw transcript or session-summary chunks. | A session-start briefing, `MEMORY.md` dump, or generated history file is always present. |
| No routine retrieval call | A standard edit/test turn records zero memory-tool calls and zero memory latency. | "Call memory at task start" guidance or a hidden startup query. |
| Efficient tokens | Memory contributes zero tokens on routine turns and a hard, small budget on triggered turns. | All rules and decisions are serialized into every prompt; top-k results contain raw transcripts. |
| Cross-tool truth | The same fact is retrievable from Codex, Claude Code, Cursor, and a terminal client without copying it manually. | Each agent owns a private store or requires a separate migration. |
| Strict tiers | A fact is either user, project, or session scope, with no accidental cross-project leakage. | `global` search returns unrelated repositories or session hypotheses become permanent. |
| Session expiry | Short-lived hypotheses disappear or become unavailable after a defined TTL or task boundary. | Compaction leaves old hypotheses queryable indefinitely. |
| Picky extraction | Generated memories contain durable, useful claims and reject pleasantries, secrets, transient output, and unverified speculation. | Every turn becomes a memory; noisy chat is stored as semantic truth. |
| Zero external API cost | The memory store and default retrieval run locally with no paid embedding/vector/LLM service. | Defaults call OpenAI/another provider, or a hosted vector database is required. |
| Minimal maintenance | No always-on fleet, database cluster, or fragile background daemon is required for normal use. | Docker Compose, Postgres/Neo4j, or a manually supervised service is part of the normal path. |
| Low resources | Measured idle and active CPU/RSS stay small on the target laptop. | A large model or graph service is loaded for every lookup. |

The first two tests are stricter than most product documentation. "No prompt bloat" must be interpreted carefully: no raw conversational logs is achievable, while byte-for-byte zero fixed overhead requires lazy tool discovery or host-side routing.

## 2. What native coding agents provide

### Codex local memories

Codex has a local memory feature that turns eligible prior chats into files under `~/.codex/memories/`, including summaries, durable entries, recent inputs, and supporting evidence. It skips active or short-lived sessions, redacts secrets, and runs extraction in the background after idle time rather than at the end of every chat. The feature is off by default and can be controlled per chat. `memories.use_memories` controls whether existing memories are injected into future sessions; `memories.generate_memories` controls whether chats become generation inputs. The official guidance explicitly says to keep required team guidance in `AGENTS.md`, because memories are a recall layer rather than the only source for mandatory rules. [Codex Memories](https://developers.openai.com/codex/customization/memories) [Codex configuration reference](https://developers.openai.com/codex/config-file/config-reference)

This is the best turnkey match for picky, background extraction, but it is Codex-local rather than a shared cross-tool memory layer. The injection setting also means the normal read path is prompt injection, not zero-overhead lazy retrieval.

### Claude Code memory

Claude Code has two layers:

- `CLAUDE.md` and `AGENTS.md` instructions, written by humans or imported from other tools.
- Auto memory, written by Claude, with `user`, `feedback`, `project`, and `reference` records.

Auto memory lives per repository under `~/.claude/projects/<project>/memory/`. The first 200 lines or 25 KB of `MEMORY.md` load at the start of every conversation; topic files are not loaded at startup and are read on demand. Claude skips facts it can derive from the codebase and facts already stated in `CLAUDE.md`. The documentation recommends keeping instruction files short and using path-scoped rules or skills for content that is not needed in every session. [Claude Code memory](https://code.claude.com/docs/en/memory)

This provides unusually good curation and on-demand topic files, but the memory index is still resident at session start and the store is Claude-specific. The `user` records are useful preferences, but the automatic store is repository-scoped; a truly global user tier must be represented separately, such as `~/.claude/CLAUDE.md`.

### Cursor rules and Memories

Cursor's general mechanism is rules, not a unified memory database. Project rules, user rules, team rules, and `AGENTS.md` provide persistent prompt-level context. Cursor says applied rule contents are included at the start of the model context. Project rules can be scoped by file globs, manually invoked, or selected by relevance; `AGENTS.md` supports nested files. [Cursor Rules](https://cursor.com/docs/rules)

Cursor's named Memories feature in the current docs is scoped to Cloud Agent Automations, where each automation stores `MEMORIES.md` outside the working filesystem and can use it across runs. It is not the same as a shared local memory store for ordinary Agent sessions. [Cursor Automations](https://cursor.com/docs/cloud-agent/automations)

Cursor therefore supplies a useful project/user instruction layer, but not the required session lifecycle or unified cross-tool memory.

### AGENTS.md as a portable policy layer

`AGENTS.md` is an open Markdown format supported by many coding agents, including Codex, Cursor, Aider, Gemini CLI, Goose, OpenCode, Zed, VS Code, Copilot, and others. It is appropriate for tiny durable rules and repository commands, not for conversation logs. [AGENTS.md](https://agents.md/) [AGENTS.md repository](https://github.com/agentsmd/agents.md)

The format has no built-in memory extraction, expiry, retrieval policy, or scope model. It is best used as the small always-on policy surface above a lazy memory store.

## 3. Cross-client memory candidates

| System | Cross-tool | Local / no paid memory service | Scope and lifecycle | Routine hot path | Extraction | Main blocker |
|---|---|---|---|---|---|---|
| Codex local memories | No | Local files; host model quota for extraction | Machine-local `CODEX_HOME` store; project rules separate | Existing memories are injected when enabled | Strong, background and secret-aware | Codex-only and prompt-injected |
| Claude Code auto memory | No | Local files; host model quota | Per-repository automatic memory; manual global instructions | `MEMORY.md` index loads every session | Strong, skips derivable and duplicate facts | Claude-only and resident index |
| Cursor rules / Memories | Partial | Local rules; Memories documented for cloud automations | User/project/team rules; automation memory | Applied rules enter model context | Mostly manual | No unified local memory lifecycle |
| `memories.sh` | Strong | Strong: SQLite, local FTS, local embeddings | Global/project/session plus lifecycle lanes | Default task-start recall and generated rules | Manual writes plus consolidation | Default read policy and resident growth |
| Memorix | Strong, including Hermes Agent | Strong without model keys; optional local/API models | Git project, session, curated long-term, portable user items | Recommended first context call and tool schemas | Curated pipeline with noise filtering | Broad surface and young platform |
| Basic Memory | Strong | Strong local Markdown/index/MCP | Arbitrary projects; no native three-tier TTL | Explicit MCP search/read/write calls | Agent-authored notes | No automatic picky extraction |
| Mem0 / OpenMemory | Strong via MCP | Poor default: OpenAI plus Docker/Postgres; Ollama possible | User/agent/session metadata | Search-tool calls | Strong LLM extraction and consolidation | External cost or local model operations |
| Zep / Graphiti | Strong via MCP | Poor default: OpenAI plus Neo4j/FalkorDB | Multi-tenant temporal graph | Search-tool calls | Strong temporal graph pipeline | Database and model-service operations |
| Letta / MemGPT | Product-specific | Partial: server and Postgres/pgvector | Shared blocks and archival memory | Blocks are prepended to prompts | Agent-managed memory | Prompt residency and runtime coupling |

### `memories.sh`

`memories.sh` is a local-first CLI and MCP server with one SQLite database at `~/.config/memories/local.db`. It supports:

- global and Git-project scope;
- session lifecycle (`start`, `checkpoint`, `snapshot`, `end`) and compaction;
- semantic, episodic, and procedural lifecycle lanes;
- FTS5/BM25 search plus local embeddings;
- generated native configs for Claude Code, Cursor, Codex, Copilot, Windsurf, Gemini, Cline, Roo, and `.agents/`;
- optional cloud sync, which is not required for local operation.

The product docs say the semantic model is approximately 100 MB, downloads once, and runs locally with no API calls. The source uses a local Transformers.js-compatible embedding model and SQLite FTS5. [memories.sh documentation](https://memories.sh/docs) [Memory segmentation](https://memories.sh/docs/concepts/memory-segmentation) [Getting started](https://memories.sh/docs/getting-started) [CLI reference](https://memories.sh/docs/cli) [source: embeddings models](https://github.com/webrenew/memories/blob/main/packages/cli/src/lib/embeddings-models.ts)

Important implementation evidence:

- `agents-generator.ts` creates `.agents/instructions.md` from all non-path-scoped `rule`, `decision`, and `fact` memories, with a fetch limit of 10,000. [source](https://github.com/webrenew/memories/blob/main/packages/cli/src/lib/agents-generator.ts)
- The generated harness says "Before coding, recall current context" and "Start tasks with a context recall." [same source](https://github.com/webrenew/memories/blob/main/packages/cli/src/lib/agents-generator.ts)
- The MCP `get_context` description says to use it at the start of tasks, and the implementation always returns all active rules. [source: MCP tools](https://github.com/webrenew/memories/blob/main/packages/cli/src/mcp/tools.ts) [source: context implementation](https://github.com/webrenew/memories/blob/main/packages/cli/src/lib/memory.ts)
- The MCP server instructions also say to call `get_context` at the start of a task. [source: MCP server](https://github.com/webrenew/memories/blob/main/packages/cli/src/mcp/index.ts)

Assessment: strong storage and compatibility substrate, weak default hot-path policy. It is the best base to modify, not a compliant system out of the box.

### Memorix

Memorix is a local-first cross-agent memory layer explicitly targeting Claude Code, Codex, Cursor, Windsurf, Copilot, Gemini CLI, OpenCode, Hermes Agent, and other MCP clients. Its canonical store is SQLite; Orama provides search, with optional local or API embeddings. Without model keys it still works with local full-text retrieval. It has project identity derived from Git, observation and reasoning memory, Git-derived facts, curated long-term episodic/semantic/procedural records, and a compact task Workset. [Memorix README](https://github.com/AVIDS2/memorix/blob/main/README.md) [Architecture](https://github.com/AVIDS2/memorix/blob/main/docs/ARCHITECTURE.md) [Setup](https://github.com/AVIDS2/memorix/blob/main/docs/SETUP.md)

The architecture is thoughtful about scope and noise:

- `memorix_search` is project-scoped by default.
- Curated long-term records move through candidate, qualified, approved, and archived/superseded states.
- Candidates are never injected into agent context.
- Only explicitly user-owned portable items cross local projects; project, Git, session, code, and claim evidence do not.
- Retrieval is progressive: compact results first, timeline second, full detail only on request.

Those properties fit the requested tiering and picky extraction much better than a flat memory dump. The task Workset is bounded to a 180-token target in the 1.2 design and returns less context when nothing is relevant. [Workset retrieval](https://github.com/AVIDS2/memorix/blob/main/docs/1.2.0-WORKSET-RETRIEVAL.md)

The remaining mismatches are operational and hot-path related. `memorix setup` installs agent plugins, hooks, skills, and MCP configuration; Node.js 22.18+ and Git are required. HTTP mode can run a shared background service and dashboard, although stdio mode does not require one. The recommended normal entry point is `memorix_project_context`/`memorix context` at task start, and the default installed MCP profile teaches a 20+ tool surface. The config supports `[memory] inject = "silent"` and `minimal`, which can disable or reduce automatic hook delivery. [Configuration](https://github.com/AVIDS2/memorix/blob/main/docs/CONFIGURATION.md) [API reference](https://github.com/AVIDS2/memorix/blob/main/docs/API_REFERENCE.md)

Assessment: best project-safe, cross-agent candidate, especially for Hermes compatibility. It needs a strict custom read policy, a small tool profile, and acceptance testing because it is a young, broad platform rather than a minimal memory store.

### Basic Memory

Basic Memory is a file-first local knowledge engine. Markdown files are the source of truth; a local database indexes entities, observations, relations, and full text. The local MCP server exposes `search_notes`, `read_note`, `write_note`, `edit_note`, and `build_context`. Local mode supports Claude, Codex, Cursor, VS Code, and other MCP clients. It runs locally with no cloud account and offers hybrid keyword plus semantic search. [Technical information](https://docs.basicmemory.com/reference/technical-information) [Local install](https://docs.basicmemory.com/local/local-install) [Basic Memory README](https://github.com/basicmachines-co/basic-memory/blob/main/README.md)

The local installation requires Python 3.12+ and `uv`/Homebrew, and the current release uses FastMCP prerelease dependencies. The system is lightweight relative to graph/database stacks, but its normal interaction is explicit note search/read/write through MCP. It does not provide the requested native user/project/session TTL model or automatic preference extraction.

Assessment: excellent human-readable, private fallback when MCP calls are acceptable. It is less turnkey for picky automatic capture and lifecycle isolation.

### Mem0 and OpenMemory

Mem0 combines LLM extraction/consolidation with vector storage and optional graph memory. Its open-source library defaults to OpenAI for the LLM and embeddings; the self-hosted server defaults to OpenAI plus Postgres/pgvector and is launched as a Docker stack. Local providers such as Ollama can be configured, but then the operator owns the model runtime and its resource cost. [Mem0 overview](https://docs.mem0.ai/overview) [Mem0 open source](https://docs.mem0.ai/open-source/overview) [Mem0 repository](https://github.com/mem0ai/mem0)

OpenMemory is a related local MCP experience. The official Mem0 repository carries a sunset notice and directs local users to the self-hosted Mem0 server. Its documented local setup requires Docker and an OpenAI API key by default, with Ollama configuration possible. [OpenMemory repository](https://github.com/mem0ai/mem0/blob/3e6ab394/openmemory/README.md)

Assessment: strong extraction and consolidation, poor default fit for zero external API cost and minimal maintenance. A local-model deployment is possible, but it is no longer a zero-ops small-footprint system.

### Zep and Graphiti

Graphiti is a temporal knowledge-graph memory framework. The documented quick start requires Python, Neo4j or FalkorDB, and an OpenAI API key by default; the Graphiti MCP server can be run with Docker. It supports multi-tenant graphs, hybrid semantic/graph search, and temporal relationships. [Graphiti quick start](https://help.getzep.com/graphiti/getting-started/quick-start) [FalkorDB Graphiti docs](https://docs.falkordb.com/agentic-memory/graphiti) [Graphiti MCP server](https://docs.falkordb.com/agentic-memory/graphiti-mcp-server.html) [Zep paper](https://arxiv.org/abs/2501.13956)

Assessment: best fit when time-varying facts, entity relationships, and enterprise-grade memory quality dominate. It is a poor fit for the stated low-maintenance, low-resource, zero-external-cost constraints.

### Letta / MemGPT

Letta's memory blocks are structured sections of the agent context that are always visible and prepended to the prompt; no retrieval is needed for them. Blocks can be shared between agents and edited with memory tools. The self-hosted Docker path uses PostgreSQL with pgvector and model API configuration. [Letta memory blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks) [Letta Docker deployment](https://docs.letta.com/guides/docker)

MemGPT introduced virtual context management: the model pages information between a limited context window and external storage using function calls. This is a strong research architecture for context management, but it is agent-runtime-specific and makes memory operations part of the model's decision loop. [MemGPT paper](https://arxiv.org/abs/2310.08560)

Assessment: useful for a single long-lived agent or an application built around Letta, not a lightweight shared memory layer for existing coding CLIs.

## 4. Research evidence

### Memory taxonomy

CoALA separates working memory from long-term episodic, semantic, and procedural memory and places retrieval inside the agent's decision cycle. This supports the proposed two-axis model: scope (user/project/session) and lifecycle/type (semantic/episodic/procedural). [CoALA](https://arxiv.org/abs/2309.02427)

LangGraph's memory documentation makes the same practical distinction: semantic facts, episodic experiences, and procedural rules, with short-term thread state separate from long-term stores. [LangGraph memory](https://docs.langchain.com/oss/python/concepts/memory) [LangMem conceptual guide](https://github.com/langchain-ai/langmem/blob/main/docs/docs/concepts/conceptual_guide.md)

### Selective extraction and filtering

Generative Agents scores memories by recency, importance, and relevance, and periodically creates higher-level reflections. That is a useful baseline for ranking and for turning repeated experience into durable knowledge, although its importance scores are model-generated and not a guarantee of correctness. [Generative Agents](https://arxiv.org/abs/2304.03442)

MemInsight autonomously mines attributes from interactions, annotates memories, and retrieves by those attributes. It reports improved recommendation persuasiveness and higher LoCoMo retrieval recall than a RAG baseline, supporting the idea that extraction should add structure and filter irrelevant memory rather than copy turns. [MemInsight](https://arxiv.org/abs/2503.21760) [ACL Anthology](https://aclanthology.org/2025.emnlp-main.1683/)

The native products provide the strongest current examples of conservative write policies. Codex skips active/short-lived sessions and redacts secrets; Claude Code skips code-derived facts and existing instructions. Those rules should be preserved in any custom extractor.

### Retrieval only when needed

Self-RAG explicitly rejects indiscriminate retrieval and learns to issue retrieval only when it is helpful. Its reflection tokens decide whether to retrieve, then critique the retrieved passage and generation. [Self-RAG](https://arxiv.org/abs/2310.11511) [ICLR paper](https://proceedings.iclr.cc/paper_files/paper/2024/hash/25f7be9694d7b32d5cc670927b8091e1-Abstract-Conference.html)

Adaptive-RAG uses a classifier to choose no retrieval, single-step retrieval, or iterative retrieval based on query complexity. The relevant design lesson is not the particular classifier; it is that retrieval strategy can be routed instead of applied uniformly. [Adaptive-RAG](https://arxiv.org/abs/2403.14403) [NAACL paper](https://aclanthology.org/2024.naacl-long.389/)

ProactAgent (2026 preprint) treats retrieval as an explicit policy action and learns when retrieval improves task outcomes or efficiency. It reports up to 32% relative success-rate improvement and more than 33% fewer interaction rounds on its evaluated environments. This is direct evidence for a "missing-context trigger" rather than a task-start query. [ProactAgent](https://arxiv.org/abs/2604.20572)

### Context and token tradeoffs

Mem0's paper reports 91% lower p95 latency and more than 90% token savings versus full-context processing on LoCoMo, with a graph variant improving the base configuration. These are useful directional results, but they are vendor-authored, benchmark-specific, and depend on the LLM/judge and retrieval settings. [Mem0 paper](https://arxiv.org/abs/2504.19413)

Zep's paper reports strong Deep Memory Retrieval and LongMemEval results using a temporal graph, but the deployment requirements are materially heavier than a local SQLite store. [Zep paper](https://arxiv.org/abs/2501.13956)

LazyMem (2026 preprint) preserves raw interactions and defers construction to query time, then compresses a retrieved pool with a lightweight model. It reports 213 answer-context memory tokens on LongMemEval, 21 times fewer than its strongest non-oracle baseline, while retaining an 0.85 LLM-judge accuracy. This supports query-conditioned compression, but it still performs retrieval and construction when a query arrives. [LazyMem](https://arxiv.org/abs/2607.22690)

ActiveContext (2026 preprint) trains a lightweight context curator and reports an 8x token reduction on DeepSearch with a small success-rate improvement. It shows that curation can be learned, but also illustrates the cost and complexity of doing it well. [ActiveContext](https://arxiv.org/abs/2604.11462)

### Benchmarks and limits

LongMemEval measures information extraction, multi-session reasoning, temporal reasoning, knowledge updates, and abstention. It reports roughly a 30% accuracy drop for commercial assistants and long-context LLMs under sustained interaction, and recommends session decomposition, fact-augmented keys, and time-aware query expansion. [LongMemEval](https://arxiv.org/abs/2410.10813) [ICLR version](https://proceedings.iclr.cc/paper_files/paper/2025/hash/d813d324dbf0598bbdc9c8e79740ed01-Abstract-Conference.html)

MemoryAgentBench (ICLR 2026) evaluates accurate retrieval, test-time learning, long-range understanding, and selective forgetting. Its results say current methods do not master all four, which is why the session tier must be allowed to expire or be overwritten rather than merely hidden. [MemoryAgentBench](https://arxiv.org/abs/2507.05257) [ICLR version](https://proceedings.iclr.cc/paper_files/paper/2026/hash/fd1eff9dd295df50a41f2521942fa31d-Abstract.html)

MemBench evaluates effectiveness, efficiency, and capacity, which are exactly the dimensions most memory product pages omit. [MemBench](https://arxiv.org/abs/2506.21605)

The 2026 "Harness the Memory" study finds no single substrate dominates: broad retrieval helps factual long-context QA, while excessive retrieval can hurt sequential decision-making. It also reports that only 21% of surveyed systems publish any efficiency metric. Treat all latency/token claims as provisional until tested in the target agent workload. [Harness the Memory](https://arxiv.org/abs/2608.15008)

## 5. Why the strict requirements conflict

There are three possible read paths:

1. **Always-on prompt injection.** Memory is written into the system or user context at session start. This gives zero retrieval calls but consumes tokens every turn and eventually injects logs or summaries.
2. **Model-controlled retrieval.** A memory tool is exposed through MCP or a native tool. This avoids resident memory content but adds tool schemas, occasional calls, latency, and model-choice reliability.
3. **Host-side lazy routing.** The host detects a missing-context condition and invokes a local memory service outside the normal prompt path. This can meet both overhead requirements, but it must be implemented in each client or through a shared plugin/hook layer.

MCP makes the second path portable but not invisible. The MCP specification defines model-visible tool names, descriptions, and input schemas; Claude Code documentation explicitly discusses tool definitions consuming context and uses tool search to withhold definitions when needed. Codex documents MCP tool catalogs and deferred tool exposure. An always-connected memory server therefore has fixed schema overhead even when it is not called. [MCP tools specification](https://modelcontextprotocol.io/specification/2024-11-05/server/tools) [Claude Code MCP](https://code.claude.com/docs/en/mcp) [Codex MCP](https://developers.openai.com/codex/mcp)

Consequences:

- "Zero prompt bloat" can mean no raw conversation-log chunks. Small tool schemas and a few stable rules may be acceptable.
- If it means zero fixed bytes, use lazy tool discovery, a single composite tool, or host hooks. Do not expose a large memory tool catalog every turn.
- "Zero retrieval calls on standard operations" requires a trigger policy. A blanket "call memory at task start" rule fails this requirement even if the result is compact.
- A strict user/project/session model is not the same as the semantic/episodic/procedural model. Store both dimensions explicitly.

## 6. Recommended target architecture

### Storage

Use a local SQLite database as the canonical store. Use FTS5/BM25 on the hot path and optional local embeddings only for semantic recall. Keep human-readable Markdown mirrors or exports for inspection and migration. This pattern is present in `memories.sh`, Basic Memory, and Memorix, and avoids a vector database or server fleet.

Each record should have at least:

`id`, `scope` (`user`, `project`, `session`), `project_id`, `lifecycle` (`semantic`, `episodic`, `procedural`), `content`, `source`, `confidence`, `created_at`, `last_confirmed_at`, `expires_at`, `supersedes`, and `status`.

`project_id` should come from the real Git root and remote identity, not from the current folder name. Session records should be invisible after `expires_at` or after the task closes. A superseding fact should replace the active claim while preserving provenance.

### Hot path

Keep only a tiny policy layer resident:

- user formatting and personal shortcuts in a global `AGENTS.md`/equivalent;
- a few project commands and non-derivable conventions in project `AGENTS.md`;
- no raw session logs, no full history, and no complete memory dump.

Use path-scoped rules or skills for content that applies only to a subtree or workflow. Claude Code's own documentation warns that imports still load at launch, while path-scoped rules and skills reduce resident context. [Claude Code memory](https://code.claude.com/docs/en/memory)

Expose one lazy memory entry point, not nine or twenty always-visible tools. If the host supports tool search/deferred tools, register the memory tool as deferred. Otherwise use a small plugin/hook that calls the store only after a trigger.

### Missing-context triggers

The router should call memory only when one of these conditions is true:

- the user refers to prior work ("last time", "we decided", "as agreed", "remember");
- an unknown project constraint, owner, command, or external dependency is needed;
- repository and documentation inspection cannot resolve the question;
- a hypothesis has failed repeatedly or evidence conflicts;
- the task explicitly crosses sessions, devices, or agents;
- the request is a memory query rather than a normal code change.

A simple syntax edit, local refactor, command execution, or question answerable from current files should record zero memory calls. The trigger should be deterministic where possible and logged for evaluation.

### Triggered read

When triggered, return a compact claim packet:

- top 3 relevant claims;
- one-line source/provenance;
- current/superseded status;
- a link or ID for detail lookup;
- at most a small token budget, such as 200-400 tokens.

Do not return raw conversation transcripts. Expand to an episodic slice or full record only if the compact packet is insufficient. This matches Memorix's progressive disclosure and LazyMem's query-conditioned compression.

### Write path

Run extraction after an idle period or at a task boundary, not on every turn. Keep the extraction prompt conservative:

- accept durable preferences, stable project facts, architecture decisions, repeatable workflows, and confirmed fixes;
- reject greetings, filler, transient code output, secrets, speculative hypotheses, and facts already derivable from the repository;
- require evidence or explicit user confirmation for high-impact project decisions;
- promote a procedural memory only after repeated successful use;
- expire session hypotheses and overwrite stale semantic claims.

This follows Codex's idle/background and secret-redaction behavior, Claude Code's code-derived and duplicate filtering, and the consolidation rules in `memories.sh`.

### Cross-tool integration

Use `AGENTS.md` as the portable tiny policy layer and one shared local store behind MCP or a CLI. Generate native files only for stable rules and path-scoped guidance. Do not generate every memory into `CLAUDE.md`, `.cursor/rules`, or `AGENTS.md`.

For a near-strict setup based on existing software:

1. Use `memories.sh` or Memorix for the store and scope model.
2. Disable automatic task-start recall in the generated guidance.
3. Set Memorix `memory.inject = "silent"` if using its hook path; use its micro profile or a single composite tool.
4. Keep generated instructions to a small rule set.
5. Add a shared trigger policy to each client's plugin/skill.
6. Use background extraction and explicit review for the first weeks.

This is configuration and integration work, not a from-scratch memory engine.

## 7. Candidate recommendation

### Recommended base: `memories.sh`, with policy changes

Choose `memories.sh` when the priority is a small local runtime, zero paid memory service, straightforward global/project/session data, and broad tool generation. Its implementation already contains most of the storage primitives required. Replace its task-start recall instructions, cap generated resident content, and enforce a trigger-based read path.

### Alternative: Memorix

Choose Memorix when cross-agent coverage including Hermes Agent, Git-derived project truth, curated long-term records, and project-safe default retrieval are more important than minimal surface area. Use a small tool profile and silent/minimal injection, and treat the task Workset as an on-demand capability rather than a mandatory first call.

### Keep native memory as a complementary layer

Codex and Claude Code are good at automatic, conservative extraction. They can remain enabled for product-specific learning while a shared store holds portable facts. Do not treat their generated memory as the single source of truth unless all work stays within one product.

### Do not choose these for the stated constraints without relaxing them

- Mem0/OpenMemory: useful extraction, but default external LLM/embedding dependency or Docker/Postgres operation.
- Zep/Graphiti: excellent temporal graph behavior, but database and model-service requirements.
- Letta/MemGPT: strong context-management research and agent runtime, but resident blocks and product-specific integration.
- Basic Memory: good local knowledge base, but no native three-tier TTL or automatic preference extraction.

## 8. Validation plan

Run these checks against the chosen base before treating it as compliant:

1. **Routine-turn overhead:** run 20 ordinary code edits and tests. Save the prompt/token trace and assert zero memory calls and zero injected transcript bytes.
2. **Triggered recall:** run tasks that refer to a past decision, a prior fix, and a user preference. Assert exactly one bounded retrieval on each.
3. **Cross-client parity:** write one fact through Codex, Claude Code, Cursor, and a terminal client; retrieve it from each and compare scope, content, and provenance.
4. **Scope isolation:** write user, project A, project B, and session facts. Query each from each project and assert no leakage.
5. **Session expiry:** create a hypothesis, advance the TTL or close the task, and assert it is unavailable to normal recall.
6. **Extraction precision:** manually label 100 candidate memories and measure the percentage that are durable, non-secret, non-transient, and useful.
7. **Token budget:** measure resident prompt tokens and triggered memory tokens separately. Record the configured budget and the actual distribution.
8. **Resource profile:** measure idle RSS/CPU and peak local embedding/model cost on the target laptop. Do not rely on vendor "minimal footprint" wording.
9. **Failure behavior:** test SQLite lock contention, missing embeddings, corrupt records, and an unavailable optional model. Reads should fail clearly rather than silently return an empty memory.
10. **Regression benchmark:** run a small LongMemEval/MemoryAgentBench-style set plus sequential coding tasks. The 2026 substrate study warns that broad retrieval can help QA while harming action-oriented work, so evaluate both.

## 9. Bottom line

The requirements define a lazy, host-routed memory system, not merely a memory database. Existing products provide the pieces but not the complete policy:

- Codex and Claude Code provide the best automatic filtering, but are product-specific and inject or load memory into sessions.
- `memories.sh` and Memorix provide the best local cross-tool substrate, but their defaults favor task-start recall and can grow resident context.
- Basic Memory is the simplest human-readable local fallback, but lacks the requested lifecycle and extraction policy.
- Mem0, Zep/Graphiti, and Letta improve memory quality or context management at the cost of external services, databases, model runtimes, or prompt residency.

The practical route is to use a local SQLite-based system as a substrate, keep only a tiny `AGENTS.md` policy resident, and add a shared trigger-based read path with bounded results and conservative background extraction. That route is close to turnkey today, but the trigger/router layer is still the one component that must be integrated explicitly.

## 10. Source re-check and feasibility

All cited source URLs were checked again on 2026-10-02. The final repeatable audit is implemented in [`benchmarks/check_sources.py`](benchmarks/check_sources.py) and checked 71/71 URLs successfully on that date. A later run with two added sources read 72 of 73: the ICLR 2026 proceedings page for MemoryAgentBench could not be scraped, while the arXiv link for the same paper passed. The ACM HTML viewer and GitHub's OpenMemory directory page were not readable by the scraper, so this report now uses the arXiv paper and a stable OpenMemory README URL.

The sources converge on a feasible cross-client pattern:

1. **A shared local source of truth.** SQLite, Markdown, or another local store removes vendor lock-in and lets terminal and IDE clients read the same records. `memories.sh`, Memorix, Basic Memory, and MCP all demonstrate parts of this pattern.
2. **A tiny portable policy layer.** `AGENTS.md` and client-specific skills say when memory should be used without copying conversation logs into every prompt. The AGENTS.md standard and native client documentation support this layer.
3. **One compact tool boundary.** MCP provides a common callable interface. Lazy or deferred tool discovery can keep schemas out of routine context, although one small schema remains the portability cost.
4. **Progressive disclosure.** Strong systems return a compact claim or task workset first and expose timeline/detail only when needed. Memorix and query-time construction research support this design.
5. **A conservative write path.** Codex and Claude Code show how background extraction can reject short-lived sessions, secrets, derivable facts, and duplicate instructions. A local implementation should reproduce those rules before pursuing broader semantic extraction.

These mechanisms make memory usable across clients. They do not eliminate representation limits: semantic multi-hop and commonsense recall still depend on retrieval quality, while answer quality depends on the model that reads the evidence.

## 11. Real tests for `unimem`

The full methodology and reproduction commands are in [`benchmarks/README.md`](benchmarks/README.md). The runner uses the real extraction, SQLite search, CLI, and MCP tool code paths and reports separate metrics rather than one composite score.

| Source-derived capability | Real test | Current result |
|---|---|---|
| Self-RAG / Adaptive-RAG: retrieve only when useful | 20 routine and missing-context routing cases | Routing F1 1.00 |
| Codex / Claude Code: selective extraction | 20 durable, noisy, secret, and transient cases | Precision 1.00, recall 1.00 |
| LongMemEval: extraction and multi-session evidence | LongMemEval-S session retrieval, LoCoMo evidence-ID retrieval, controlled update cases | LongMemEval-S session recall @5 0.96; LoCoMo evidence recall @3 0.54, @10 0.69 |
| MemoryAgentBench: learning and forgetting | Test-time write/recall and closed-session expiry | Both pass; forgetting rate 1.00 |
| MemBench / Harness the Memory: efficiency and capacity | 1,000 records and 50 bounded recalls | Recall p50 2.02 ms, p95 2.25 ms, max 163 output tokens |
| MCP portability | Database, CLI, and MCP-tool parity | Identical result IDs |

The system is strong on safety, scope, lifecycle, token bounds, and direct retrieval. It is below the predeclared LoCoMo targets, especially for multi-hop and commonsense questions. Current details are in [`artifacts/memory-quality.json`](artifacts/memory-quality.json).

A later full run across LoCoMo, MemBench, MemoryAgentBench, and BEAM, after tuning the keyword index (stemming, stopwords, per-scope index pruning), reached 0.82 balanced evidence recall @10 and 0.71 @3 with every safety gate passing, 10 ms recall p95, 333 MB peak memory for the benchmark process, and no external calls. A local embedding hybrid scored below the tuned keyword index on LoCoMo, so it stays optional. Optional write-time enrichment with a local 1.2B model (`unimem enrich`) raised full-LoCoMo evidence recall from 0.54 to 0.62 @3 and from 0.69 to 0.79 @10 with no model on the recall path; it is still below the 0.70 / 0.90 targets, and commonsense questions remain weak (0.49 @10). Results and commands are in [`benchmarks/README.md`](benchmarks/README.md).

On retrieval benchmarks that other systems publish without an LLM in the loop, `unimem` is competitive but not first:

| Benchmark and metric | unimem | Published |
|---|---:|---|
| LongMemEval-S session recall @5 | 0.960 (0.970 over distinct sessions) | MemPalace raw 0.966, tuned hybrid 0.984 held-out; SelRoute 0.920; BM25 0.862 |
| LoCoMo session recall @10, fractional | 0.899 keyword only, 0.912 enriched | MemPalace bge-large hybrid 0.924, hybrid v5 0.889; Memori 0.820 |

The published figures are those projects' own numbers ([MemPalace](https://github.com/MemPalace/mempalace/blob/develop/benchmarks/BENCHMARKS.md), [SelRoute](https://arxiv.org/abs/2604.02431)) and were not reproduced here. Those systems return whole sessions, while `unimem` returns single turns inside a 400-token budget, so the comparison is conservative for `unimem` on output size and not like-for-like on granularity.

With an LLM answering from recalled memories, scored in the open Agent Memory Benchmark harness with Gemini 3.8 Flash answering and judging, partial runs estimate 91.5% on LongMemEval-S and 88.7% on LoCoMo. On AMB's own leaderboard that is ahead of cognee (80.3% LoCoMo) and hybrid search (74.0% / 79.1%), and about 3 points behind Hindsight (94.6% / 92.0%), which uses two to three times the context. Details and caveats are in [`benchmarks/README.md`](benchmarks/README.md).

Against the systems surveyed here, `unimem` is the only option that meets every stated requirement at once: no injected context on routine turns, one shared store for all clients, strict user/project/session tiers, and zero external API or service cost. Mem0, Zep, and Letta report higher answer-level scores, but they rely on hosted LLMs, databases, or resident memory blocks, which the requirements rule out, and their numbers are measured with LLM judges that this local benchmark does not use.

## Sources

Primary product and protocol sources:

- [Codex Memories](https://developers.openai.com/codex/customization/memories)
- [Codex Customization](https://developers.openai.com/codex/concepts/customization)
- [Codex AGENTS.md](https://developers.openai.com/codex/agent-configuration/agents-md)
- [Codex MCP](https://developers.openai.com/codex/mcp)
- [Claude Code memory](https://code.claude.com/docs/en/memory)
- [Claude Code MCP](https://code.claude.com/docs/en/mcp)
- [Cursor Rules](https://cursor.com/docs/rules)
- [Cursor Automations](https://cursor.com/docs/cloud-agent/automations)
- [AGENTS.md](https://agents.md/)
- [MCP tools specification](https://modelcontextprotocol.io/specification/2024-11-05/server/tools)
- [memories.sh](https://memories.sh/docs)
- [memories.sh source](https://github.com/webrenew/memories)
- [Memorix](https://github.com/AVIDS2/memorix)
- [Basic Memory](https://docs.basicmemory.com/reference/technical-information)
- [Mem0](https://docs.mem0.ai/overview)
- [Graphiti](https://help.getzep.com/graphiti/getting-started/quick-start)
- [Letta memory blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks)

Research sources:

- [CoALA](https://arxiv.org/abs/2309.02427)
- [Generative Agents](https://arxiv.org/abs/2304.03442)
- [MemGPT](https://arxiv.org/abs/2310.08560)
- [Self-RAG](https://arxiv.org/abs/2310.11511)
- [Adaptive-RAG](https://arxiv.org/abs/2403.14403)
- [LongMemEval](https://arxiv.org/abs/2410.10813)
- [MemInsight](https://arxiv.org/abs/2503.21760)
- [Mem0 paper](https://arxiv.org/abs/2504.19413)
- [Zep paper](https://arxiv.org/abs/2501.13956)
- [MemoryAgentBench](https://arxiv.org/abs/2507.05257)
- [MemBench](https://arxiv.org/abs/2506.21605)
- [ProactAgent](https://arxiv.org/abs/2604.20572)
- [ActiveContext](https://arxiv.org/abs/2604.11462)
- [LazyMem](https://arxiv.org/abs/2607.22690)
- [Harness the Memory](https://arxiv.org/abs/2608.15008)

Evaluation repositories and datasets:

- [LongMemEval repository](https://github.com/xiaowu0162/LongMemEval)
- [LongMemEval-V2 repository](https://github.com/xiaowu0162/LongMemEval-V2)
- [LongMemEval cleaned dataset](https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned)
- [LoCoMo repository](https://github.com/snap-research/LoCoMo)
- [MemoryAgentBench repository](https://github.com/HUST-AI-HYZ/MemoryAgentBench)
- [MemoryAgentBench dataset](https://huggingface.co/datasets/ai-hyz/MemoryAgentBench)
- [MemBench repository](https://github.com/import-myself/Membench)
- [Mem0 memory benchmark suite](https://github.com/mem0ai/memory-benchmarks)
- [MemPalace benchmark results](https://github.com/MemPalace/mempalace/blob/develop/benchmarks/BENCHMARKS.md)
- [SelRoute](https://arxiv.org/abs/2604.02431)
- [Agent Memory Benchmark](https://github.com/vectorize-io/agent-memory-benchmark)

Research confidence: high for documented product behavior and architecture; medium for comparative performance numbers; low-to-medium for 2026 preprints and vendor benchmark claims until independently reproduced.
