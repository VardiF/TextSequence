from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from app.domain.models import Project, ValidationError, project_to_dict


def canonical_json(data: dict[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def snapshot_hash(project: Project | dict[str, Any]) -> str:
    data = project_to_dict(project) if isinstance(project, Project) else project
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RevisionMetadata:
    project_id: str
    revision_id: str
    revision_number: int
    parent_revision_id: Optional[str]
    created_at: str
    origin: str
    actor: dict[str, str]
    operation: str
    summary: str
    snapshot_sha256: str
    restored_from_revision_id: Optional[str] = None

    def validate(self, project: Project) -> None:
        if self.project_id != project.id:
            raise ValidationError("Revision metadata project ID does not match snapshot")
        if self.revision_id != project.revision_id:
            raise ValidationError("Revision metadata revision ID does not match snapshot")
        if self.revision_number != project.revision:
            raise ValidationError("Revision metadata revision number does not match snapshot")
        if self.origin not in {"rest", "mcp", "system"}:
            raise ValidationError("Invalid revision origin")
        if self.actor.get("type") not in {"human", "agent", "system", "unknown"}:
            raise ValidationError("Invalid revision actor type")
        if not self.operation or len(self.summary) > 240:
            raise ValidationError("Invalid revision audit metadata")
        if self.snapshot_sha256 != snapshot_hash(project):
            raise ValidationError("Revision snapshot hash does not match project")


@dataclass(frozen=True)
class RevisionRecord:
    metadata: RevisionMetadata
    snapshot: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"metadata": asdict(self.metadata), "snapshot": self.snapshot}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RevisionRecord":
        if set(data) != {"metadata", "snapshot"} or not isinstance(data["metadata"], dict) or not isinstance(data["snapshot"], dict):
            raise ValidationError("Invalid revision record shape")
        metadata = RevisionMetadata(**data["metadata"])
        from app.domain.models import project_from_dict
        project = project_from_dict(data["snapshot"])
        metadata.validate(project)
        return cls(metadata, data["snapshot"])


@dataclass(frozen=True)
class HeadPointer:
    project_id: str
    revision: int
    revision_id: str
    snapshot_sha256: str

    def validate(self, project: Project) -> None:
        if (self.project_id, self.revision, self.revision_id) != (project.id, project.revision, project.revision_id):
            raise ValidationError("HEAD does not identify the loaded project")
        if self.snapshot_sha256 != snapshot_hash(project):
            raise ValidationError("HEAD snapshot hash does not match project")


def new_revision_id() -> str:
    return f"revision_{uuid4().hex}"
