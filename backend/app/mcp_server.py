from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from app.application import application
from app.agent.context import EditorContextError
from app.audio.silence import SilenceAnalysisError
from app.domain.models import TimelineConflictError, ValidationError
from app.persistence.project_store import StaleRevisionError
from app.mcp_contracts import McpResult, ProjectSummaryOutput, QueryOutput
from app.mcp_resources import register_resources
from app.persistence.project_store import RevisionNotFoundError
from app.revision_diff_models import RevisionDiffErrorOutput, RevisionDiffResult
from app.services.revision_diff import RevisionDiffError
from app.services.transactions import TransactionError
from app.services.restore import RestoreError
from app.transaction_models import CommitTransactionOutput, PrepareTransactionOutput, TransactionErrorOutput
from app.restore_models import RestoreErrorOutput, RestoreRevisionResult
from app.guard_models import GuardError

mcp = FastMCP("TextSequence", instructions="Local-first TextSequence project collaboration.", streamable_http_path="/")


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, (TransactionError, RestoreError)): code = exc.code
    elif isinstance(exc, StaleRevisionError): code = "STALE_REVISION"
    elif isinstance(exc, TimelineConflictError): code = "TIMELINE_CONFLICT"
    elif isinstance(exc, RevisionNotFoundError): code = "REVISION_NOT_FOUND"
    elif isinstance(exc, RevisionDiffError): code = exc.code
    elif isinstance(exc, GuardError): code = exc.code
    elif isinstance(exc, FileNotFoundError): code = "PROJECT_NOT_FOUND"
    elif isinstance(exc, SilenceAnalysisError): code = exc.code
    elif isinstance(exc, ValidationError) and "Clip does not exist" in str(exc): code = "CLIP_NOT_FOUND"
    elif isinstance(exc, ValidationError) and "Marker does not exist" in str(exc): code = "MARKER_NOT_FOUND"
    elif isinstance(exc, ValidationError) and "Track does not exist" in str(exc): code = "TRACK_NOT_FOUND"
    elif isinstance(exc, ValidationError) and "no changes" in str(exc).lower(): code = "NO_CHANGES"
    else: code = "INVALID_ARGUMENT"
    messages = {
        "PROJECT_NOT_FOUND": "Project does not exist",
        "CLIP_NOT_FOUND": "Clip does not exist",
        "MARKER_NOT_FOUND": "Marker does not exist",
        "TRACK_NOT_FOUND": "Track does not exist",
        "STALE_REVISION": "Project revision is stale",
        "TIMELINE_CONFLICT": "Timeline operation conflicts with an existing clip",
        "NO_CHANGES": "The requested operation would not change the project",
        "REVISION_NOT_FOUND": "Revision does not exist",
        "HISTORY_UNAVAILABLE": "Revision history is unavailable for this project",
        "INVALID_ARGUMENT": "Invalid argument",
        "INVALID_TRANSACTION": "Invalid transaction",
        "OPERATION_FAILED": "Transaction operation failed",
        "NO_CHANGES": "The transaction would not change the project",
        "INTEGRITY_ERROR": "Project integrity validation failed",
        "PERSISTENCE_ERROR": "Transaction could not be persisted",
        "REVISION_CONFLICT": "Project revision is no longer the prepared base",
        "GUARD_CONFLICT": "This edit is protected by an active edit guard",
        "GUARD_NOT_FOUND": "Edit guard is not active",
        "GUARD_CAPABILITY_INVALID": "The edit guard capability is invalid",
        "INVALID_GUARD_SCOPE": "Guard scope is invalid",
        "INVALID_GUARD_TTL": "Guard TTL is invalid",
        "INVALID_GUARD_AUTHORIZATION": "Guard authorization is invalid",
        "GUARD_LIMIT_EXCEEDED": "The edit guard limit has been reached",
        "GUARD_STATE_ERROR": "Edit guard state could not be validated",
    }
    result = {"ok": False, "error": {"code": code, "message": messages.get(code, "Invalid argument")}}
    if isinstance(exc, StaleRevisionError) and exc.current_revision is not None:
        result["error"]["current_revision"] = exc.current_revision
    if isinstance(exc, (TransactionError, RestoreError)):
        for key in ("operation_index", "operation", "cause_code", "current_revision", "current_revision_id"):
            value = getattr(exc, key, None)
            if value is not None:
                result["error"][key] = value
        if getattr(exc, "conflicts", None):
            result["error"]["conflicts"] = exc.conflicts
    if isinstance(exc, GuardError) and exc.conflicts:
        result["error"]["conflicts"] = exc.conflicts
    return result


