from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings, now_iso, parse_iso
from .db import Database, MemoryError
from .extract import distill_messages, load_messages
from .hooks import normalize_payload, session_end, session_start
from .integrations import (
    CLIENT_ALIASES,
    CLIENTS,
    IntegrationError,
    agents_section,
    install_integrations,
    skill_text,
)
from .mcp import serve as serve_mcp
from .mcp import tool_schema_bytes
from .policy import classify_route, compact_recall_items, validate_recall


def _parser() -> argparse.ArgumentParser:
    # No prefix matching: on Python 3.11, "--project" would be read as an ambiguous
    # abbreviation of the global --project-dir and --project-id options.
    parser = argparse.ArgumentParser(
        prog="unimem", description="Local-first lazy memory for coding agents", allow_abbrev=False
    )
    parser.add_argument("--version", action="version", version=f"unimem {__version__}")
    parser.add_argument("--home", help="Override UNIMEM_HOME")
    parser.add_argument("--project-dir", help="Project directory used for project scope")
    parser.add_argument("--project-id", help="Override the derived project id")
    parser.add_argument("--session-id", help="Override UNIMEM_SESSION_ID")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Initialize storage and client integrations")
    init.add_argument(
        "--client",
        choices=["all", *CLIENTS, *CLIENT_ALIASES],
        default="all",
        help=(
            "Client to connect for your user account: claude (Claude Code), claude-desktop, "
            "codex (Codex CLI, IDE extension, and ChatGPT/Codex desktop app), cursor, pi, "
            "hermes, agents (other skill-aware agents), or all"
        ),
    )
    init.add_argument("--project-dir", help="Project directory to configure")
    init.add_argument(
        "--project",
        action="store_true",
        help="Write the config into the project folder instead, to share it with a team",
    )
    init.add_argument(
        "--no-hooks",
        action="store_true",
        help="Skip the session hooks that open sessions and save durable facts automatically",
    )

    remember = sub.add_parser("remember", help="Store a durable memory")
    remember.add_argument("content")
    remember.add_argument("--scope", choices=["user", "project", "session"], default="project")
    remember.add_argument("--lifecycle", choices=["semantic", "episodic", "procedural"], default="semantic")
    remember.add_argument("--kind", default="fact")
    remember.add_argument("--source", default="cli")
    remember.add_argument("--evidence", required=True)
    remember.add_argument("--confidence", type=float, default=0.8)
    remember.add_argument("--expires-at")
    remember.add_argument("--session-id")
    remember.add_argument("--semantic-model")

    recall = sub.add_parser("recall", help="Recall memory after a missing-context trigger")
    recall.add_argument("query")
    recall.add_argument("--trigger", required=True, choices=[
        "explicit_reference",
        "missing_context",
        "cross_session",
        "conflict",
        "memory_query",
    ])
    recall.add_argument("--evidence", required=True)
    recall.add_argument("--limit", type=int, default=3)
    recall.add_argument("--session-id")
    recall.add_argument("--semantic-model")

    route = sub.add_parser("route", help="Decide whether a task warrants memory retrieval")
    route.add_argument("task")

    session = sub.add_parser("session", help="Manage short-lived session memory")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_start = session_sub.add_parser("start")
    session_start.add_argument("--title")
    session_start.add_argument("--client")
    session_start.add_argument("--ttl-seconds", type=float, default=24 * 60 * 60)
    session_end = session_sub.add_parser("end")
    session_end.add_argument("session_id")
    session_end.add_argument("--status", choices=["closed", "expired"], default="closed")
    session_sub.add_parser("expire")
    session_status = session_sub.add_parser("status")
    session_status.add_argument("session_id")

    distill = sub.add_parser("distill", help="Extract durable claims from a transcript")
    distill.add_argument("input", type=Path)
    distill.add_argument("--apply", action="store_true")
    distill.add_argument("--session-id")
    distill.add_argument("--ttl-hours", type=float, default=24.0)
    distill.add_argument("--source")
    distill.add_argument("--semantic-model")

    forget = sub.add_parser("forget", help="Mark a memory deleted")
    forget.add_argument("memory_id")

    consolidate = sub.add_parser("consolidate", help="Supersede exact duplicate memories")
    consolidate.add_argument("--session-id")

    audit = sub.add_parser("audit", help="Inspect retrieval audit events")
    audit.add_argument("--limit", type=int, default=50)

    enrich = sub.add_parser(
        "enrich",
        help="Index memories with facts, questions, and keywords from a local model",
    )
    enrich.add_argument("--model", help="MLX model id (default: LFM2.5 1.2B Instruct 4-bit)")
    enrich.add_argument("--limit", type=int, help="Maximum memories to enrich in this pass")
    enrich.add_argument(
        "--download",
        action="store_true",
        help="Download the model now and exit, so later runs work offline",
    )

    hook = sub.add_parser("hook", help="Run by coding tools when a session starts or ends")
    hook.add_argument("event", choices=["session-start", "session-end"])
    hook.add_argument("--client", required=True, help="Tool that fired the hook")
    hook.add_argument("--session-id", help="Tool session id (otherwise read from stdin JSON)")
    hook.add_argument("--cwd", help="Workspace directory (otherwise read from stdin JSON)")
    hook.add_argument("--transcript", help="Transcript file (otherwise read from stdin JSON)")
    hook.add_argument(
        "--foreground", action="store_true", help="Finish the work before returning"
    )

    sub.add_parser("doctor", help="Report local runtime and prompt overhead")
    sub.add_parser("mcp", help="Run the stdio MCP server")
    return parser


