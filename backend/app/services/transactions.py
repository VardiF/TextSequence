"""Stateless transaction preparation and commit orchestration.

This module deliberately knows nothing about HTTP, MCP, or filesystem formats.
It loads through ProjectService and delegates every edit to the existing domain
operations, keeping preparation side-effect free and commit authoritative.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError as PydanticValidationError

from app.domain.models import (
    ExternalReference, Marker, MarkerProductionMetadata, TimelineConflictError,
    ValidationError, project_to_dict,
)
from app.domain.operations import add_marker, delete_clip, delete_marker, move_clip, split_clip, trim_clip, update_marker
from app.persistence.project_store import StaleRevisionError
from app.services.revision_diff import diff_projects, summarize_changes
from app.services.timeline import timeline_projection
from app.transaction_models import (
    AddMarkerOperation, CommitTransactionOutput, CommitTransactionRequest,
    DeleteClipOperation, DeleteMarkerOperation, EntityReference, IdReference,
    MarkerChanges, MarkerProduction, MoveOperation, OperationResult,
    PrepareTransactionOutput, PrepareTransactionRequest, PreparedOperation,
    PreparedTransaction, ProjectStateDiff, RawOperation, ResolvedAddMarkerOperation,
    ResolvedDeleteClipOperation, ResolvedDeleteMarkerOperation, ResolvedMoveOperation,
    ResolvedSplitOperation, ResolvedTrimOperation, ResolvedUpdateMarkerOperation,
    ResultReference, SplitOperation, TrimOperation, UpdateMarkerOperation,
)

if TYPE_CHECKING:
    from app.services.projects import ProjectService
    from app.domain.models import Project


MAX_TRANSACTION_BYTES = 1024 * 1024
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")


class TransactionError(Exception):
    def __init__(self, code: str, message: str, *, operation_index: int | None = None,
                 operation: str | None = None, cause_code: str | None = None,
                 current_revision: int | None = None, current_revision_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.operation_index = operation_index
        self.operation = operation
        self.cause_code = cause_code
        self.current_revision = current_revision
        self.current_revision_id = current_revision_id


class TransactionRevisionConflict(TransactionError):
    def __init__(self, revision: int, revision_id: str) -> None:
        super().__init__("REVISION_CONFLICT", "Project revision is no longer the prepared base",
                         current_revision=revision, current_revision_id=revision_id)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def transaction_hash(prepared: PreparedTransaction) -> str:
    payload = prepared.model_dump(mode="json")
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _safe_id(value: str) -> bool:
    return bool(_SAFE_ID.fullmatch(value))


def _entity_ids(project: "Project") -> set[str]:
    return {
        project.id, project.timeline.id,
        *(asset.id for asset in project.assets),
        *(track.id for track in project.timeline.tracks),
        *(clip.id for track in project.timeline.tracks for clip in track.clips),
        *(marker.id for marker in project.timeline.markers),
    }


def _find_clip(project: "Project", clip_id: str):
    for track in project.timeline.tracks:
        for clip in track.clips:
            if clip.id == clip_id:
                return clip
    return None


def _find_marker(project: "Project", marker_id: str):
    return next((marker for marker in project.timeline.markers if marker.id == marker_id), None)


def _marker_production(value: MarkerProduction) -> MarkerProductionMetadata:
    return MarkerProductionMetadata(
        shot_ids=list(value.shot_ids),
        dialogue_line_ids=list(value.dialogue_line_ids),
        external_refs=[ExternalReference(item.system, item.id, item.kind) for item in value.external_refs],
    )


def _marker_changes(value: MarkerChanges) -> dict[str, Any]:
    result = value.model_dump(mode="python", exclude_unset=True)
    if "production" in result and result["production"] is not None:
        result["production"] = _marker_production(value.production)
    return result


def _operation_name(operation: RawOperation | PreparedOperation) -> str:
    return operation.op


def _resolve_ref(project: "Project", ref: EntityReference, expected: str,
                 bindings: dict[str, tuple[str, str]]) -> str:
    if isinstance(ref, ResultReference):
        binding = bindings.get(ref.ref)
        if binding is None:
            raise TransactionError("INVALID_TRANSACTION", "Transaction reference is unavailable", cause_code="UNKNOWN_RESULT_REF")
        if binding[0] != expected:
            raise TransactionError("INVALID_TRANSACTION", "Transaction reference has the wrong entity type", cause_code="WRONG_ENTITY_TYPE")
        return binding[1]
    if not _safe_id(ref.id):
        raise TransactionError("INVALID_TRANSACTION", "Entity reference is malformed", cause_code="MALFORMED_ID")
    found = _find_clip(project, ref.id) if expected == "clip" else _find_marker(project, ref.id)
    if found is None:
        cause = "CLIP_NOT_FOUND" if expected == "clip" else "MARKER_NOT_FOUND"
        raise TransactionError("INVALID_TRANSACTION", "Referenced entity does not exist", cause_code=cause)
    return ref.id


def _generated_id(project: "Project", raw: RawOperation, index: int, entity: str, used: set[str]) -> str:
    normalized = raw.model_dump(mode="json")
    for counter in range(1000):
        seed = _canonical({
            "domain": "textsequence.transaction.generated-id",
            "contract_version": 1,
            "project_id": project.id,
            "base_revision": project.revision,
            "base_revision_id": project.revision_id,
            "operation": normalized,
            "operation_index": index,
            "entity_type": entity,
            "collision_counter": counter,
        })
        candidate = f"{entity}_{hashlib.sha256(seed).hexdigest()[:32]}"
        if candidate not in used:
            return candidate
    raise TransactionError("INVALID_TRANSACTION", "Unable to allocate a deterministic entity ID", cause_code="ID_COLLISION")


def _resolve_operations(project: "Project", operations: list[RawOperation]) -> list[PreparedOperation]:
    bindings: dict[str, tuple[str, str]] = {}
    used = _entity_ids(project)
    resolved: list[PreparedOperation] = []
    for index, raw in enumerate(operations):
        try:
            if isinstance(raw, SplitOperation):
                if raw.result_ref in bindings:
                    raise TransactionError("INVALID_TRANSACTION", "Transaction result reference is duplicated", cause_code="DUPLICATE_RESULT_REF")
                clip_id = _resolve_ref(project, raw.clip, "clip", bindings)
                new_clip_id = _generated_id(project, raw, index, "clip", used)
                used.add(new_clip_id)
                bindings[raw.result_ref] = ("clip", new_clip_id)
                resolved.append(ResolvedSplitOperation(op=raw.op, clip_id=clip_id, timeline_frame=raw.timeline_frame,
                                                       new_clip_id=new_clip_id, result_ref=raw.result_ref))
            elif isinstance(raw, MoveOperation):
                resolved.append(ResolvedMoveOperation(op=raw.op, clip_id=_resolve_ref(project, raw.clip, "clip", bindings),
                                                      timeline_start_frame=raw.timeline_start_frame))
            elif isinstance(raw, TrimOperation):
                resolved.append(ResolvedTrimOperation(op=raw.op, clip_id=_resolve_ref(project, raw.clip, "clip", bindings),
                                                      source_in_frame=raw.source_in_frame, source_out_frame=raw.source_out_frame))
            elif isinstance(raw, DeleteClipOperation):
                resolved.append(ResolvedDeleteClipOperation(op=raw.op, clip_id=_resolve_ref(project, raw.clip, "clip", bindings)))
            elif isinstance(raw, AddMarkerOperation):
                if raw.result_ref in bindings:
                    raise TransactionError("INVALID_TRANSACTION", "Transaction result reference is duplicated", cause_code="DUPLICATE_RESULT_REF")
                marker_id = _generated_id(project, raw, index, "marker", used)
                used.add(marker_id)
                bindings[raw.result_ref] = ("marker", marker_id)
                resolved.append(ResolvedAddMarkerOperation(
                    op=raw.op, result_ref=raw.result_ref, marker_id=marker_id,
                    start_frame=raw.start_frame, end_frame=raw.end_frame, name=raw.name,
                    description=raw.description, type=raw.type, production=raw.production,
                ))
            elif isinstance(raw, UpdateMarkerOperation):
                resolved.append(ResolvedUpdateMarkerOperation(op=raw.op,
                    marker_id=_resolve_ref(project, raw.marker, "marker", bindings), changes=raw.changes))
            elif isinstance(raw, DeleteMarkerOperation):
                resolved.append(ResolvedDeleteMarkerOperation(op=raw.op,
                    marker_id=_resolve_ref(project, raw.marker, "marker", bindings)))
        except TransactionError as exc:
            if exc.operation_index is None:
                raise TransactionError(exc.code, exc.message, operation_index=index, operation=raw.op,
                                       cause_code=exc.cause_code) from exc
            raise
        except (ValidationError, ValueError) as exc:
            raise TransactionError("INVALID_TRANSACTION", "Transaction operation is invalid", operation_index=index,
                                   operation=raw.op, cause_code="INVALID_OPERATION") from exc
    return resolved


def _failure(index: int, op: str, exc: Exception) -> TransactionError:
    if isinstance(exc, TimelineConflictError):
        cause = "TIMELINE_CONFLICT"
    elif isinstance(exc, ValidationError) and "Clip does not exist" in str(exc):
        cause = "CLIP_NOT_FOUND"
    elif isinstance(exc, ValidationError) and "Marker does not exist" in str(exc):
        cause = "MARKER_NOT_FOUND"
    elif isinstance(exc, ValidationError) and "no changes" in str(exc).lower():
        return TransactionError("NO_CHANGES", "The transaction operation would not change the project",
                                operation_index=index, operation=op, cause_code="NO_CHANGES")
    elif isinstance(exc, ValidationError):
        cause = "VALIDATION_ERROR"
    else:
        cause = "OPERATION_ERROR"
    return TransactionError("OPERATION_FAILED", "Transaction operation failed", operation_index=index,
                           operation=op, cause_code=cause)


def _execute(project: "Project", operations: list[PreparedOperation]) -> tuple["Project", list[OperationResult]]:
    candidate = project
    results: list[OperationResult] = []
    for index, operation in enumerate(operations):
        try:
            if isinstance(operation, ResolvedSplitOperation):
                candidate = split_clip(candidate, operation.clip_id, operation.timeline_frame, operation.new_clip_id)
                result = OperationResult(operation_index=index, op=operation.op,
                                         affected_ids=[operation.clip_id, operation.new_clip_id],
                                         created_ids=[operation.new_clip_id], result_ref=operation.result_ref)
            elif isinstance(operation, ResolvedMoveOperation):
                clip = _find_clip(candidate, operation.clip_id)
                if clip is None:
                    raise ValidationError("Clip does not exist")
                if clip.timeline_start_frame == operation.timeline_start_frame:
                    raise ValidationError("Move produced no changes")
                candidate = move_clip(candidate, operation.clip_id, operation.timeline_start_frame)
                result = OperationResult(operation_index=index, op=operation.op, affected_ids=[operation.clip_id])
            elif isinstance(operation, ResolvedTrimOperation):
                clip = _find_clip(candidate, operation.clip_id)
                if clip is None:
                    raise ValidationError("Clip does not exist")
                if ((operation.source_in_frame is None or operation.source_in_frame == clip.source_in_frame) and
                        (operation.source_out_frame is None or operation.source_out_frame == clip.source_out_frame)):
                    raise ValidationError("Trim produced no changes")
                candidate = trim_clip(candidate, operation.clip_id, operation.source_in_frame, operation.source_out_frame)
                result = OperationResult(operation_index=index, op=operation.op, affected_ids=[operation.clip_id])
            elif isinstance(operation, ResolvedDeleteClipOperation):
                candidate = delete_clip(candidate, operation.clip_id)
                result = OperationResult(operation_index=index, op=operation.op, affected_ids=[operation.clip_id])
            elif isinstance(operation, ResolvedAddMarkerOperation):
                marker = Marker(operation.marker_id, operation.start_frame, operation.end_frame, operation.name,
                                operation.description, operation.type, _marker_production(operation.production))
                candidate = add_marker(candidate, marker)
                result = OperationResult(operation_index=index, op=operation.op, affected_ids=[operation.marker_id],
                                         created_ids=[operation.marker_id], result_ref=operation.result_ref)
            elif isinstance(operation, ResolvedUpdateMarkerOperation):
                marker = _find_marker(candidate, operation.marker_id)
                if marker is None:
                    raise ValidationError("Marker does not exist")
                changes = _marker_changes(operation.changes)
                if not changes or all(getattr(marker, key) == value for key, value in changes.items()):
                    raise ValidationError("Marker update produced no changes")
                candidate = update_marker(candidate, operation.marker_id, changes)
                result = OperationResult(operation_index=index, op=operation.op, affected_ids=[operation.marker_id])
            elif isinstance(operation, ResolvedDeleteMarkerOperation):
                candidate = delete_marker(candidate, operation.marker_id)
                result = OperationResult(operation_index=index, op=operation.op, affected_ids=[operation.marker_id])
            else:
                raise ValidationError("Unsupported transaction operation")
            results.append(result)
        except TransactionError:
            raise
        except Exception as exc:
            raise _failure(index, _operation_name(operation), exc) from exc
    candidate.validate()
    return candidate, results


def _diff(before: "Project", after: "Project") -> ProjectStateDiff:
    changes = diff_projects(before, after)
    return ProjectStateDiff(summary=summarize_changes(changes), changes=changes)


def _request_size(value: Any) -> None:
    if len(_canonical(value.model_dump(mode="json"))) > MAX_TRANSACTION_BYTES:
        raise TransactionError("INVALID_TRANSACTION", "Transaction request exceeds the size limit", cause_code="REQUEST_TOO_LARGE")


class TransactionService:
    def __init__(self, projects: "ProjectService") -> None:
        self.projects = projects

    def _load(self, project_id: str):
        try:
            return self.projects.store.load_with_source(project_id)
        except FileNotFoundError:
            raise
        except ValidationError as exc:
            raise TransactionError("INTEGRITY_ERROR", "Project integrity validation failed") from exc

    def _parse_prepare(self, value: PrepareTransactionRequest | dict[str, Any]) -> PrepareTransactionRequest:
        try:
            request = value if isinstance(value, PrepareTransactionRequest) else PrepareTransactionRequest.model_validate(value)
            _request_size(request)
            return request
        except TransactionError:
            raise
        except PydanticValidationError as exc:
            raise TransactionError("INVALID_TRANSACTION", "Transaction request is invalid", cause_code="INVALID_REQUEST") from exc

    def _parse_commit(self, value: CommitTransactionRequest | dict[str, Any]) -> CommitTransactionRequest:
        try:
            request = value if isinstance(value, CommitTransactionRequest) else CommitTransactionRequest.model_validate(value)
            _request_size(request)
            if len(_canonical(request.prepared_transaction.model_dump(mode="json"))) > MAX_TRANSACTION_BYTES:
                raise TransactionError("INVALID_TRANSACTION", "Prepared transaction exceeds the size limit", cause_code="REQUEST_TOO_LARGE")
            return request
        except TransactionError:
            raise
        except PydanticValidationError as exc:
            raise TransactionError("INVALID_TRANSACTION", "Prepared transaction is invalid", cause_code="INVALID_REQUEST") from exc

    def prepare(self, project_id: str, value: PrepareTransactionRequest | dict[str, Any]) -> PrepareTransactionOutput:
        request = self._parse_prepare(value)
        with self.projects._project_lock(project_id):
            loaded = self._load(project_id)
            base = loaded.project
            if base.revision != request.expected_revision:
                raise TransactionRevisionConflict(base.revision, base.revision_id)
            resolved = _resolve_operations(base, request.operations)
            candidate, results = _execute(deepcopy(base), resolved)
            if project_to_dict(candidate) == project_to_dict(base):
                raise TransactionError("NO_CHANGES", "The transaction would not change the project", cause_code="NO_CHANGES")
            prepared = PreparedTransaction(contract_version=1, project_id=project_id,
                                           base_revision=base.revision, base_revision_id=base.revision_id,
                                           operations=resolved)
            return PrepareTransactionOutput(status="prepared", transaction_hash=transaction_hash(prepared),
                                            prepared_transaction=prepared, operation_results=results,
                                            diff=_diff(base, candidate))

    def commit(self, project_id: str, value: CommitTransactionRequest | dict[str, Any], *, origin: str, actor: dict[str, str]) -> CommitTransactionOutput:
        request = self._parse_commit(value)
        prepared = request.prepared_transaction
        if prepared.project_id != project_id:
            raise TransactionError("INVALID_TRANSACTION", "Transaction project does not match the request", cause_code="PROJECT_MISMATCH")
        if transaction_hash(prepared) != request.transaction_hash:
            raise TransactionError("INVALID_TRANSACTION", "Transaction hash does not match the prepared transaction", cause_code="HASH_MISMATCH")
        with self.projects._project_lock(project_id):
            loaded = self._load(project_id)
            base = loaded.project
            if base.revision != prepared.base_revision or base.revision_id != prepared.base_revision_id:
                raise TransactionRevisionConflict(base.revision, base.revision_id)
            candidate, results = _execute(deepcopy(base), prepared.operations)
            if project_to_dict(candidate) == project_to_dict(base):
                raise TransactionError("NO_CHANGES", "The transaction would not change the project", cause_code="NO_CHANGES")
            diff = _diff(base, candidate)
            candidate.revision = base.revision + 1
            candidate.revision_id = self.projects.store.revision_id_factory()
            try:
                committed = self.projects.store.commit(loaded, candidate, base.revision_id, origin, actor,
                                                        "transaction", f"Apply transaction ({len(prepared.operations)} operations)",
                                                        base.revision)
            except StaleRevisionError:
                raise TransactionRevisionConflict(base.revision, base.revision_id)
            except TransactionError:
                raise
            except Exception as exc:
                raise TransactionError("PERSISTENCE_ERROR", "Transaction could not be persisted") from exc
            return CommitTransactionOutput(
                status="committed", project_id=committed.id, revision=committed.revision,
                revision_id=committed.revision_id, parent_revision_id=base.revision_id,
                transaction_hash=request.transaction_hash, operation_results=results, diff=diff,
                timeline=timeline_projection(committed),
            )