def _mutation(fn, *args):
    try:
        project = fn(*args)
        return {"ok": True, "project_id": project.id, "revision": project.revision,
                "revision_id": project.revision_id, "timeline_id": project.timeline.id,
                "timeline": application.projects.timeline(project.id)}
    except (Exception,) as exc:
        return _error(exc)


@mcp.tool(structured_output=True)
def list_projects() -> list[ProjectSummaryOutput]:
    return application.projects.list_summaries()


@mcp.tool(structured_output=True)
def get_timeline(project_id: str) -> McpResult:
    try: return application.projects.timeline(project_id)
    except Exception as exc: return _error(exc)


@mcp.tool(structured_output=True)
def get_editor_context(editor_session_id: str) -> McpResult:
    try:
        return {"ok": True, "context": application.editor_contexts.get(editor_session_id)}
    except EditorContextError as exc:
        return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}


@mcp.tool(structured_output=True)
def analyze_silence(project_id: str, minimum_silence_ms: int = 700, noise_threshold_db: float = -35) -> McpResult:
    try:
        return {"ok": True, **application.projects.analyze_silence(project_id, minimum_silence_ms, noise_threshold_db)}
    except Exception as exc: return _error(exc)


@mcp.tool(structured_output=True)
def remove_silence(project_id: str, expected_revision: int, minimum_silence_ms: int = 700,
                  noise_threshold_db: float = -35, keep_padding_ms: int = 0,
                  guard_tokens: list[str] | None = None) -> McpResult:
    try:
        result = application.projects.remove_silence(project_id, expected_revision, minimum_silence_ms,
                                                      noise_threshold_db, keep_padding_ms, origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens)
        return {key: value for key, value in result.items() if key != "project"}
    except Exception as exc: return _error(exc)


@mcp.tool(structured_output=True)
def split_clip(project_id: str, clip_id: str, timeline_frame: int, expected_revision: int, guard_tokens: list[str] | None = None) -> McpResult:
    return _mutation(lambda *args: application.projects.split(*args, origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens), project_id, clip_id, timeline_frame, expected_revision)


@mcp.tool(structured_output=True)
def delete_clip(project_id: str, clip_id: str, expected_revision: int, guard_tokens: list[str] | None = None) -> McpResult:
    return _mutation(lambda *args: application.projects.delete(*args, origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens), project_id, clip_id, expected_revision)


@mcp.tool(structured_output=True)
def move_clip(project_id: str, clip_id: str, expected_revision: int, destination: dict[str, Any], guard_tokens: list[str] | None = None) -> McpResult:
    try:
        kind = destination.get("kind")
        target_track_id = destination.get("target_track_id")
        if kind == "timeline_frame": result = application.projects.move(project_id, clip_id, int(destination["timeline_start_frame"]), expected_revision, target_track_id, origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens)
        elif kind == "gap" and destination.get("alignment") == "start": result = application.projects.move_to_gap(project_id, clip_id, int(destination["gap_ordinal"]), expected_revision, target_track_id, origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens)
        else: raise ValidationError("destination must be a timeline_frame or start-aligned gap")
        return {"ok": True, "project_id": result.id, "revision": result.revision, "revision_id": result.revision_id,
                "timeline_id": result.timeline.id, "timeline": application.projects.timeline(result.id)}
    except Exception as exc: return _error(exc)


