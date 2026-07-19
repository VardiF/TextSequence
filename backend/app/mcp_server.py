from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from app.application import application
from app.agent.context import EditorContextError
from app.audio.silence import SilenceAnalysisError
from app.domain.models import TimelineConflictError, ValidationError
from app.persistence.project_store import StaleRevisionError
from app.mcp_contracts import McpResult, ProjectSummaryOutput, QueryOutput
from app.mcp_resources import register_resources

mcp = FastMCP("TextSequence", instructions="Local-first TextSequence project collaboration.", streamable_http_path="/")


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, StaleRevisionError): code = "STALE_REVISION"
    elif isinstance(exc, TimelineConflictError): code = "TIMELINE_CONFLICT"
    elif isinstance(exc, FileNotFoundError): code = "PROJECT_NOT_FOUND"
    elif isinstance(exc, SilenceAnalysisError): code = exc.code
    elif isinstance(exc, ValidationError) and "Clip does not exist" in str(exc): code = "CLIP_NOT_FOUND"
    elif isinstance(exc, ValidationError) and "Marker does not exist" in str(exc): code = "MARKER_NOT_FOUND"
    elif isinstance(exc, ValidationError) and "no changes" in str(exc).lower(): code = "NO_CHANGES"
    else: code = "INVALID_ARGUMENT"
    messages = {
        "PROJECT_NOT_FOUND": "Project does not exist",
        "CLIP_NOT_FOUND": "Clip does not exist",
        "MARKER_NOT_FOUND": "Marker does not exist",
        "STALE_REVISION": "Project revision is stale",
        "TIMELINE_CONFLICT": "Timeline operation conflicts with an existing clip",
        "NO_CHANGES": "The requested operation would not change the project",
    }
    result = {"ok": False, "error": {"code": code, "message": messages.get(code, "Invalid argument")}}
    if isinstance(exc, StaleRevisionError) and exc.current_revision is not None:
        result["error"]["current_revision"] = exc.current_revision
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
                  noise_threshold_db: float = -35, keep_padding_ms: int = 0) -> McpResult:
    try:
        result = application.projects.remove_silence(project_id, expected_revision, minimum_silence_ms,
                                                      noise_threshold_db, keep_padding_ms, origin="mcp", actor={"type": "agent"})
        return {key: value for key, value in result.items() if key != "project"}
    except Exception as exc: return _error(exc)


@mcp.tool(structured_output=True)
def split_clip(project_id: str, clip_id: str, timeline_frame: int, expected_revision: int) -> McpResult:
    return _mutation(lambda *args: application.projects.split(*args, origin="mcp", actor={"type": "agent"}), project_id, clip_id, timeline_frame, expected_revision)


@mcp.tool(structured_output=True)
def delete_clip(project_id: str, clip_id: str, expected_revision: int) -> McpResult:
    return _mutation(lambda *args: application.projects.delete(*args, origin="mcp", actor={"type": "agent"}), project_id, clip_id, expected_revision)


@mcp.tool(structured_output=True)
def move_clip(project_id: str, clip_id: str, expected_revision: int, destination: dict[str, Any]) -> McpResult:
    try:
        kind = destination.get("kind")
        if kind == "timeline_frame": result = application.projects.move(project_id, clip_id, int(destination["timeline_start_frame"]), expected_revision, origin="mcp", actor={"type": "agent"})
        elif kind == "gap" and destination.get("alignment") == "start": result = application.projects.move_to_gap(project_id, clip_id, int(destination["gap_ordinal"]), expected_revision, origin="mcp", actor={"type": "agent"})
        else: raise ValidationError("destination must be a timeline_frame or start-aligned gap")
        return {"ok": True, "project_id": result.id, "revision": result.revision, "revision_id": result.revision_id,
                "timeline_id": result.timeline.id, "timeline": application.projects.timeline(result.id)}
    except Exception as exc: return _error(exc)


@mcp.tool(structured_output=True)
def trim_clip(project_id: str, clip_id: str, expected_revision: int, edge: str, frames_to_remove: int) -> McpResult:
    return _mutation(lambda *args: application.projects.trim_relative(*args, origin="mcp", actor={"type": "agent"}), project_id, clip_id, expected_revision, edge, frames_to_remove)


@mcp.tool(structured_output=True)
def add_marker(project_id: str, expected_revision: int, start_frame: int, name: str,
               end_frame: int | None = None, description: str = "", type: str = "generic",
               production: dict[str, Any] | None = None) -> McpResult:
    try:
        before = application.projects.get(project_id)
        result = application.projects.add_marker(project_id, expected_revision, start_frame, end_frame, name,
                                                  description, type, production, origin="mcp", actor={"type": "agent"})
        prior_ids = {marker.id for marker in before.timeline.markers}
        marker_id = next(marker.id for marker in result.timeline.markers if marker.id not in prior_ids)
        response = _mutation(lambda: result)
        response["marker_id"] = marker_id
        response["marker"] = next(marker for marker in application.projects.timeline(result.id)["markers"] if marker["id"] == marker_id)
        return response
    except Exception as exc:
        return _error(exc)


@mcp.tool(structured_output=True)
def update_marker(project_id: str, marker_id: str, expected_revision: int, changes: dict[str, Any]) -> McpResult:
    try:
        result = application.projects.update_marker(project_id, expected_revision, marker_id, changes,
                                                     origin="mcp", actor={"type": "agent"})
        response = _mutation(lambda: result)
        response["marker_id"] = marker_id
        response["marker"] = next(marker for marker in application.projects.timeline(result.id)["markers"] if marker["id"] == marker_id)
        return response
    except Exception as exc:
        return _error(exc)


@mcp.tool(structured_output=True)
def delete_marker(project_id: str, marker_id: str, expected_revision: int) -> McpResult:
    try:
        result = application.projects.delete_marker(project_id, expected_revision, marker_id,
                                                     origin="mcp", actor={"type": "agent"})
        response = _mutation(lambda: result)
        response["deleted_marker_id"] = marker_id
        return response
    except Exception as exc:
        return _error(exc)


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


register_resources(mcp)
