from __future__ import annotations

import json
import os
import tempfile
import re
from pathlib import Path
from typing import Optional, Union

from app.domain.models import Project, ValidationError, project_from_dict, project_to_dict


class StaleRevisionError(ValueError):
    def __init__(self, message: str, current_revision: Optional[int] = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class ProjectStore:
    def __init__(self, root: Union[str, Path] = "projects") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, project_id: str) -> Path:
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", project_id):
            raise ValidationError("Invalid project id")
        return self.root / f"{project_id}.json"

    def save(self, project: Project, expected_revision: Optional[int] = None) -> Project:
        project.validate()
        path = self.path_for(project.id)
        if expected_revision is not None and path.exists():
            current = self.load(project.id)
            if current.revision != expected_revision:
                raise StaleRevisionError("Project revision is stale", current.revision)
        data = json.dumps(project_to_dict(project), indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{project.id}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return project

    def load(self, project_id: str) -> Project:
        path = self.path_for(project_id)
        if not path.is_file():
            raise FileNotFoundError(f"Project does not exist: {project_id}")
        try:
            with path.open(encoding="utf-8") as handle:
                return project_from_dict(json.load(handle))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise ValidationError(f"Invalid project file: {path}") from exc

    def list(self) -> list[Project]:
        return [self.load(path.stem) for path in sorted(self.root.glob("*.json"))]