@mcp.tool(structured_output=True)
def trim_clip(project_id: str, clip_id: str, expected_revision: int, edge: str, frames_to_remove: int, guard_tokens: list[str] | None = None) -> McpResult:
    return _mutation(lambda *args: application.projects.trim_relative(*args, origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens), project_id, clip_id, expected_revision, edge, frames_to_remove)


@mcp.tool(structured_output=True)
def add_marker(project_id: str, expected_revision: int, start_frame: int, name: str,
               end_frame: int | None = None, description: str = "", type: str = "generic",
               production: dict[str, Any] | None = None, guard_tokens: list[str] | None = None) -> McpResult:
    try:
        before = application.projects.get(project_id)
        result = application.projects.add_marker(project_id, expected_revision, start_frame, end_frame, name,
                                                  description, type, production, origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens)
        prior_ids = {marker.id for marker in before.timeline.markers}
        marker_id = next(marker.id for marker in result.timeline.markers if marker.id not in prior_ids)
        response = _mutation(lambda: result)
        response["marker_id"] = marker_id
        response["marker"] = next(marker for marker in application.projects.timeline(result.id)["markers"] if marker["id"] == marker_id)
        return response
    except Exception as exc:
        return _error(exc)


@mcp.tool(structured_output=True)
def update_marker(project_id: str, marker_id: str, expected_revision: int, changes: dict[str, Any], guard_tokens: list[str] | None = None) -> McpResult:
    try:
        result = application.projects.update_marker(project_id, expected_revision, marker_id, changes,
                                                     origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens)
        response = _mutation(lambda: result)
        response["marker_id"] = marker_id
        response["marker"] = next(marker for marker in application.projects.timeline(result.id)["markers"] if marker["id"] == marker_id)
        return response
    except Exception as exc:
        return _error(exc)


@mcp.tool(structured_output=True)
def delete_marker(project_id: str, marker_id: str, expected_revision: int, guard_tokens: list[str] | None = None) -> McpResult:
    try:
        result = application.projects.delete_marker(project_id, expected_revision, marker_id,
                                                     origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens)
        response = _mutation(lambda: result)
        response["deleted_marker_id"] = marker_id
        return response
    except Exception as exc:
        return _error(exc)


@mcp.tool(structured_output=True)
def add_track(project_id: str, name: str, expected_revision: int, position: int | None = None,
              external_refs: list[dict[str, Any]] | None = None, guard_tokens: list[str] | None = None) -> McpResult:
    try:
        before = application.projects.get(project_id)
        result = application.projects.add_track(project_id, expected_revision, name, position, external_refs or [],
                                                 origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens)
        created = next(track for track in result.timeline.tracks if track.id not in {item.id for item in before.timeline.tracks})
        response = _mutation(lambda: result)
        response["track_id"] = created.id
        return response
    except Exception as exc:
        return _error(exc)


@mcp.tool(structured_output=True)
def update_track(project_id: str, track_id: str, expected_revision: int, name: str | None = None,
                 external_refs: list[dict[str, Any]] | None = None, guard_tokens: list[str] | None = None) -> McpResult:
    return _mutation(lambda: application.projects.update_track(project_id, track_id, expected_revision, name, external_refs,
                                                                 origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens))


@mcp.tool(structured_output=True)
def delete_track(project_id: str, track_id: str, expected_revision: int, guard_tokens: list[str] | None = None) -> McpResult:
    return _mutation(lambda: application.projects.delete_track(project_id, track_id, expected_revision,
                                                                 origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens))


@mcp.tool(structured_output=True)
def reorder_track(project_id: str, track_id: str, position: int, expected_revision: int,
                  guard_tokens: list[str] | None = None) -> McpResult:
    return _mutation(lambda: application.projects.reorder_track(project_id, track_id, expected_revision, position,
                                                                  origin="mcp", actor={"type": "agent"}, guard_tokens=guard_tokens))


