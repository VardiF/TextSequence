"""Forward-only restore from an authenticated, HEAD-reachable revision snapshot."""
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from app.domain.models import ValidationError, project_from_dict, project_to_dict, validate_revision_id
from app.persistence.project_store import StaleRevisionError
from app.revision_diff_models import ProjectStateDiff
from app.restore_models import RestoreRevisionRequest, RestoreRevisionResult
from app.services.revision_diff import RevisionDiffIntegrityError, diff_projects, summarize_changes
from app.services.timeline import timeline_projection

if TYPE_CHECKING:
    from app.services.projects import ProjectService


class RestoreError(Exception):
    def __init__(self, code: str, message: str, *, current_revision: int | None = None,
                 current_revision_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.current_revision = current_revision
        self.current_revision_id = current_revision_id


def _canonical_state(project) -> dict[str, Any]:
    """Return full canonical state while excluding only revision identity."""
    data = project_to_dict(project)
    data.pop("revision", None)
    data.pop("revision_id", None)
    return data


class RestoreService:
    def __init__(self, projects: "ProjectService") -> None:
        self.projects = projects

    @staticmethod
    def _parse(value: RestoreRevisionRequest | dict[str, Any]) -> RestoreRevisionRequest:
        try:
            return value if isinstance(value, RestoreRevisionRequest) else RestoreRevisionRequest.model_validate(value)
        except PydanticValidationError as exc:
            raise RestoreError("INVALID_ARGUMENT", "Restore request is invalid") from exc

    @staticmethod
    def _invalid_identifier() -> RestoreError:
        return RestoreError("INVALID_ARGUMENT", "Invalid project or revision identifier")

    def restore(self, project_id: str, target_revision_id: str,
                value: RestoreRevisionRequest | dict[str, Any], *, origin: str,
                actor: dict[str, str]) -> RestoreRevisionResult:
        request = self._parse(value)
        try:
            self.projects.store.path_for(project_id)
            validate_revision_id(target_revision_id)
        except ValidationError as exc:
            raise self._invalid_identifier() from exc

        with self.projects._project_lock(project_id):
            try:
                history = self.projects.store.load_reachable_history(project_id)
            except FileNotFoundError as exc:
                raise RestoreError("PROJECT_NOT_FOUND", "Project does not exist") from exc
            except ValidationError as exc:
                raise RestoreError("INTEGRITY_ERROR", "Project integrity validation failed") from exc

            if history.loaded.source != "directory":
                raise RestoreError("HISTORY_UNAVAILABLE", "Revision history is unavailable for this project")

            current = history.loaded.project
            if (current.revision, current.revision_id) != (request.expected_revision, request.expected_revision_id):
                raise RestoreError("REVISION_CONFLICT", "Project revision is no longer the expected base",
                                    current_revision=current.revision, current_revision_id=current.revision_id)

            target_record = next((record for record in history.records
                                  if record.metadata.revision_id == target_revision_id), None)
            if target_record is None:
                raise RestoreError("REVISION_NOT_FOUND", "Revision does not exist")

            try:
                target = project_from_dict(target_record.snapshot)
                if target.id != current.id or target.timeline.id != current.timeline.id:
                    raise RevisionDiffIntegrityError("Restore target has an incompatible project or timeline identity")
                target.validate()
                candidate = deepcopy(target)
                if _canonical_state(candidate) == _canonical_state(current):
                    raise RestoreError("NO_CHANGES", "The requested restore would not change the project")
                candidate.revision = current.revision + 1
                candidate.revision_id = self.projects.store.revision_id_factory()
                candidate.validate()
                changes = diff_projects(current, candidate)
                diff = ProjectStateDiff(summary=summarize_changes(changes), changes=changes)
            except RestoreError:
                raise
            except (ValidationError, RevisionDiffIntegrityError) as exc:
                raise RestoreError("INTEGRITY_ERROR", "Project integrity validation failed") from exc

            try:
                committed = self.projects.store.commit(
                    history.loaded, candidate, current.revision_id, origin, actor,
                    "restore", f"Restore project state from revision {target_record.metadata.revision_number}",
                    current.revision, restored_from_revision_id=target_record.metadata.revision_id,
                )
            except StaleRevisionError as exc:
                raise RestoreError("REVISION_CONFLICT", "Project revision is no longer the expected base",
                                    current_revision=exc.current_revision,
                                    current_revision_id=current.revision_id) from exc
            except ValidationError as exc:
                raise RestoreError("INTEGRITY_ERROR", "Project integrity validation failed") from exc
            except Exception as exc:
                raise RestoreError("PERSISTENCE_ERROR", "Restore could not be persisted") from exc

            return RestoreRevisionResult(
                status="restored", project_id=committed.id, revision=committed.revision,
                revision_id=committed.revision_id, parent_revision_id=current.revision_id,
                restored_from_revision_id=target_record.metadata.revision_id,
                diff=diff, timeline=timeline_projection(committed),
            )
