from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import Settings

MARKER_START = "<!-- unimem:start -->"
MARKER_END = "<!-- unimem:end -->"
BLOCK_START = "# BEGIN UNIMEM"
BLOCK_END = "# END UNIMEM"

CLIENTS = ("agents", "claude", "claude-desktop", "codex", "cursor", "pi", "hermes")
# Clients that can also be configured inside one project with --project.
PROJECT_CLIENTS = ("agents", "claude", "codex", "cursor")
# Clients with documented session hooks; Claude Desktop has none.
HOOK_CLIENTS = ("claude", "codex", "cursor", "pi", "hermes")
CLIENT_ALIASES = {
    "claude-code": "claude",
    "terminal": "agents",
    "codex-app": "codex",
    "chatgpt": "codex",
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


def _load_json_object(path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        # Never overwrite a config the user can still repair.
        raise IntegrationError(f"{path} is not valid JSON ({error}); fix it and rerun init") from error
    if not isinstance(loaded, dict):
        raise IntegrationError(f"{path} must contain a JSON object; fix it and rerun init")
    return loaded


def _merge_json(path: Path, key: str, value: dict[str, Any]) -> None:
    data = _load_json_object(path)
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


def _merge_yaml_section(
    path: Path, key: str, body: list[str], *, start: str, end: str, owned: tuple[str, ...]
) -> None:
    """Add a marked block under a top-level YAML key without a YAML parser.

    `owned` lists the child keys the block defines; an existing definition of
    one outside the markers is reported instead of duplicated.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    entry = [f"  {start}", *body, f"  {end}"]
    if f"  {start}" in lines and f"  {end}" in lines:
        first = lines.index(f"  {start}")
        last = lines.index(f"  {end}")
        lines[first : last + 1] = entry
    elif f"{key}:" in lines:
        section = lines.index(f"{key}:")
        following = lines[section + 1 :]
        body_end = next(
            (i for i, line in enumerate(following) if line.strip() and not line[0].isspace()),
            len(following),
        )
        taken = [name for name in owned if f"  {name}:" in following[:body_end]]
        if taken:
            raise IntegrationError(
                f"{path} already defines {key}.{taken[0]}; add unimem there by hand or remove it and rerun init"
            )
        lines[section + 1 : section + 1] = entry
    elif any(line.startswith(f"{key}:") for line in lines):
        raise IntegrationError(
            f"{path} uses an inline {key} value; add unimem by hand or convert it to a block"
        )
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines += [f"{key}:", *entry]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _merge_hermes_yaml(path: Path, config: dict[str, Any]) -> None:
    """Add unimem under `mcp_servers:` in Hermes' config.yaml."""
    body = [
        "  unimem:",
        f"    command: {json.dumps(str(config['command']))}",
        f"    args: {json.dumps([str(arg) for arg in config['args']])}",
        "    env:",
        *(f"      {key}: {json.dumps(str(value))}" for key, value in config["env"].items()),
    ]
    _merge_yaml_section(path, "mcp_servers", body, start=BLOCK_START, end=BLOCK_END, owned=("unimem",))


def hook_command(config: dict[str, Any], event: str, client: str) -> str:
    """Shell command a tool runs for a session hook, matching the MCP launcher."""
    parts = [str(config["command"]), *[str(arg) for arg in config["args"][:-1]]]
    parts += ["hook", event, "--client", client]
    default_home = str((Path.home() / ".unimem").resolve())
    env = {
        key: str(value)
        for key, value in config["env"].items()
        if not (key == "UNIMEM_HOME" and str(value) == default_home)
    }
    if env:
        # `env` rather than VAR=value so it also works where no shell is involved.
        parts = ["/usr/bin/env", *(f"{key}={value}" for key, value in env.items()), *parts]
    return shlex.join(parts)


def _is_unimem_hook(command: Any) -> bool:
    return isinstance(command, str) and "unimem" in command and " hook session-" in command


def _merge_claude_style_hooks(
    path: Path, config: dict[str, Any], client: str, *, end_timeout: int
) -> None:
    """Claude Code settings.json and Codex hooks.json share this hook shape."""
    data = _load_json_object(path)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    for event, name, extra in (
        ("SessionStart", "session-start", {}),
        ("SessionEnd", "session-end", {"timeout": end_timeout}),
    ):
        groups = [
            group
            for group in hooks.get(event, [])
            if not any(_is_unimem_hook(hook.get("command")) for hook in group.get("hooks", []))
        ]
        groups.append(
            {"hooks": [{"type": "command", "command": hook_command(config, name, client), **extra}]}
        )
        hooks[event] = groups
    data["hooks"] = hooks
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _merge_cursor_hooks(path: Path, config: dict[str, Any]) -> None:
    data = _load_json_object(path)
    data.setdefault("version", 1)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    for event, name in (("sessionStart", "session-start"), ("sessionEnd", "session-end")):
        entries = [entry for entry in hooks.get(event, []) if not _is_unimem_hook(entry.get("command"))]
        entries.append({"command": hook_command(config, name, "cursor")})
        hooks[event] = entries
    data["hooks"] = hooks
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _merge_hermes_hooks(path: Path, config: dict[str, Any]) -> None:
    body = [
        "  on_session_start:",
        f"    - command: {json.dumps(hook_command(config, 'session-start', 'hermes'))}",
        "  on_session_end:",
        f"    - command: {json.dumps(hook_command(config, 'session-end', 'hermes'))}",
    ]
    _merge_yaml_section(
        path,
        "hooks",
        body,
        start=f"{BLOCK_START} HOOKS",
        end=f"{BLOCK_END} HOOKS",
        owned=("on_session_start", "on_session_end"),
    )


def pi_extension_text(config: dict[str, Any]) -> str:
    command = shlex.split(hook_command(config, "EVENT", "pi"))
    base = command[: command.index("hook")]
    return f"""// Installed by `unimem init --client pi`. Rerunning init rewrites this file.
// Opens a unimem session when a Pi session starts, and saves durable facts
// from the transcript when it ends.
import type {{ ExtensionAPI }} from "@earendil-works/pi-coding-agent";

const UNIMEM = {json.dumps(base)};

export default function (pi: ExtensionAPI) {{
  const run = async (event: string, ctx: any) => {{
    const args = [...UNIMEM.slice(1), "hook", event, "--client", "pi", "--cwd", ctx.cwd];
    const sessionId = ctx.sessionManager?.getSessionId?.();
    const sessionFile = ctx.sessionManager?.getSessionFile?.();
    if (!sessionId) return;
    args.push("--session-id", sessionId);
    if (sessionFile) args.push("--transcript", sessionFile);
    try {{
      await pi.exec(UNIMEM[0], args);
    }} catch {{
      // Memory is optional; never interrupt the session.
    }}
  }};
  pi.on("session_start", (_event, ctx) => run("session-start", ctx));
  pi.on("session_shutdown", (_event, ctx) => run("session-end", ctx));
}}
"""


def _install_hooks(selected: set[str], config: dict[str, Any]) -> list[Path]:
    """Session hooks for every tool that documents them. Claude Desktop has none."""
    home = Path.home()
    files: list[Path] = []
    if "claude" in selected:
        # Claude Code allows SessionEnd hooks 1.5 s unless a timeout is set.
        _merge_claude_style_hooks(home / ".claude/settings.json", config, "claude", end_timeout=10)
        files.append(home / ".claude/settings.json")
    if "codex" in selected:
        # Codex caps SessionEnd hooks at 3 s.
        _merge_claude_style_hooks(home / ".codex/hooks.json", config, "codex", end_timeout=3)
        files.append(home / ".codex/hooks.json")
    if "cursor" in selected:
        _merge_cursor_hooks(home / ".cursor/hooks.json", config)
        files.append(home / ".cursor/hooks.json")
    if "pi" in selected:
        path = home / ".pi/agent/extensions/unimem.ts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(pi_extension_text(config), encoding="utf-8")
        files.append(path)
    if "hermes" in selected:
        _merge_hermes_hooks(home / ".hermes/config.yaml", config)
        files.append(home / ".hermes/config.yaml")
    return files


def _write_skill(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_text(), encoding="utf-8")


def _write_agents(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(_replace_block(original, agents_section(), MARKER_START, MARKER_END), encoding="utf-8")


def resolve_clients(clients: tuple[str, ...], *, project: bool = False) -> set[str]:
    selected = {CLIENT_ALIASES.get(client, client) for client in clients}
    if "all" in selected:
        return set(PROJECT_CLIENTS if project else CLIENTS)
    unknown = selected - set(CLIENTS)
    if unknown:
        raise IntegrationError(f"unknown client: {', '.join(sorted(unknown))}")
    user_only = selected - set(PROJECT_CLIENTS)
    if project and user_only:
        raise IntegrationError(
            f"{', '.join(sorted(user_only))} can only be set up for your user account; drop --project"
        )
    return selected


def _install_project(selected: set[str], project_dir: Path, servers: dict[str, Any], config: dict[str, Any]) -> list[str]:
    """Files a team can commit so everyone who clones the repository gets unimem."""
    files: list[Path] = [project_dir / "AGENTS.md"]
    _write_agents(project_dir / "AGENTS.md")
    if selected & {"agents", "claude", "codex"}:
        files.append(project_dir / ".agents/skills/unimem/SKILL.md")
    if "claude" in selected:
        files.append(project_dir / ".claude/skills/unimem/SKILL.md")
        _merge_json(project_dir / ".mcp.json", "mcpServers", servers)
        files.append(project_dir / ".mcp.json")
    if "cursor" in selected:
        files.append(project_dir / ".cursor/skills/unimem/SKILL.md")
        _merge_json(project_dir / ".cursor/mcp.json", "mcpServers", servers)
        files.append(project_dir / ".cursor/mcp.json")
    if "codex" in selected:
        _merge_codex_toml(project_dir / ".codex/config.toml", config)
        files.append(project_dir / ".codex/config.toml")
    for path in files:
        if path.name == "SKILL.md":
            _write_skill(path)
    return [str(path) for path in files]


def _install_user(
    selected: set[str], servers: dict[str, Any], config: dict[str, Any], *, hooks: bool
) -> list[str]:
    """One-time setup in the home directory; serves every project and session."""
    home = Path.home()
    files: list[Path] = []
    # Codex, Pi, Cursor, and other Agent Skills clients read ~/.agents/skills.
    if selected & {"agents", "codex", "pi", "cursor"}:
        files.append(home / ".agents/skills/unimem/SKILL.md")
    if "claude" in selected:
        files.append(home / ".claude/skills/unimem/SKILL.md")
        # User-scoped servers live at the top level of ~/.claude.json.
        _merge_json(home / ".claude.json", "mcpServers", servers)
        files.append(home / ".claude.json")
    if "claude-desktop" in selected:
        path = claude_desktop_config_path()
        _merge_json(path, "mcpServers", servers)
        files.append(path)
    if "codex" in selected:
        # Shared by the Codex CLI, the IDE extension, and the ChatGPT desktop app.
        _merge_codex_toml(home / ".codex/config.toml", config)
        files.append(home / ".codex/config.toml")
    if "cursor" in selected:
        _merge_json(home / ".cursor/mcp.json", "mcpServers", servers)
        files.append(home / ".cursor/mcp.json")
    if "pi" in selected:
        _merge_json(home / ".pi/agent/mcp.json", "mcpServers", servers)
        files.append(home / ".pi/agent/mcp.json")
    if "hermes" in selected:
        files.append(home / ".hermes/skills/unimem/SKILL.md")
        _merge_hermes_yaml(home / ".hermes/config.yaml", config)
        files.append(home / ".hermes/config.yaml")
    for path in files:
        if path.name == "SKILL.md":
            _write_skill(path)
    if hooks:
        files += [path for path in _install_hooks(selected, config) if path not in files]
    return [str(path) for path in files]


def install_integrations(
    settings: Settings,
    *,
    project_dir: Path,
    clients: tuple[str, ...],
    mcp_tool_schema_bytes: int,
    project: bool = False,
    hooks: bool = True,
) -> dict[str, Any]:
    selected = resolve_clients(clients, project=project)
    config = launcher(settings)
    servers = {"unimem": config}
    if project:
        files = _install_project(selected, project_dir, servers, config)
    else:
        files = _install_user(selected, servers, config, hooks=hooks)
    resident_bytes = len(agents_section().encode("utf-8")) + len(skill_text().split("---", 2)[-1].encode("utf-8"))
    return {
        "ok": True,
        "mode": "project" if project else "user",
        "project_id": settings.project_id,
        "clients": sorted(selected),
        "files": files,
        "restart_required": sorted(selected - {"agents"}),
        "session_hooks": sorted(selected & set(HOOK_CLIENTS)) if hooks and not project else [],
        "resident_instruction_bytes": resident_bytes,
        "mcp_tool_schema_bytes": mcp_tool_schema_bytes,
        "launcher": config,
    }
