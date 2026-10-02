from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    scope: str
    project_id: str | None
    session_id: str | None
    lifecycle: str
    kind: str
    content: str
    source: str
    evidence: str
    confidence: float
    status: str
    created_at: str
    updated_at: str
    last_confirmed_at: str
    expires_at: str | None
    supersedes: str | None
    content_hash: str

    @classmethod
    def from_row(cls, row: Any) -> "MemoryRecord":
        return cls(
            id=row["id"],
            scope=row["scope"],
            project_id=row["project_id"],
            session_id=row["session_id"],
            lifecycle=row["lifecycle"],
            kind=row["kind"],
            content=row["content"],
            source=row["source"],
            evidence=row["evidence"],
            confidence=float(row["confidence"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_confirmed_at=row["last_confirmed_at"],
            expires_at=row["expires_at"],
            supersedes=row["supersedes"],
            content_hash=row["content_hash"],
        )

    def public_dict(self, *, include_evidence: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "scope": self.scope,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "lifecycle": self.lifecycle,
            "kind": self.kind,
            "content": self.content,
            "source": self.source,
            "confidence": self.confidence,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_confirmed_at": self.last_confirmed_at,
            "expires_at": self.expires_at,
            "supersedes": self.supersedes,
        }
        if include_evidence:
            data["evidence"] = self.evidence
        return data

    def recall_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "lifecycle": self.lifecycle,
            "kind": self.kind,
            "content": self.content,
            "source": self.source,
            "status": self.status,
        }


@dataclass(frozen=True)
class RouteDecision:
    should_recall: bool
    trigger: str | None
    reason: str
    retrieval_performed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "should_recall": self.should_recall,
            "trigger": self.trigger,
            "reason": self.reason,
            "retrieval_performed": self.retrieval_performed,
        }
