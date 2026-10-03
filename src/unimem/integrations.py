from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import Settings

MARKER_START = "<!-- unimem:start -->"
MARKER_END = "<!-- unimem:end -->"
TOML_START = "# BEGIN UNIMEM"
TOML_END = "# END UNIMEM"
CLIENT_ALIASES = {
    "claude-code": "claude",
    "terminal": "agents",
}


def skill_text() -> str:
    return """---
name: unimem
description: Use when prior work, stored preferences, or project decisions are needed and repository or documentation context is insufficient. Also use after confirming a durable preference, decision, fact, or procedure for future work.
---

# unimem

1. Inspect the repository and current documentation first.
2. Recall memory only for prior-work references, missing constraints, cross-session handoffs, or conflicting evidence. Call the `unimem` MCP tool with `action=recall`, a matching `trigger`, and specific `evidence`.
3. Store only durable preferences, decisions, facts, and procedures after the user or code confirms them. Use `action=remember`; keep session hypotheses in the session scope.
4. Return compact claims and leave raw transcripts, secrets, command output, and transient code out of memory.
"""


def agents_section() -> str:
    return f"""{MARKER_START}
## unimem

When prior work, stored preferences, or project decisions are needed and repository or documentation context is insufficient, read `.agents/skills/unimem/SKILL.md`.
Use the `unimem` memory tool only for those missing-context branches. Routine code work stays free of memory calls.
{MARKER_END}
"""


def launcher(settings: Settings) -> dict[str, Any]:
    installed = shutil.which("unimem")
    env = {"UNIMEM_HOME": str(settings.home)}
    if installed:
        return {"command": installed, "args": ["mcp"], "env": env}
    source_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(source_root / "src")
    return {"command": sys.executable, "args": ["-m", "unimem", "mcp"], "env": env}


def _merge_json(path: Path, key: str, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    existing = data.setdefault(key, {})
    if not isinstance(existing, dict):
        data[key] = {}
    data[key].update(value)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _merge_codex_toml(path: Path, block: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if TOML_START in original and TOML_END in original:
        prefix, rest = original.split(TOML_START, 1)
        _, suffix = rest.split(TOML_END, 1)
        updated = prefix + block + suffix
    else:
        updated = original.rstrip() + ("\n\n" if original.strip() else "") + block
    path.write_text(updated, encoding="utf-8")


def _write_skill(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_text(), encoding="utf-8")


def _write_agents(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    section = agents_section()
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    if MARKER_START in original and MARKER_END in original:
        prefix, rest = original.split(MARKER_START, 1)
        _, suffix = rest.split(MARKER_END, 1)
        updated = prefix + section + suffix
    else:
        updated = original.rstrip() + ("\n\n" if original.strip() else "") + section
    path.write_text(updated, encoding="utf-8")


def install_integrations(
    settings: Settings,
    *,
    project_dir: Path,
    clients: tuple[str, ...],
    mcp_tool_schema_bytes: int,
) -> dict[str, Any]:
    selected = {CLIENT_ALIASES.get(client, client) for client in clients}
    if "all" in selected:
        selected = {"agents", "claude", "cursor", "codex"}
    files: list[str] = []
    if selected & {"agents", "claude", "cursor", "codex"}:
        agents_path = project_dir / "AGENTS.md"
        _write_agents(agents_path)
        files.append(str(agents_path))
    if selected & {"agents", "claude", "codex"}:
        skill_path = project_dir / ".agents/skills/unimem/SKILL.md"
        _write_skill(skill_path)
        files.append(str(skill_path))
    if "claude" in selected:
        skill_path = project_dir / ".claude/skills/unimem/SKILL.md"
        _write_skill(skill_path)
        files.append(str(skill_path))
    if "cursor" in selected:
        skill_path = project_dir / ".cursor/skills/unimem/SKILL.md"
        _write_skill(skill_path)
        files.append(str(skill_path))

    config = launcher(settings)
    if "claude" in selected:
        path = project_dir / ".mcp.json"
        _merge_json(path, "mcpServers", {"unimem": config})
        files.append(str(path))
    if "cursor" in selected:
        path = project_dir / ".cursor/mcp.json"
        _merge_json(path, "mcpServers", {"unimem": config})
        files.append(str(path))
    if "codex" in selected:
        env_items = ", ".join(f"{key} = {json.dumps(str(value))}" for key, value in config["env"].items())
        args = ", ".join(json.dumps(str(arg)) for arg in config["args"])
        block = f"""{TOML_START}
[mcp_servers.unimem]
command = {json.dumps(str(config['command']))}
args = [{args}]
env = {{ {env_items} }}
{TOML_END}
"""
        path = project_dir / ".codex/config.toml"
        _merge_codex_toml(path, block)
        files.append(str(path))

    resident_bytes = len(agents_section().encode("utf-8")) + len(skill_text().split("---", 2)[-1].encode("utf-8"))
    return {
        "ok": True,
        "project_id": settings.project_id,
        "clients": sorted(selected),
        "files": files,
        "resident_instruction_bytes": resident_bytes,
        "mcp_tool_schema_bytes": mcp_tool_schema_bytes,
        "launcher": config,
    }