def _json_mode(raw: list[str]) -> tuple[list[str], bool]:
    enabled = "--json" in raw
    return [item for item in raw if item != "--json"], enabled


def _emit(payload: dict[str, Any], *, json_mode: bool, text: str | None = None) -> None:
    if json_mode:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(text if text is not None else json.dumps(payload, indent=2, sort_keys=True))


def _fail(code: str, message: str, *, json_mode: bool) -> int:
    payload = {"ok": False, "error": {"code": code, "message": message}}
    _emit(payload, json_mode=json_mode, text=message)
    return 2


def _settings(args: argparse.Namespace, *, project_dir: str | Path | None = None) -> Settings:
    return Settings.load(
        project_dir=project_dir or getattr(args, "project_dir", None),
        project_id=getattr(args, "project_id", None),
        home=getattr(args, "home", None),
        session_id=getattr(args, "session_id", None),
    )


def _recall_payload(
    database: Database,
    settings: Settings,
    *,
    query: str,
    trigger: str,
    evidence: str,
    limit: int,
    session_id: str | None,
    semantic: bool,
) -> dict[str, Any]:
    validate_recall(trigger, evidence)
    records = database.recall(
        query=query,
        project_id=settings.project_id,
        session_id=session_id or settings.session_id,
        limit=max(1, min(limit, 20)),
        semantic=semantic,
    )
    items, count, estimated = compact_recall_items(records)
    database.audit_recall(
        action="recall",
        trigger=trigger,
        reason=evidence,
        project_id=settings.project_id,
        session_id=session_id or settings.session_id,
        result_count=count,
        estimated_tokens=estimated,
        retrieval_performed=True,
    )
    return {
        "ok": True,
        "retrieval_performed": True,
        "trigger": trigger,
        "items": items,
        "count": count,
        "estimated_tokens": estimated,
    }


def _run_hook(args: argparse.Namespace) -> int:
    """Handle a session hook. Prints nothing on stdout and never fails the tool."""
    try:
        raw: dict[str, Any] = {}
        if not args.session_id and not sys.stdin.isatty():
            try:
                loaded = json.loads(sys.stdin.read() or "{}")
                raw = loaded if isinstance(loaded, dict) else {}
            except json.JSONDecodeError:
                raw = {}
        payload = normalize_payload(
            raw, {"cwd": args.cwd, "session_id": args.session_id, "transcript": args.transcript}
        )
        if args.event == "session-end" and not args.foreground:
            # Tools allow session-end hooks only a second or two; finish in the background.
            command = [sys.executable, "-m", "unimem"]
            if args.home:
                command += ["--home", args.home]
            command += ["hook", "session-end", "--client", args.client, "--foreground"]
            command += ["--cwd", str(payload["cwd"])]
            if payload["session_id"]:
                command += ["--session-id", payload["session_id"]]
            if payload["transcript"]:
                command += ["--transcript", payload["transcript"]]
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return 0
        settings = Settings.load(project_dir=payload["cwd"], home=args.home)
        handler = session_start if args.event == "session-start" else session_end
        result = handler(settings, client=args.client, payload=payload)
        _log_hook(settings, {"event": args.event, "client": args.client, **result})
    except Exception as error:  # noqa: BLE001 - a hook must never break the tool that ran it
        print(f"unimem hook: {error}", file=sys.stderr)
    return 0


