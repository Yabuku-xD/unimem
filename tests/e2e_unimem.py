#!/usr/bin/env python3
"""End-to-end acceptance harness for the unimem memory layer.

Failure modes covered:
- generated prompt content contains memory dumps or conversation logs;
- routine tasks trigger memory retrieval;
- retrieval has no trigger/evidence gate;
- user, project, or session memories leak across scopes;
- session memories survive expiry or session closure;
- distillation stores greetings, code output, secrets, or transient hypotheses as durable truth;
- recall output grows without a hard item/token budget;
- MCP exposes a large tool catalog or drifts from the CLI contract;
- normal operation requires an external API, database service, or daemon.

Run:
    uv run python tests/e2e_unimem.py

The run writes artifacts/e2e-unimem.json and exits non-zero on the first failed
check after recording all checks it can complete.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ARTIFACT = ROOT / "artifacts" / "e2e-unimem.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Harness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="unimem-e2e-")
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.project_a = self.base / "project-a"
        self.project_b = self.base / "project-b"
        self.project_git = self.base / "project-git"
        self.home.mkdir(parents=True)
        self.project_a.mkdir()
        self.project_b.mkdir()
        self.project_git.mkdir()
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(SRC)
        self.env["UNIMEM_HOME"] = str(self.home)
        self.env.pop("UNIMEM_SESSION_ID", None)
        self.checks: list[dict[str, Any]] = []
        self.started = now_iso()

    def cleanup(self) -> None:
        self.temp.cleanup()

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        expect: int = 0,
        name: str | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Any]:
        command = [sys.executable, "-m", "unimem", *args]
        result = subprocess.run(
            command,
            cwd=str(cwd or self.project_a),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        parsed: Any = None
        if "--json" in args:
            try:
                parsed = json.loads(result.stdout)
            except json.JSONDecodeError:
                parsed = None
        if name:
            self.check(
                name,
                result.returncode == expect,
                {
                    "command": command,
                    "returncode": result.returncode,
                    "stdout": result.stdout[-2000:],
                    "stderr": result.stderr[-2000:],
                },
            )
        return result, parsed

    def check(self, name: str, ok: bool, details: Any) -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "details": details})
        return bool(ok)

    def json_or_fail(self, name: str, result: subprocess.CompletedProcess[str], payload: Any) -> Any:
        ok = result.returncode == 0 and isinstance(payload, dict)
        self.check(name, ok, {"stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]})
        return payload if ok else {}


def mcp_call(harness: Harness, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run one initialize/list/call sequence against the stdio MCP server."""
    command = [sys.executable, "-m", "unimem", "mcp"]
    process = subprocess.Popen(
        command,
        cwd=str(harness.project_a),
        env=harness.env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    def send(payload: dict[str, Any]) -> None:
        try:
            process.stdin.write(json.dumps(payload) + "\n")
            process.stdin.flush()
        except BrokenPipeError:
            return

    def read_response(request_id: int) -> dict[str, Any]:
        deadline = time.time() + 5
        while time.time() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == request_id:
                return message
        return {"error": {"message": "timed out waiting for MCP response"}}

    send(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "unimem-e2e", "version": "1"},
            },
        }
    )
    init_response = read_response(1)
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    list_response = read_response(2)
    send(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "unimem", "arguments": arguments},
        }
    )
    call_response = read_response(3)
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    stderr = ""
    if process.stderr:
        try:
            stderr = process.stderr.read()
        except OSError:
            stderr = ""
    return {
        "initialize": init_response,
        "tools": list_response,
        "call": call_response,
        "stderr": stderr,
    }


