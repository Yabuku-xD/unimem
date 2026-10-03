from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import Settings

MARKER_START = "<!-- unimem:start -->"
MARKER_END = "<!-- unimem:end -->"
BLOCK_START = "# BEGIN UNIMEM"
BLOCK_END = "# END UNIMEM"

# Clients configured inside the project directory.
PROJECT_CLIENTS = ("agents", "claude", "cursor", "codex")
# Clients configured once per user; they serve every project.
GLOBAL_CLIENTS = ("claude-desktop", "codex-app", "pi", "hermes")
CLIENTS = PROJECT_CLIENTS + GLOBAL_CLIENTS
CLIENT_ALIASES = {
    "claude-code": "claude",
    "terminal": "agents",
    "chatgpt": "codex-app",
}


class IntegrationError(ValueError):
    """A client configuration file cannot be updated safely."""


def skill_text() -> str:
    return """---
name: unimem
description: Use when prior work, stored preferences, or project decisions are needed and repository or documentation context is insufficient. Also use after confirming a durable preference, decision, fact, or procedure for future work.
---

# unimem

1. Inspect the repository and current documentation first.
2. Recall memory only for prior-work references, missing constraints, cross-session handoffs, or conflicting evidence. Call the `unimem` MCP tool with `action=recall`, a matching `trigger`, and specific `evidence`.
3. Store only durable preferences, decisions, facts, and procedures after the user or code confirms them. Use `action=remember`; keep session hypotheses in the session scope.
4. Pass `project_dir` with the workspace's absolute path when the tool may run outside the project.
5. Return compact claims and leave raw transcripts, secrets, command output, and transient code out of memory.
"""


def agents_section() -> str:
    return f"""{MARKER_START}
## unimem

When prior work, stored preferences, or project decisions are needed and repository or documentation context is insufficient, read `.agents/skills/unimem/SKILL.md`.
Use the `unimem` memory tool only for those missing-context branches. Routine code work stays free of memory calls.
{MARKER_END}
"""


def launcher(settings: Settings) -> dict[str, Any]:
    # Desktop apps do not inherit the shell PATH, so always write an absolute command.
    installed = shutil.which("unimem")
    env = {"UNIMEM_HOME": str(settings.home)}
    if installed:
        return {"command": installed, "args": ["mcp"], "env": env}
    source_root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(source_root / "src")
    return {"command": sys.executable, "args": ["-m", "unimem", "mcp"], "env": env}


def claude_desktop_config_path() -> Path:
    """Location documented at modelcontextprotocol.io for Claude Desktop."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Claude/claude_desktop_config.json"
    return Path.home() / ".config/Claude/claude_desktop_config.json"


def _merge_json(path: Path, key: str, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists() and path.read_text(encoding="utf-8").strip():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            # Never overwrite a config the user can still repair.
            raise IntegrationError(f"{path} is not valid JSON ({error}); fix it and rerun init") from error
        if not isinstance(loaded, dict):
            raise IntegrationError(f"{path} must contain a JSON object; fix it and rerun init")
        data = loaded
    existing = data.get(key)
    if not isinstance(existing, dict):
        existing = {}
    existing.update(value)
    data[key] = existing
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _replace_block(original: str, block: str, start: str, end: str) -> str:
    if start in original and end in original:
        prefix, rest = original.split(start, 1)
        _, suffix = rest.split(end, 1)
        return prefix + block + suffix.lstrip("\n")
    return original.rstrip() + ("\n\n" if original.strip() else "") + block


def _merge_codex_toml(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    unmanaged = original
    if BLOCK_START in original and BLOCK_END in original:
        prefix, rest = original.split(BLOCK_START, 1)
        unmanaged = prefix + rest.split(BLOCK_END, 1)[1]
    if "[mcp_servers.unimem]" in unmanaged:
        raise IntegrationError(
            f"{path} already defines [mcp_servers.unimem]; remove it and rerun init"
        )
    env_items = ", ".join(f"{key} = {json.dumps(str(value))}" for key, value in config["env"].items())
    args = ", ".join(json.dumps(str(arg)) for arg in config["args"])
    block = f"""{BLOCK_START}
