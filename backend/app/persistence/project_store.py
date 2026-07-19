from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional, Union

from app.domain.models import Project, ValidationError, project_from_dict, project_to_dict, validate_revision_id
from app.persistence.revisions import HeadPointer, RevisionMetadata, RevisionRecord, new_revision_id, revision_hash, snapshot_hash, utc_now


class StaleRevisionError(ValueError):
    def __init__(self, message: str, current_revision: Optional[int] = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


@dataclass(frozen=True)
class LoadedProject:
    project: Project
    source: str
    legacy_path: Optional[Path] = None
    legacy_bytes: Optional[bytes] = None


class ProjectStore:
    def __init__(self, root: Union[str, Path] = "projects", clock: Callable[[], str] = utc_now,
                 revision_id_factory: Callable[[], str] = new_revision_id) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.revision_id_factory = revision_id_factory

    def _validate_project_id(self, project_id: str) -> None:
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", project_id):
            raise ValidationError("Invalid project id")

    def path_for(self, project_id: str) -> Path:
        self._validate_project_id(project_id)
        return self.root / f"{project_id}.json"

    def directory_for(self, project_id: str) -> Path:
        self._validate_project_id(project_id)
        return self.root / project_id

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(data, indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _record_for(self, project: Project, parent_revision_id: Optional[str], origin: str, actor: dict[str, str], operation: str, summary: str) -> RevisionRecord:
        project.validate()
        metadata = RevisionMetadata(project_id=project.id, revision_id=project.revision_id,
                                    revision_number=project.revision, parent_revision_id=parent_revision_id,
                                    created_at=self.clock(), origin=origin, actor=dict(actor),
                                    operation=operation, summary=summary, snapshot_sha256="")
        metadata = replace(metadata, snapshot_sha256=revision_hash(metadata, project))
        metadata.validate(project)
        return RevisionRecord(metadata, project_to_dict(project))

    def _write_revision(self, directory: Path, record: RevisionRecord) -> None:
        path = directory / "revisions" / f"{record.metadata.revision_id}.json"
        if path.exists():
            raise ValidationError(f"Revision file already exists: {path.name}")
        self._write_json(path, record.to_dict())

    def _write_head(self, directory: Path, project: Project) -> None:
        pointer = HeadPointer(project.id, project.revision, project.revision_id, snapshot_hash(project))
        self._write_json(directory / "head.json", pointer.__dict__)

    @staticmethod
    def _load_revision_record(directory: Path, revision_id: str) -> RevisionRecord:
        validate_revision_id(revision_id)
        path = directory / "revisions" / f"{revision_id}.json"
        with path.open(encoding="utf-8") as handle:
            record = RevisionRecord.from_dict(json.load(handle))
        if record.metadata.revision_id != revision_id:
            raise ValidationError("Revision record ID does not match its path")
        return record

    def _validate_reachable_chain(self, directory: Path, head_record: RevisionRecord) -> None:
        """Validate the linear parent chain reachable from HEAD.

        The migration baseline is a legitimate root even when its revision
        number is greater than zero; ordinary revisions must form consecutive
        links down to that root.
        """
        current = head_record
        visited: set[str] = set()
        while True:
            metadata = current.metadata
            revision_id = metadata.revision_id
            if revision_id in visited:
                raise ValidationError("Revision parent chain contains a cycle")
            visited.add(revision_id)

            parent_id = metadata.parent_revision_id
            if parent_id is None:
                if metadata.revision_number != 0 and metadata.operation != "migration":
                    raise ValidationError("Non-migration root revision must be revision 0")
                return

            validate_revision_id(parent_id)
            if parent_id in visited:
                raise ValidationError("Revision parent chain contains a cycle")
            parent = self._load_revision_record(directory, parent_id)
            if parent.metadata.project_id != metadata.project_id:
                raise ValidationError("Revision parent belongs to another project")
            if parent.metadata.revision_number != metadata.revision_number - 1:
                raise ValidationError("Revision parent must be the immediately preceding revision")
            current = parent

    def _load_directory(self, project_id: str) -> Project:
        directory = self.directory_for(project_id)
        try:
            with (directory / "head.json").open(encoding="utf-8") as handle:
                head = HeadPointer(**json.load(handle))
            validate_revision_id(head.revision_id)
            record = self._load_revision_record(directory, head.revision_id)
            project = project_from_dict(record.snapshot)
            self._validate_reachable_chain(directory, record)
            if snapshot_hash(record.snapshot) != head.snapshot_sha256:
                raise ValidationError("HEAD and revision snapshot hashes differ")
            head.validate(project)
            return project
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise ValidationError(f"Invalid directory-backed project: {directory}: {exc}") from exc

    def load_with_source(self, project_id: str) -> LoadedProject:
        directory = self.directory_for(project_id)
        if directory.is_dir():
            return LoadedProject(self._load_directory(project_id), "directory")
        path = self.path_for(project_id)
        if not path.is_file():
            raise FileNotFoundError(f"Project does not exist: {project_id}")
        try:
            raw = path.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            project = project_from_dict(data)
            return LoadedProject(project, "legacy", path, raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValidationError) as exc:
            raise ValidationError(f"Invalid project file: {path}: {exc}") from exc

    def load(self, project_id: str) -> Project:
        return self.load_with_source(project_id).project

    def create_initial(self, project: Project, operation: str = "create_project", origin: str = "system", actor: Optional[dict[str, str]] = None) -> Project:
        if self.directory_for(project.id).exists() or self.path_for(project.id).exists():
            raise ValidationError(f"Project already exists: {project.id}")
        if project.revision != 0:
            raise ValidationError("New projects must start at revision 0")
        project.revision_id = project.revision_id or self.revision_id_factory()
        directory = self.directory_for(project.id)
        directory.mkdir(parents=True, exist_ok=False)
        try:
            record = self._record_for(project, None, origin, actor or {"type": "system"}, operation, "Project created")
            self._write_revision(directory, record)
            self._write_head(directory, project)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        return project

    def _install_legacy_baseline(self, loaded: LoadedProject) -> None:
        assert loaded.source == "legacy" and loaded.legacy_path and loaded.legacy_bytes is not None
        project = loaded.project
        directory = self.directory_for(project.id)
        temporary = Path(tempfile.mkdtemp(prefix=f".{project.id}.promote-", dir=self.root))
        try:
            record = self._record_for(project, None, "system", {"type": "system"}, "migration", "Legacy v1 compatibility baseline")
            self._write_revision(temporary, record)
            self._write_bytes(temporary / "legacy-v1.json", loaded.legacy_bytes)
            self._write_head(temporary, project)
            # The flat file remains untouched and recoverable. Directory install is a single rename
            # because no directory representation exists for a legacy project yet.
            os.replace(temporary, directory)
            temporary = Path()
        finally:
            if str(temporary) and temporary != Path() and temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def commit(self, loaded: LoadedProject, candidate: Project, parent_revision_id: Optional[str], origin: str,
               actor: dict[str, str], operation: str, summary: str, expected_revision: int) -> Project:
        if loaded.project.revision != expected_revision:
            raise StaleRevisionError("Project revision is stale", loaded.project.revision)
        if candidate.id != loaded.project.id or candidate.revision != expected_revision + 1:
            raise ValidationError("Committed project must advance revision by one")
        if parent_revision_id != loaded.project.revision_id:
            raise ValidationError("Committed project has an invalid parent revision")
        if candidate.revision_id == loaded.project.revision_id:
            raise ValidationError("Committed project needs a new revision_id")
        candidate.validate()
        record = self._record_for(candidate, parent_revision_id, origin, actor, operation, summary)
        directory = self.directory_for(candidate.id)
        if loaded.source == "legacy" and not directory.exists():
            self._install_legacy_baseline(loaded)
        if not directory.is_dir():
            raise ValidationError("Project directory is unavailable for commit")
        self._write_revision(directory, record)
        self._write_head(directory, candidate)
        return candidate

    def save(self, project: Project, expected_revision: Optional[int] = None) -> Project:
        """Compatibility helper for tests/tools; production mutations use ProjectService.commit."""
        try:
            loaded = self.load_with_source(project.id)
        except FileNotFoundError:
            return self.create_initial(project)
        if project.revision == loaded.project.revision and project.revision_id == loaded.project.revision_id:
            return project
        if expected_revision is not None and loaded.project.revision != expected_revision:
            raise StaleRevisionError("Project revision is stale", loaded.project.revision)
        if project.revision != loaded.project.revision + 1:
            raise ValidationError("Direct saves must advance revision by one")
        return self.commit(loaded, project, loaded.project.revision_id, "system", {"type": "system"}, "save", "Compatibility save", loaded.project.revision)

    def list(self) -> list[Project]:
        ids = {path.stem for path in self.root.glob("*.json") if path.is_file()}
        ids.update(path.name for path in self.root.iterdir() if path.is_dir() and (path / "head.json").is_file())
        return [self.load(project_id) for project_id in sorted(ids)]