def _render(fn, project_id: str, expected_revision: int) -> dict[str, Any]:
    try:
        result = fn(project_id, expected_revision)
        return {"ok": True, "project_id": project_id, "revision": result.revision,
                "render_type": result.render_type, "duration_frames": result.duration_frames,
                "url": f"/api/projects/{project_id}/renders/{result.render_type}/{result.revision}"}
    except Exception as exc: return _error(exc)


@mcp.tool(structured_output=True)
def render_preview(project_id: str, expected_revision: int) -> McpResult:
    return _render(application.projects.render_preview, project_id, expected_revision)


@mcp.tool(structured_output=True)
def export_project(project_id: str, expected_revision: int) -> McpResult:
    return _render(application.projects.export_project, project_id, expected_revision)


@mcp.tool(structured_output=True)
def query_timeline(project_id: str, query: dict[str, Any]) -> QueryOutput | McpResult:
    try:
        return application.projects.query_timeline(project_id, query)
    except Exception as exc:
        return _error(exc)


@mcp.tool(structured_output=True)
def diff_revisions(project_id: str, from_revision_id: str, to_revision_id: str) -> RevisionDiffResult | RevisionDiffErrorOutput:
    try:
        return application.projects.diff_revisions(project_id, from_revision_id, to_revision_id).model_dump(mode="json")
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    structured_output=True,
)
def prepare_transaction(project_id: str, expected_revision: int, operations: list[dict[str, Any]]) -> PrepareTransactionOutput | TransactionErrorOutput:
    try:
        return application.projects.prepare_transaction(
            project_id, {"expected_revision": expected_revision, "operations": operations}
        ).model_dump(mode="json")
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    structured_output=True,
)
def commit_transaction(project_id: str, transaction_hash: str, prepared_transaction: dict[str, Any], guard_tokens: list[str] | None = None) -> CommitTransactionOutput | TransactionErrorOutput:
    try:
        return application.projects.commit_transaction(
            project_id,
            {"transaction_hash": transaction_hash, "prepared_transaction": prepared_transaction, "guard_tokens": guard_tokens or []},
            origin="mcp", actor={"type": "agent"},
        ).model_dump(mode="json")
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False),
    structured_output=True,
)
def restore_revision(project_id: str, target_revision_id: str, expected_revision: int,
                     expected_revision_id: str, guard_tokens: list[str] | None = None) -> RestoreRevisionResult | RestoreErrorOutput:
    try:
        result = application.projects.restore_revision(
            project_id, target_revision_id,
            {"expected_revision": expected_revision, "expected_revision_id": expected_revision_id, "guard_tokens": guard_tokens or []},
            origin="mcp", actor={"type": "agent"},
        )
        return result.model_dump(mode="json")
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    structured_output=True,
)
def acquire_edit_guard(project_id: str, owner: dict[str, Any], scope: dict[str, Any],
                       ttl_seconds: int | None = None, purpose: str | None = None) -> McpResult:
    try:
        return application.projects.guards.acquire(project_id, owner, scope, ttl_seconds, purpose)
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
    structured_output=True,
)
def renew_edit_guard(project_id: str, guard_id: str, guard_token: str,
                     ttl_seconds: int | None = None) -> McpResult:
    try:
        return application.projects.guards.renew(project_id, guard_id, guard_token, ttl_seconds)
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True),
    structured_output=True,
)
def release_edit_guard(project_id: str, guard_id: str, guard_token: str) -> McpResult:
    try:
        return application.projects.guards.release(project_id, guard_id, guard_token)
    except Exception as exc:
        return _error(exc)


@mcp.tool(
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True),
    structured_output=True,
)
def list_edit_guards(project_id: str) -> McpResult:
    try:
        return application.projects.guards.list(project_id)
    except Exception as exc:
        return _error(exc)


register_resources(mcp)