[mcp_servers.unimem]
command = {json.dumps(str(config['command']))}
args = [{args}]
env = {{ {env_items} }}
{BLOCK_END}
"""
    path.write_text(_replace_block(original, block, BLOCK_START, BLOCK_END), encoding="utf-8")


def _merge_hermes_yaml(path: Path, config: dict[str, Any]) -> None:
    """Add unimem under `mcp_servers:` in Hermes' config.yaml without a YAML parser."""
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    entry = [
        f"  {BLOCK_START}",
        "  unimem:",
        f"    command: {json.dumps(str(config['command']))}",
        f"    args: {json.dumps([str(arg) for arg in config['args']])}",
        "    env:",
        *(f"      {key}: {json.dumps(str(value))}" for key, value in config["env"].items()),
        f"  {BLOCK_END}",
    ]
    lines = original.splitlines()
    if f"  {BLOCK_START}" in lines and f"  {BLOCK_END}" in lines:
        start = lines.index(f"  {BLOCK_START}")
        end = lines.index(f"  {BLOCK_END}")
        lines[start : end + 1] = entry
    elif "mcp_servers:" in lines:
        section = lines.index("mcp_servers:")
        following = lines[section + 1 :]
        body_end = next(
            (i for i, line in enumerate(following) if line.strip() and not line[0].isspace()),
            len(following),
        )
        if any(line.strip() == "unimem:" for line in following[:body_end]):
            raise IntegrationError(f"{path} already defines mcp_servers.unimem; remove it and rerun init")
        lines[section + 1 : section + 1] = entry
    elif any(line.startswith("mcp_servers:") for line in lines):
        raise IntegrationError(
            f"{path} uses an inline mcp_servers value; add unimem by hand or convert it to a block"
        )
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += ["mcp_servers:", *entry]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_skill(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_text(), encoding="utf-8")


def _write_agents(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(_replace_block(original, agents_section(), MARKER_START, MARKER_END), encoding="utf-8")


def resolve_clients(clients: tuple[str, ...]) -> set[str]:
    selected = {CLIENT_ALIASES.get(client, client) for client in clients}
    if "all" in selected:
        return set(CLIENTS)
    unknown = selected - set(CLIENTS)
    if unknown:
        raise IntegrationError(f"unknown client: {', '.join(sorted(unknown))}")
    return selected


def install_integrations(
    settings: Settings,
    *,
    project_dir: Path,
    clients: tuple[str, ...],
    mcp_tool_schema_bytes: int,
) -> dict[str, Any]:
    selected = resolve_clients(clients)
    home = Path.home()
    files: list[str] = []

    def wrote(path: Path) -> None:
        files.append(str(path))

    if selected & set(PROJECT_CLIENTS):
        _write_agents(project_dir / "AGENTS.md")
        wrote(project_dir / "AGENTS.md")
    if selected & {"agents", "claude", "codex"}:
        _write_skill(project_dir / ".agents/skills/unimem/SKILL.md")
        wrote(project_dir / ".agents/skills/unimem/SKILL.md")
    if "claude" in selected:
        _write_skill(project_dir / ".claude/skills/unimem/SKILL.md")
        wrote(project_dir / ".claude/skills/unimem/SKILL.md")
    if "cursor" in selected:
        _write_skill(project_dir / ".cursor/skills/unimem/SKILL.md")
        wrote(project_dir / ".cursor/skills/unimem/SKILL.md")
    # Codex (CLI and desktop app) and Pi read user skills from ~/.agents/skills.
    if selected & {"codex-app", "pi"}:
        _write_skill(home / ".agents/skills/unimem/SKILL.md")
        wrote(home / ".agents/skills/unimem/SKILL.md")
    if "hermes" in selected:
        _write_skill(home / ".hermes/skills/unimem/SKILL.md")
        wrote(home / ".hermes/skills/unimem/SKILL.md")

    config = launcher(settings)
    servers = {"unimem": config}
    if "claude" in selected:
        _merge_json(project_dir / ".mcp.json", "mcpServers", servers)
        wrote(project_dir / ".mcp.json")
    if "cursor" in selected:
        _merge_json(project_dir / ".cursor/mcp.json", "mcpServers", servers)
        wrote(project_dir / ".cursor/mcp.json")
    if "codex" in selected:
        _merge_codex_toml(project_dir / ".codex/config.toml", config)
        wrote(project_dir / ".codex/config.toml")
    if "claude-desktop" in selected:
        path = claude_desktop_config_path()
        _merge_json(path, "mcpServers", servers)
        wrote(path)
    if "codex-app" in selected:
        # Shared by the ChatGPT desktop app, the Codex CLI, and the IDE extension.
        _merge_codex_toml(home / ".codex/config.toml", config)
        wrote(home / ".codex/config.toml")
    if "pi" in selected:
        _merge_json(home / ".pi/agent/mcp.json", "mcpServers", servers)
        wrote(home / ".pi/agent/mcp.json")
    if "hermes" in selected:
        _merge_hermes_yaml(home / ".hermes/config.yaml", config)
        wrote(home / ".hermes/config.yaml")

    restart = sorted(selected & {"claude-desktop", "codex-app", "hermes", "pi"})
    resident_bytes = len(agents_section().encode("utf-8")) + len(skill_text().split("---", 2)[-1].encode("utf-8"))
    return {
        "ok": True,
        "project_id": settings.project_id,
        "clients": sorted(selected),
        "files": files,
        "restart_required": restart,
        "resident_instruction_bytes": resident_bytes,
        "mcp_tool_schema_bytes": mcp_tool_schema_bytes,
        "launcher": config,
    }
