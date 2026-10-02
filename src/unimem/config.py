from __future__ import annotations

import hashlib
import math
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_after(seconds: float) -> str:
    if not math.isfinite(seconds):
        raise ValueError("duration must be finite")
    try:
        return datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() + seconds,
            tz=timezone.utc,
        ).isoformat()
    except (OverflowError, OSError) as error:
        raise ValueError("duration is outside the supported range") from error


def _git_output(cwd: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            capture_output=True,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def normalize_git_remote(remote: str) -> str:
    value = remote.strip()
    if not value:
        return ""
    if value.startswith("git@") and ":" in value:
        host, path = value[4:].split(":", 1)
        return f"{host}/{path}".removesuffix(".git").rstrip("/")
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        path = parsed.path.strip("/")
        return f"{host}/{path}".removesuffix(".git").rstrip("/")
    if ":" in value and "/" in value:
        host, path = value.split(":", 1)
        return f"{host}/{path}".removesuffix(".git").rstrip("/")
    return value.removesuffix(".git").rstrip("/")


def project_id_for(cwd: Path, explicit: str | None = None) -> str:
    if explicit:
        return explicit.strip()
    root_output = _git_output(cwd, ["rev-parse", "--show-toplevel"])
    root = Path(root_output).resolve() if root_output else cwd.resolve()
    remote = _git_output(root, ["remote", "get-url", "origin"])
    if remote:
        normalized = normalize_git_remote(remote)
        if normalized:
            return f"git:{normalized}"
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return f"path:{digest}"


@dataclass(frozen=True)
class Settings:
    home: Path
    db_path: Path
    cwd: Path
    project_id: str
    session_id: str | None

    @classmethod
    def load(
        cls,
        *,
        project_dir: str | Path | None = None,
        project_id: str | None = None,
        home: str | Path | None = None,
        session_id: str | None = None,
    ) -> "Settings":
        cwd = Path(project_dir or Path.cwd()).expanduser().resolve()
        resolved_home = Path(
            home or os.environ.get("UNIMEM_HOME") or (Path.home() / ".unimem")
        ).expanduser().resolve()
        db_override = os.environ.get("UNIMEM_DB")
        db_path = Path(db_override).expanduser().resolve() if db_override else resolved_home / "unimem.db"
        resolved_session = session_id or os.environ.get("UNIMEM_SESSION_ID") or None
        return cls(
            home=resolved_home,
            db_path=db_path,
            cwd=cwd,
            project_id=project_id_for(cwd, project_id),
            session_id=resolved_session,
        )

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