def main() -> int:
    harness = Harness()
    failure_modes = [
        "prompt_bloat",
        "routine_retrieval",
        "missing_trigger_gate",
        "scope_leakage",
        "session_expiry",
        "noise_and_secret_capture",
        "unbounded_recall",
        "mcp_contract_drift",
        "external_service_dependency",
    ]
    try:
        # 1. Install tiny, memory-free integration surfaces.
        init_a, init_payload = harness.run(
            ["init", "--client", "all", "--json"],
            cwd=harness.project_a,
            name="init_project_a",
        )
        init_payload = harness.json_or_fail("init_payload_project_a", init_a, init_payload)
        generated_files = [
            harness.project_a / "AGENTS.md",
            harness.project_a / ".agents/skills/unimem/SKILL.md",
            harness.project_a / ".claude/skills/unimem/SKILL.md",
            harness.project_a / ".cursor/skills/unimem/SKILL.md",
            harness.project_a / ".mcp.json",
            harness.project_a / ".cursor/mcp.json",
            harness.project_a / ".codex/config.toml",
        ]
        harness.check(
            "integration_files_exist",
            all(path.exists() for path in generated_files),
            [str(path) for path in generated_files],
        )
        generated_text = "\n".join(
            path.read_text(encoding="utf-8") for path in generated_files if path.exists()
        )
        harness.check(
            "generated_files_have_no_memory_dump",
            "Always use pnpm" not in generated_text
            and "SQLite is the project store" not in generated_text
            and "Session Snapshot" not in generated_text,
            {"bytes": len(generated_text.encode("utf-8"))},
        )
        harness.check(
            "resident_instruction_budget",
            int(init_payload.get("resident_instruction_bytes", 10_000)) <= 2_500,
            init_payload,
        )
        client_matrix = {
            "claude": {
                "expected": {".claude/skills/unimem/SKILL.md", ".mcp.json"},
                "forbidden": {".cursor/skills/unimem/SKILL.md", ".codex/config.toml"},
            },
            "cursor": {
                "expected": {".cursor/skills/unimem/SKILL.md", ".cursor/mcp.json"},
                "forbidden": {".claude/skills/unimem/SKILL.md", ".codex/config.toml"},
            },
            "codex": {
                "expected": {".agents/skills/unimem/SKILL.md", ".codex/config.toml"},
                "forbidden": {".claude/skills/unimem/SKILL.md", ".cursor/mcp.json"},
            },
            "terminal": {
                "expected": {".agents/skills/unimem/SKILL.md", "AGENTS.md"},
                "forbidden": {".mcp.json", ".cursor/mcp.json", ".codex/config.toml"},
            },
        }
        for client, expectations in client_matrix.items():
            client_dir = harness.base / f"client-{client}"
            client_dir.mkdir()
            client_result, client_payload = harness.run(
                ["init", "--client", client, "--json"],
                cwd=client_dir,
                name=f"init_client_{client}",
            )
            harness.json_or_fail(f"init_client_{client}_payload", client_result, client_payload)
            harness.check(
                f"client_{client}_writes_only_selected_surface",
                all((client_dir / path).exists() for path in expectations["expected"])
                and not any((client_dir / path).exists() for path in expectations["forbidden"]),
                {"expected": sorted(expectations["expected"]), "forbidden": sorted(expectations["forbidden"])},
            )

        # 2. Route is deterministic and does not retrieve for routine work.
        routine_result, routine_payload = harness.run(
            ["route", "Fix the typo in README and run the tests.", "--json"],
            name="route_routine",
        )
        routine_payload = harness.json_or_fail("route_routine_payload", routine_result, routine_payload)
        harness.check(
            "routine_route_has_no_recall",
            routine_payload.get("should_recall") is False
            and routine_payload.get("retrieval_performed") is False,
            routine_payload,
        )
        trigger_result, trigger_payload = harness.run(
            ["route", "What did we decide about authentication last time?", "--json"],
            name="route_trigger",
        )
        trigger_payload = harness.json_or_fail("route_trigger_payload", trigger_result, trigger_payload)
        harness.check(
            "explicit_reference_routes_to_recall",
            trigger_payload.get("should_recall") is True
            and trigger_payload.get("trigger") == "explicit_reference",
            trigger_payload,
        )
        routine_before_result, routine_before_payload = harness.run(
            ["route", "Before committing, run tests.", "--json"],
            name="route_before_is_routine",
        )
        routine_before_payload = harness.json_or_fail(
            "route_before_is_routine_payload", routine_before_result, routine_before_payload
        )
        harness.check(
            "routine_before_phrase_has_no_recall",
            routine_before_payload.get("should_recall") is False,
            routine_before_payload,
        )

        # 3. Store user, project, and session memories in separate scopes.
        user_result, user_payload = harness.run(
            [
                "remember",
                "Always use pnpm for package management.",
                "--scope",
                "user",
                "--lifecycle",
                "semantic",
                "--kind",
                "preference",
                "--source",
                "user-statement",
                "--evidence",
                "Explicit preference stated by the user.",
                "--json",
            ],
            name="remember_user",
        )
        user_payload = harness.json_or_fail("remember_user_payload", user_result, user_payload)
        project_a_result, project_a_payload = harness.run(
            [
                "remember",
                "SQLite is the project store because it needs no service.",
                "--scope",
                "project",
                "--lifecycle",
                "semantic",
                "--kind",
                "decision",
                "--source",
                "architecture-decision",
                "--evidence",
                "Project decision recorded in the acceptance scenario.",
                "--json",
            ],
            cwd=harness.project_a,
            name="remember_project_a",
        )
        project_a_payload = harness.json_or_fail(
            "remember_project_a_payload", project_a_result, project_a_payload
        )
        init_b, _ = harness.run(
            ["init", "--client", "agents", "--json"],
            cwd=harness.project_b,
            name="init_project_b",
        )
        project_b_result, project_b_payload = harness.run(
            [
                "remember",
                "Postgres is the project store for the second repository.",
                "--scope",
                "project",
                "--lifecycle",
                "semantic",
                "--kind",
                "decision",
                "--source",
                "architecture-decision",
                "--evidence",
                "Separate repository decision.",
                "--json",
            ],
            cwd=harness.project_b,
            name="remember_project_b",
        )
        project_b_payload = harness.json_or_fail(
            "remember_project_b_payload", project_b_result, project_b_payload
        )
        subprocess.run(["git", "init"], cwd=str(harness.project_git), check=True, capture_output=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "git@github.com:acme/unimem-fixture.git"],
            cwd=str(harness.project_git),
            check=True,
            capture_output=True,
        )
        git_identity_result, git_identity_payload = harness.run(
            ["doctor", "--json"],
            cwd=harness.project_git,
            name="git_project_identity",
        )
        git_identity_payload = harness.json_or_fail(
            "git_project_identity_payload", git_identity_result, git_identity_payload
        )
        harness.check(
            "ssh_git_remote_uses_stable_project_identity",
            git_identity_payload.get("project_id") == "git:github.com/acme/unimem-fixture",
            git_identity_payload,
        )
        session_result, session_payload = harness.run(
            ["session", "start", "--title", "acceptance", "--ttl-seconds", "3600", "--json"],
            name="session_start",
        )
        session_payload = harness.json_or_fail("session_start_payload", session_result, session_payload)
        session_id = session_payload.get("session", {}).get("id", "")
        harness.check("session_id_created", bool(session_id), session_payload)
        session_memory_result, session_memory_payload = harness.run(
            [
                "remember",
                "Temporary hypothesis: the retry bug may be caused by cache.",
                "--scope",
                "session",
                "--session-id",
                session_id,
                "--lifecycle",
                "episodic",
                "--kind",
                "hypothesis",
                "--source",
                "session-note",
                "--evidence",
                "Short-lived debugging hypothesis.",
                "--expires-at",
                "2000-01-01T00:00:00+00:00",
                "--json",
            ],
            name="remember_expired_session",
        )
        session_memory_payload = harness.json_or_fail(
            "remember_expired_session_payload", session_memory_result, session_memory_payload
        )

        recall_result, recall_payload = harness.run(
            [
                "recall",
                "package management",
                "--trigger",
                "explicit_reference",
                "--evidence",
                "User referenced a prior preference.",
                "--json",
            ],
            name="recall_scoped",
        )
        recall_payload = harness.json_or_fail("recall_scoped_payload", recall_result, recall_payload)
        recalled_text = json.dumps(recall_payload)
        harness.check(
            "user_memory_is_available_in_project_a",
            "Always use pnpm" in recalled_text,
            recall_payload,
        )
        project_scope_result, project_scope_payload = harness.run(
            [
                "recall",
                "project store",
                "--trigger",
                "missing_context",
                "--evidence",
                "Repository inspection did not identify the storage decision.",
                "--json",
            ],
            cwd=harness.project_a,
            name="recall_project_scope",
        )
        project_scope_payload = harness.json_or_fail(
            "recall_project_scope_payload", project_scope_result, project_scope_payload
        )
        project_scope_text = json.dumps(project_scope_payload)
        harness.check(
            "project_scope_isolated",
            "SQLite is the project store" in project_scope_text
            and "Postgres is the project store" not in project_scope_text,
            project_scope_payload,
        )
        project_b_scope_result, project_b_scope_payload = harness.run(
            [
                "recall",
                "project store",
                "--trigger",
                "missing_context",
                "--evidence",
                "Repository inspection did not identify the storage decision.",
                "--json",
            ],
            cwd=harness.project_b,
            name="recall_project_b_scope",
        )
        project_b_scope_payload = harness.json_or_fail(
            "recall_project_b_scope_payload", project_b_scope_result, project_b_scope_payload
        )
        project_b_scope_text = json.dumps(project_b_scope_payload)
        harness.check(
            "project_b_scope_isolated",
            "Postgres is the project store" in project_b_scope_text
            and "SQLite is the project store" not in project_b_scope_text,
            project_b_scope_payload,
        )
        expired_result, expired_payload = harness.run(
            [
                "recall",
                "retry bug cache",
                "--trigger",
                "missing_context",
                "--evidence",
                "The current debugging context lacks the prior hypothesis.",
                "--session-id",
                session_id,
                "--json",
            ],
            name="expired_session_hidden",
        )
        expired_payload = harness.json_or_fail("expired_session_hidden_payload", expired_result, expired_payload)
        harness.check(
            "expired_session_memory_hidden",
            "Temporary hypothesis" not in json.dumps(expired_payload),
            expired_payload,
        )
        closed_session_result, closed_session_payload = harness.run(
            ["session", "start", "--title", "closed-by-end", "--ttl-seconds", "3600", "--json"],
            name="closed_session_start",
        )
        closed_session_payload = harness.json_or_fail(
            "closed_session_start_payload", closed_session_result, closed_session_payload
        )
        closed_session_id = closed_session_payload.get("session", {}).get("id", "")
        harness.run(
            [
                "remember",
                "Temporary hypothesis that should disappear after session end.",
                "--scope",
                "session",
                "--session-id",
                closed_session_id,
                "--lifecycle",
                "episodic",
                "--kind",
                "hypothesis",
                "--source",
                "session-note",
                "--evidence",
                "Short-lived session claim.",
                "--json",
            ],
            name="closed_session_remember",
        )
        harness.run(
            ["session", "end", closed_session_id, "--json"],
            name="closed_session_end",
        )
        closed_recall_result, closed_recall_payload = harness.run(
            [
                "recall",
                "disappear after session end",
                "--trigger",
                "missing_context",
                "--evidence",
                "The current task needs the prior session hypothesis.",
                "--session-id",
                closed_session_id,
                "--json",
            ],
            name="closed_session_hidden",
        )
        closed_recall_payload = harness.json_or_fail(
            "closed_session_hidden_payload", closed_recall_result, closed_recall_payload
        )
        harness.check(
            "closed_session_memory_hidden",
            "Temporary hypothesis that should disappear" not in json.dumps(closed_recall_payload),
            closed_recall_payload,
        )

        # 4. A missing trigger/evidence cannot silently query memory.
        blocked_result, blocked_payload = harness.run(
            [
                "recall",
                "package management",
                "--trigger",
                "explicit_reference",
                "--evidence",
                "",
                "--json",
            ],
            expect=2,
            name="recall_requires_evidence",
        )
        harness.check(
            "recall_gate_rejects_empty_evidence",
            blocked_result.returncode == 2
            and isinstance(blocked_payload, dict)
            and blocked_payload.get("ok") is False,
            {"stdout": blocked_result.stdout, "stderr": blocked_result.stderr},
        )
        secret_write_result, secret_write_payload = harness.run(
            [
                "remember",
                "API_KEY=sk-this-must-not-be-stored",
                "--scope",
                "user",
                "--lifecycle",
                "semantic",
                "--kind",
                "fact",
                "--source",
                "manual",
                "--evidence",
                "Manual secret fixture.",
                "--json",
            ],
            expect=2,
            name="remember_rejects_secret",
        )
        harness.check(
            "manual_write_rejects_secret",
            secret_write_result.returncode == 2
            and isinstance(secret_write_payload, dict)
            and secret_write_payload.get("ok") is False
            and "sk-this-must-not-be-stored" not in secret_write_result.stdout,
            {"stdout": secret_write_result.stdout, "stderr": secret_write_result.stderr},
        )

        # 5. Conservative distillation stores durable claims and rejects noise/secrets/code.
        transcript = harness.base / "transcript.jsonl"
        messages = [
            {"role": "user", "content": "Hi there!"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "tool", "content": "npm ERR! code ELIFECYCLE"},
            {"role": "user", "content": "Always use pnpm for package management."},
            {
                "role": "user",
                "content": "For this repo, we decided to use SQLite for local memory because it needs no service.",
            },
            {
                "role": "user",
                "content": "Maybe the retry bug is caused by cache; test tomorrow.",
            },
            {"role": "user", "content": "Always failing during checkout."},
            {"role": "user", "content": "API_KEY=sk-this-must-not-be-stored"},
            {"role": "assistant", "content": "```python\nprint('transient output')\n```"},
        ]
        transcript.write_text(
            "\n".join(json.dumps(message) for message in messages) + "\n",
            encoding="utf-8",
        )
        distill_result, distill_payload = harness.run(
            [
                "distill",
                str(transcript),
                "--apply",
                "--session-id",
                session_id,
                "--ttl-hours",
                "1",
                "--json",
            ],
            name="distill_transcript",
        )
        distill_payload = harness.json_or_fail("distill_payload", distill_result, distill_payload)
        accepted_text = json.dumps(distill_payload.get("accepted", []))
        rejected_text = json.dumps(distill_payload.get("rejected", []))
        harness.check(
            "distillation_keeps_durable_claims",
            "Always use pnpm" in accepted_text
            and "SQLite for local memory" in accepted_text
            and "retry bug" in accepted_text,
            distill_payload,
        )
        harness.check(
            "distillation_rejects_noise_secret_and_code",
            "Hi there!" in rejected_text
            and "sk-this-must-not-be-stored" not in accepted_text
            and "print('transient output')" not in accepted_text
            and "npm ERR!" not in accepted_text
            and "Always failing during checkout." not in accepted_text,
            distill_payload,
        )
        invalid_ttl_result, invalid_ttl_payload = harness.run(
            [
                "distill",
                str(transcript),
                "--ttl-hours",
                "inf",
                "--json",
            ],
            expect=2,
            name="distill_rejects_nonfinite_ttl",
        )
        harness.check(
            "distill_rejects_nonfinite_ttl",
            invalid_ttl_result.returncode == 2
            and isinstance(invalid_ttl_payload, dict)
            and invalid_ttl_payload.get("ok") is False,
            {"stdout": invalid_ttl_result.stdout, "stderr": invalid_ttl_result.stderr},
        )

        # 6. Recall is bounded and audited.
        for index in range(10):
            harness.run(
                [
                    "remember",
                    f"Token cap test memory {index}: " + ("long durable claim " * 30),
                    "--scope",
                    "project",
                    "--lifecycle",
                    "semantic",
                    "--kind",
                    "fact",
                    "--source",
                    "load-test",
                    "--evidence",
                    "Synthetic bounded-output fixture.",
                    "--json",
                ],
                cwd=harness.project_a,
                name=f"seed_token_memory_{index}",
            )
        bounded_result, bounded_payload = harness.run(
            [
                "recall",
                "Token cap test memory",
                "--trigger",
                "missing_context",
                "--evidence",
                "The current task needs a stored load-test fact.",
                "--limit",
                "10",
                "--json",
            ],
            name="bounded_recall",
        )
        bounded_payload = harness.json_or_fail("bounded_recall_payload", bounded_result, bounded_payload)
        harness.check(
            "recall_output_is_bounded",
            len(bounded_payload.get("items", [])) <= 3
            and int(bounded_payload.get("estimated_tokens", 10_000)) <= 400
            and len(json.dumps(bounded_payload).encode("utf-8")) <= 6_000,
            bounded_payload,
        )
        audit_result, audit_payload = harness.run(["audit", "--json"], name="audit_retrieval")
        audit_payload = harness.json_or_fail("audit_payload", audit_result, audit_payload)
        recall_events = [
            event
            for event in audit_payload.get("events", [])
            if event.get("action") == "recall" and event.get("retrieval_performed") is True
        ]
        harness.check("retrieval_is_audited", len(recall_events) >= 3, audit_payload)

        # 7. MCP is one compact tool and obeys the same contract.
        mcp_result = mcp_call(
            harness,
            {
                "action": "recall",
                "query": "package management",
                "trigger": "explicit_reference",
                "evidence": "User referenced a prior preference.",
            },
        )
        tools = mcp_result.get("tools", {}).get("result", {}).get("tools", [])
        harness.check(
            "mcp_exposes_one_compact_tool",
            len(tools) == 1
            and tools[0].get("name") == "unimem"
            and len(json.dumps(tools).encode("utf-8")) <= 2_500,
            {"tool_count": len(tools), "schema_bytes": len(json.dumps(tools).encode("utf-8"))},
        )
        call_result = mcp_result.get("call", {}).get("result", {})
        call_text = json.dumps(call_result)
        harness.check(
            "mcp_recall_obeys_contract",
            "Always use pnpm" in call_text
            and "Session Snapshot" not in call_text
            and call_result.get("isError") is not True,
            mcp_result,
        )
        invalid_mcp = mcp_call(
            harness,
            {
                "action": "recall",
                "query": "package management",
                "trigger": "explicit_reference",
                "evidence": "",
            },
        )
        invalid_call = invalid_mcp.get("call", {}).get("result", {})
        harness.check(
            "mcp_recall_rejects_missing_evidence",
            invalid_call.get("isError") is True,
            invalid_mcp,
        )
        invalid_write_mcp = mcp_call(
            harness,
            {
                "action": "remember",
                "content": "Durable project claim without provenance.",
                "scope": "project",
                "lifecycle": "semantic",
                "kind": "fact",
            },
        )
        invalid_write_call = invalid_write_mcp.get("call", {}).get("result", {})
        harness.check(
            "mcp_write_requires_evidence",
            invalid_write_call.get("isError") is True,
            invalid_write_mcp,
        )

        # 8. The normal path has no external service or daemon requirement.
        doctor_result, doctor_payload = harness.run(["doctor", "--json"], name="doctor")
        doctor_payload = harness.json_or_fail("doctor_payload", doctor_result, doctor_payload)
        harness.check(
            "local_operation_contract",
            doctor_payload.get("external_api_calls") == 0
            and doctor_payload.get("daemon_required") is False
            and doctor_payload.get("fts5") is True,
            doctor_payload,
        )

        artifact = {
            "passed": all(check["ok"] for check in harness.checks),
            "command": "uv run python tests/e2e_unimem.py",
            "started_at": harness.started,
            "finished_at": now_iso(),
            "python": sys.version,
            "failure_modes_covered": failure_modes,
            "checks": harness.checks,
        }
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(json.dumps(artifact, indent=2) + "\n", encoding="utf-8")
        if not artifact["passed"]:
            for check in harness.checks:
                if not check["ok"]:
                    print(f"FAIL {check['name']}: {check['details']}", file=sys.stderr)
            return 1
        print(json.dumps({"passed": True, "artifact": str(ARTIFACT), "checks": len(harness.checks)}))
        return 0
    finally:
        harness.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