def _log_hook(settings: Settings, record: dict[str, Any]) -> None:
    path = settings.home / "hooks.log"
    settings.ensure_home()
    if path.exists() and path.stat().st_size > 1_000_000:
        path.unlink()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": now_iso(), **record}) + "\n")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    raw, json_mode = _json_mode(raw)
    parser = _parser()
    try:
        args = parser.parse_args(raw)
    except SystemExit as error:
        return int(error.code if error.code is not None else 0)

    try:
        if args.command == "mcp":
            # A user-wide server is shared by every workspace; Claude Code names the
            # open project in CLAUDE_PROJECT_DIR, other clients start it in the workspace.
            settings = _settings(args, project_dir=os.environ.get("CLAUDE_PROJECT_DIR") or None)
            return serve_mcp(settings)

        if args.command == "hook":
            return _run_hook(args)

        project_dir = getattr(args, "project_dir", None)
        settings = _settings(args, project_dir=project_dir)
        if args.command == "route":
            result = classify_route(args.task).as_dict()
            _emit(
                result,
                json_mode=json_mode,
                text=f"recall={result['should_recall']} trigger={result['trigger'] or 'none'}",
            )
            return 0

        database = Database(settings)
        database.initialize()
        if not settings.session_id and args.command in {"remember", "recall", "distill"}:
            # Use the session a tool's hook opened for this project, if one is active.
            settings = replace(settings, session_id=database.hook_session_for(settings.project_id))
        semantic_model = getattr(args, "semantic_model", None)
        if semantic_model:
            database.enable_semantic(
                model_name=semantic_model,
                embed_on_write=args.command != "recall",
            )
            if args.command == "recall":
                database.embed_pending()

        if args.command == "init":
            result = install_integrations(
                settings,
                project_dir=settings.cwd,
                clients=(args.client,),
                mcp_tool_schema_bytes=tool_schema_bytes(),
                project=args.project,
                hooks=not args.no_hooks,
            )
            lines = [f"Connected {', '.join(result['clients'])}. Updated:"]
            lines += [f"  {path}" for path in result["files"]]
            if result["restart_required"]:
                lines.append(f"Restart {', '.join(result['restart_required'])} to load unimem.")
            if "codex" in result["session_hooks"]:
                lines.append("Codex asks you to review new hooks: run /hooks in Codex once and trust them.")
            if "hermes" in result["session_hooks"]:
                lines.append("Hermes asks for consent the first time each hook runs.")
            _emit(result, json_mode=json_mode, text="\n".join(lines))
            return 0

        if args.command == "remember":
            if args.expires_at:
                parse_iso(args.expires_at)
            memory, created = database.add_memory(
                content=args.content,
                scope=args.scope,
                lifecycle=args.lifecycle,
                kind=args.kind,
                source=args.source,
                evidence=args.evidence,
                confidence=args.confidence,
                project_id=settings.project_id if args.scope == "project" else None,
                session_id=(args.session_id or settings.session_id) if args.scope == "session" else None,
                expires_at=args.expires_at,
            )
            if semantic_model:
                database.embed_pending()
            result = {"ok": True, "created": created, **memory.public_dict()}
            _emit(result, json_mode=json_mode, text=f"{memory.id} {memory.scope}:{memory.lifecycle}")
            return 0

        if args.command == "recall":
            result = _recall_payload(
                database,
                settings,
                query=args.query,
                trigger=args.trigger,
                evidence=args.evidence,
                limit=args.limit,
                session_id=args.session_id,
                semantic=bool(semantic_model),
            )
            _emit(result, json_mode=json_mode, text=json.dumps(result["items"], indent=2))
            return 0

        if args.command == "session":
            if args.session_command == "start":
                session = database.start_session(
                    project_id=settings.project_id,
                    client=args.client,
                    title=args.title,
                    ttl_seconds=args.ttl_seconds,
                )
                result = {"ok": True, "session": session.as_dict()}
                _emit(result, json_mode=json_mode, text=session.id)
                return 0
            if args.session_command == "end":
                session = database.end_session(args.session_id, status=args.status)
                if not session:
                    return _fail("session_not_found", "No active session with that id", json_mode=json_mode)
                result = {"ok": True, "session": session.as_dict()}
                _emit(result, json_mode=json_mode, text=session.id)
                return 0
            if args.session_command == "expire":
                count = database.expire_sessions()
                result = {"ok": True, "expired": count}
                _emit(result, json_mode=json_mode, text=f"expired {count}")
                return 0
            session = database.get_session(args.session_id)
            if not session:
                return _fail("session_not_found", "No session with that id", json_mode=json_mode)
            result = {"ok": True, "session": session.as_dict()}
            _emit(result, json_mode=json_mode, text=session.id)
            return 0

        if args.command == "distill":
            messages = load_messages(args.input)
            result = distill_messages(
                settings=settings,
                database=database,
                messages=messages,
                source=args.source or f"distill:{args.input.name}",
                session_id=args.session_id or settings.session_id,
                session_ttl_hours=args.ttl_hours,
                apply=args.apply,
            )
            if semantic_model and args.apply:
                database.embed_pending()
            _emit(
                result,
                json_mode=json_mode,
                text=f"accepted {result['accepted_count']} rejected {result['rejected_count']}",
            )
            return 0

        if args.command == "forget":
            found = database.forget_memory(args.memory_id)
            result = {"ok": found, "id": args.memory_id}
            if not found:
                return _fail("memory_not_found", "No active memory with that id", json_mode=json_mode)
            _emit(result, json_mode=json_mode, text=args.memory_id)
            return 0

        if args.command == "consolidate":
            count = database.consolidate(
                project_id=settings.project_id,
                session_id=args.session_id or settings.session_id,
            )
            result = {"ok": True, "superseded": count}
            _emit(result, json_mode=json_mode, text=f"superseded {count}")
            return 0

        if args.command == "audit":
            result = {"ok": True, "events": database.list_audit(project_id=settings.project_id, limit=args.limit)}
            _emit(result, json_mode=json_mode, text=json.dumps(result["events"], indent=2))
            return 0

        if args.command == "enrich":
            from .enrich import DEFAULT_ENRICH_MODEL, EnrichUnavailableError, LocalEnricher

            try:
                enricher = LocalEnricher(args.model or DEFAULT_ENRICH_MODEL)
            except EnrichUnavailableError as error:
                return _fail("enrich_unavailable", str(error), json_mode=json_mode)
            if args.download:
                enricher.prepare()
                result = {"ok": True, "downloaded": True, "model": enricher.model_name}
                _emit(result, json_mode=json_mode, text=f"{enricher.model_name} is ready")
                return 0
            count = database.enrich_pending(enricher, limit=args.limit)
            result = {"ok": True, "enriched": count, "model": enricher.model_name}
            _emit(result, json_mode=json_mode, text=f"enriched {count} with {enricher.model_name}")
            return 0

        if args.command == "doctor":
            result = {
                **database.stats(),
                "resident_instruction_bytes": len(agents_section().encode("utf-8"))
                + len(skill_text().split("---", 2)[-1].encode("utf-8")),
                "mcp_tool_schema_bytes": tool_schema_bytes(),
                "enrich_runtime_installed": importlib.util.find_spec("mlx_lm") is not None,
            }
            _emit(result, json_mode=json_mode, text=json.dumps(result, indent=2))
            return 0

        return _fail("unknown_command", "Unknown command", json_mode=json_mode)
    except IntegrationError as error:
        return _fail("integration_failed", str(error), json_mode=json_mode)
    except (MemoryError, ValueError, OSError, json.JSONDecodeError) as error:
        return _fail("invalid_operation", str(error), json_mode=json_mode)


if __name__ == "__main__":
    raise SystemExit(main())
