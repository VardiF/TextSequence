from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.domain.models import ValidationError, project_to_dict
from app.application import application
from app.agent.context import EditorContextError
from app.agent.runtime import AgentConfigurationError, AgentRuntime
from app.audio.silence import SilenceAnalysisError
from app.media.probe import ProbeError, find_ffprobe
from app.persistence.project_store import StaleRevisionError
from app.persistence.project_store import RevisionNotFoundError
from app.services.projections import revision_metadata_projection, revision_projection
from app.services.query import QueryValidationError
from app.services.revision_diff import RevisionDiffError
from app.services.transactions import TransactionError
from app.services.restore import RestoreError
from app.restore_models import RestoreRevisionRequest
from app.guard_models import GuardError
from copy import deepcopy

router = APIRouter(prefix="/api")
service = application.projects
agent_runtime = AgentRuntime()


def _rest_project(project):
    data = project_to_dict(project)
    # v1 clients may still read top-level tracks; persistence and the frontend use
    # the canonical v2 timeline object.
    data["tracks"] = deepcopy(data["timeline"]["tracks"])
    data["timeline_id"] = data["timeline"]["id"]
    return data


def _rest_project_read_error(exc: Exception) -> HTTPException:
    """Map storage read failures without exposing local persistence details."""
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"})
    if isinstance(exc, ValidationError):
        return HTTPException(500, {"code": "INTEGRITY_ERROR", "message": "Project integrity validation failed"})
    return HTTPException(500, {"code": "INTEGRITY_ERROR", "message": "Project integrity validation failed"})


class CreateProject(BaseModel):
    name: str = "Untitled project"


class ImportMedia(BaseModel):
    path: str
    guard_tokens: list[StrictStr] = Field(default_factory=list)


class ClipMutation(BaseModel):
    clip_id: str
    expected_revision: int
    guard_tokens: list[StrictStr] = Field(default_factory=list)


class SplitMutation(ClipMutation):
    timeline_frame: int


class MoveMutation(ClipMutation):
    timeline_start_frame: int


class TrimMutation(ClipMutation):
    source_in_frame: Optional[int] = None
    source_out_frame: Optional[int] = None


class MarkerProductionPayload(BaseModel):
    shot_ids: list[str] = []
    dialogue_line_ids: list[str] = []
    external_refs: list[dict] = []


class AddMarkerMutation(BaseModel):
    expected_revision: int
    start_frame: int
    end_frame: Optional[int] = None
    name: str
    description: str = ""
    type: str = "generic"
    production: MarkerProductionPayload = MarkerProductionPayload()
    guard_tokens: list[StrictStr] = Field(default_factory=list)


class UpdateMarkerMutation(BaseModel):
    marker_id: str
    expected_revision: int
    changes: dict
    guard_tokens: list[StrictStr] = Field(default_factory=list)


class DeleteMarkerMutation(BaseModel):
    marker_id: str
    expected_revision: int
    guard_tokens: list[StrictStr] = Field(default_factory=list)


class RenderRequest(BaseModel):
    expected_revision: int


class EditorContextSnapshot(BaseModel):
    editor_session_id: str
    project_id: str
    observed_revision: int
    selected_clip_id: Optional[str] = None
    selected_marker_id: Optional[str] = None
    playhead_frame: int = 0
    visible_track_id: Optional[str] = None


class AgentChatRequest(BaseModel):
    editor_session_id: str
    message: str
    editor_context: EditorContextSnapshot


class SilenceAnalysisRequest(BaseModel):
    minimum_silence_ms: int = 700
    noise_threshold_db: float = -35


class SilenceRemovalRequest(SilenceAnalysisRequest):
    expected_revision: int
    keep_padding_ms: int = 0
    guard_tokens: list[StrictStr] = Field(default_factory=list)


class AcquireGuardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: dict
    scope: dict
    ttl_seconds: int | None = None
    purpose: str | None = None


class RenewGuardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guard_token: str
    ttl_seconds: int | None = None


class ReleaseGuardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    guard_token: str


@router.get("/health")
def health():
    ffprobe = find_ffprobe()
    return {
        "status": "ok",
        "ffprobe": {"available": bool(ffprobe), "path": ffprobe},
        "mcp": {"status": "running", "endpoint": "http://127.0.0.1:8000/mcp", "transport": "Streamable HTTP", "tool_count": 23, "resource_count": 8},
        "built_in_assistant": {"configured": agent_runtime.configured()},
    }


@router.post("/agent/chat")
async def agent_chat(request: AgentChatRequest):
    if not request.message.strip():
        raise HTTPException(400, "Message cannot be empty")
    if request.editor_session_id != request.editor_context.editor_session_id:
        raise HTTPException(400, "editor_session_id does not match editor_context")
    try:
        application.editor_contexts.capture(request.editor_context.model_dump())
    except EditorContextError as exc:
        raise HTTPException(400, {"code": exc.code, "message": str(exc)}) from exc
    try:
        result = await agent_runtime.run(request.editor_session_id, request.message.strip())
        return {"message": result.message, "actions": result.actions}
    except AgentConfigurationError as exc:
        return {"message": str(exc), "actions": [], "error": {"code": "OPENAI_API_KEY_MISSING", "message": str(exc)}}
    except Exception as exc:
        return {"message": "The built-in agent could not complete the request.", "actions": [],
                "error": {"code": "AGENT_ERROR", "message": str(exc)}}


@router.post("/projects/{project_id}/analyze-silence")
def analyze_silence(project_id: str, request: SilenceAnalysisRequest):
    try:
        return service.analyze_silence(project_id, request.minimum_silence_ms, request.noise_threshold_db)
    except (SilenceAnalysisError, FileNotFoundError, ValidationError) as exc:
        raise HTTPException(400, {"code": getattr(exc, "code", "INVALID_ARGUMENT"), "message": str(exc)}) from exc


@router.post("/projects/{project_id}/remove-silence")
def remove_silence(project_id: str, request: SilenceRemovalRequest):
    try:
        result = service.remove_silence(project_id, request.expected_revision, request.minimum_silence_ms,
                                        request.noise_threshold_db, request.keep_padding_ms,
                                        guard_tokens=request.guard_tokens)
        return {key: (_rest_project(value) if key == "project" else value) for key, value in result.items()}
    except StaleRevisionError as exc:
        raise HTTPException(409, {"code": "STALE_REVISION", "message": str(exc), "current_revision": exc.current_revision}) from exc
    except GuardError as exc:
        raise _guard_http_error(exc) from exc
    except (SilenceAnalysisError, FileNotFoundError, ValidationError) as exc:
        raise HTTPException(400, {"code": getattr(exc, "code", "INVALID_ARGUMENT"), "message": str(exc)}) from exc


@router.get("/projects")
def list_projects():
    return [_rest_project(project) for project in service.list()]


@router.post("/projects")
def create_project(request: CreateProject):
    return _rest_project(service.create(request.name))


@router.get("/projects/{project_id}")
def get_project(project_id: str):
    try:
        return _rest_project(service.get(project_id))
    except (FileNotFoundError, ValidationError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/projects/{project_id}/timeline")
def get_timeline(project_id: str):
    try:
        return service.timeline(project_id)
    except (FileNotFoundError, ValidationError) as exc:
        raise _rest_project_read_error(exc) from exc


@router.post("/projects/{project_id}/timeline/query")
def query_timeline_route(project_id: str, request: dict):
    try:
        return service.query_timeline(project_id, request)
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc
    except QueryValidationError as exc:
        raise HTTPException(400, {"code": "INVALID_QUERY", "message": str(exc)}) from exc
    except ValidationError as exc:
        raise _rest_project_read_error(exc) from exc


def _transaction_http_error(exc: TransactionError) -> HTTPException:
    status = 409 if exc.code in {"REVISION_CONFLICT", "GUARD_CONFLICT"} else 403 if exc.code == "GUARD_CAPABILITY_INVALID" else 500 if exc.code in {"INTEGRITY_ERROR", "PERSISTENCE_ERROR", "GUARD_STATE_ERROR"} else 400
    detail = {"code": exc.code, "message": exc.message}
    for key in ("operation_index", "operation", "cause_code", "current_revision", "current_revision_id"):
        value = getattr(exc, key, None)
        if value is not None:
            detail[key] = value
    if exc.conflicts:
        detail["conflicts"] = exc.conflicts
    return HTTPException(status, detail)


@router.post("/projects/{project_id}/transactions/prepare")
def prepare_transaction(project_id: str, request: dict):
    try:
        return service.prepare_transaction(project_id, request)
    except TransactionError as exc:
        raise _transaction_http_error(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc
    except ValidationError as exc:
        raise HTTPException(400, {"code": "INVALID_TRANSACTION", "message": "Transaction request is invalid"}) from exc


@router.post("/projects/{project_id}/transactions/commit")
def commit_transaction(project_id: str, request: dict):
    try:
        return service.commit_transaction(project_id, request, origin="rest", actor={"type": "human"})
    except TransactionError as exc:
        raise _transaction_http_error(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc
    except ValidationError as exc:
        raise HTTPException(400, {"code": "INVALID_TRANSACTION", "message": "Transaction request is invalid"}) from exc


@router.get("/projects/{project_id}/revisions")
def list_revisions(project_id: str):
    try:
        available, records = service.revision_records(project_id)
        project = service.get(project_id)
        return {"project_id": project_id, "revision": project.revision, "revision_id": project.revision_id,
                "history_available": available,
                "revisions": [revision_metadata_projection(record.metadata, is_head=index == 0) for index, record in enumerate(records)]}
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc
    except ValidationError as exc:
        raise HTTPException(500, {"code": "INTEGRITY_ERROR", "message": "Project integrity validation failed"}) from exc


@router.get("/projects/{project_id}/revisions/{from_revision_id}/diff/{to_revision_id}")
def diff_revisions(project_id: str, from_revision_id: str, to_revision_id: str):
    try:
        return service.diff_revisions(project_id, from_revision_id, to_revision_id)
    except RevisionNotFoundError as exc:
        raise HTTPException(404, {"code": "REVISION_NOT_FOUND", "message": "Revision does not exist"}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc
    except RevisionDiffError as exc:
        status = 500 if exc.code == "INTEGRITY_ERROR" else 404 if exc.code == "HISTORY_UNAVAILABLE" else 400
        messages = {
            "INTEGRITY_ERROR": "Revision history integrity validation failed",
            "HISTORY_UNAVAILABLE": "Revision history is unavailable for this project",
            "INVALID_ARGUMENT": "Invalid project or revision identifier",
        }
        raise HTTPException(status, {"code": exc.code, "message": messages.get(exc.code, "Invalid argument")}) from exc


@router.get("/projects/{project_id}/revisions/{revision_id}")
def get_revision(project_id: str, revision_id: str):
    try:
        record = service.revision_record(project_id, revision_id)
        return revision_projection(record, is_head=record.metadata.revision_id == service.get(project_id).revision_id)
    except RevisionNotFoundError as exc:
        raise HTTPException(404, {"code": "REVISION_NOT_FOUND", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc
    except ValidationError as exc:
        raise HTTPException(500, {"code": "INTEGRITY_ERROR", "message": "Project integrity validation failed"}) from exc


def _restore_http_error(exc: RestoreError) -> HTTPException:
    status = {
        "INVALID_ARGUMENT": 400,
        "NO_CHANGES": 400,
        "PROJECT_NOT_FOUND": 404,
        "HISTORY_UNAVAILABLE": 404,
        "REVISION_NOT_FOUND": 404,
        "REVISION_CONFLICT": 409,
        "GUARD_CONFLICT": 409,
        "GUARD_CAPABILITY_INVALID": 403,
        "GUARD_STATE_ERROR": 500,
        "INTEGRITY_ERROR": 500,
        "PERSISTENCE_ERROR": 500,
    }.get(exc.code, 500)
    detail = {"code": exc.code, "message": exc.message}
    if exc.current_revision is not None:
        detail["current_revision"] = exc.current_revision
    if exc.current_revision_id is not None:
        detail["current_revision_id"] = exc.current_revision_id
    if exc.conflicts:
        detail["conflicts"] = exc.conflicts
    return HTTPException(status, detail)


@router.post("/projects/{project_id}/revisions/{target_revision_id}/restore")
def restore_revision(project_id: str, target_revision_id: str, request: RestoreRevisionRequest):
    try:
        return service.restore_revision(project_id, target_revision_id, request,
                                        origin="rest", actor={"type": "human"}).model_dump(mode="json")
    except RestoreError as exc:
        raise _restore_http_error(exc) from exc


@router.get("/projects/{project_id}/assets/{asset_id}/media")
def media(project_id: str, asset_id: str):
    try:
        path = service.media_path(project_id, asset_id)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return FileResponse(path)
    except (FileNotFoundError, ValidationError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/projects/{project_id}/assets")
def import_media(project_id: str, request: ImportMedia):
    try:
        return _rest_project(service.import_media(project_id, request.path, origin="rest", actor={"type": "human"}, guard_tokens=request.guard_tokens))
    except (FileNotFoundError, ProbeError, ValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except StaleRevisionError as exc:
        raise HTTPException(409, str(exc)) from exc
    except GuardError as exc:
        raise _guard_http_error(exc) from exc


@router.post("/projects/{project_id}/assets/upload")
async def upload_media(project_id: str, file: UploadFile = File(...), expected_revision: int = Form(...), guard_tokens: list[str] = Form(default=[])):
    try:
        project = await service.import_uploaded_media(project_id, file, expected_revision,
                                                       origin="rest", actor={"type": "human"}, guard_tokens=guard_tokens)
        return _rest_project(project)
    except StaleRevisionError as exc:
        raise HTTPException(409, {"code": "STALE_REVISION", "message": str(exc),
                                  "current_revision": exc.current_revision}) from exc
    except (FileNotFoundError, ProbeError, ValidationError) as exc:
        raise HTTPException(400, {"code": getattr(exc, "code", "INVALID_ARGUMENT"),
                                  "message": str(exc)}) from exc
    except GuardError as exc:
        raise _guard_http_error(exc) from exc


def _guard_http_error(exc: GuardError) -> HTTPException:
    status = {"GUARD_CONFLICT": 409, "GUARD_CAPABILITY_INVALID": 403,
              "GUARD_NOT_FOUND": 404, "GUARD_STATE_ERROR": 500}.get(exc.code, 400)
    detail = {"code": exc.code, "message": exc.message}
    if exc.conflicts:
        detail["conflicts"] = exc.conflicts
    return HTTPException(status, detail)


@router.post("/projects/{project_id}/guards")
def acquire_guard(project_id: str, request: AcquireGuardRequest):
    try:
        return service.guards.acquire(project_id, request.owner, request.scope, request.ttl_seconds, request.purpose)
    except GuardError as exc:
        raise _guard_http_error(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc


@router.get("/projects/{project_id}/guards")
def list_guards(project_id: str):
    try:
        return service.guards.list(project_id)
    except GuardError as exc:
        raise _guard_http_error(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc


@router.post("/projects/{project_id}/guards/{guard_id}/renew")
def renew_guard(project_id: str, guard_id: str, request: RenewGuardRequest):
    try:
        return service.guards.renew(project_id, guard_id, request.guard_token, request.ttl_seconds)
    except GuardError as exc:
        raise _guard_http_error(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc


@router.post("/projects/{project_id}/guards/{guard_id}/release")
def release_guard(project_id: str, guard_id: str, request: ReleaseGuardRequest):
    try:
        return service.guards.release(project_id, guard_id, request.guard_token)
    except GuardError as exc:
        raise _guard_http_error(exc) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, {"code": "PROJECT_NOT_FOUND", "message": "Project does not exist"}) from exc


def _mutation_error(exc: Exception) -> HTTPException:
    if isinstance(exc, GuardError):
        return _guard_http_error(exc)
    if isinstance(exc, StaleRevisionError):
        return HTTPException(409, str(exc))
    if isinstance(exc, (ValidationError, FileNotFoundError)):
        return HTTPException(400, str(exc))
    raise exc


@router.post("/projects/{project_id}/clips/split")
def split(project_id: str, request: SplitMutation):
    try:
        return _rest_project(service.split(project_id, request.clip_id, request.timeline_frame, request.expected_revision, origin="rest", actor={"type": "human"}, guard_tokens=request.guard_tokens))
    except (StaleRevisionError, ValidationError, FileNotFoundError, GuardError) as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/clips/delete")
def delete(project_id: str, request: ClipMutation):
    try:
        return _rest_project(service.delete(project_id, request.clip_id, request.expected_revision, origin="rest", actor={"type": "human"}, guard_tokens=request.guard_tokens))
    except (StaleRevisionError, ValidationError, FileNotFoundError, GuardError) as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/clips/move")
def move(project_id: str, request: MoveMutation):
    try:
        return _rest_project(service.move(project_id, request.clip_id, request.timeline_start_frame, request.expected_revision, origin="rest", actor={"type": "human"}, guard_tokens=request.guard_tokens))
    except (StaleRevisionError, ValidationError, FileNotFoundError, GuardError) as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/clips/trim")
def trim(project_id: str, request: TrimMutation):
    try:
        return _rest_project(service.trim(project_id, request.clip_id, request.expected_revision, request.source_in_frame, request.source_out_frame, origin="rest", actor={"type": "human"}, guard_tokens=request.guard_tokens))
    except (StaleRevisionError, ValidationError, FileNotFoundError, GuardError) as exc:
        raise _mutation_error(exc) from exc


@router.post("/projects/{project_id}/markers/add")
def add_marker(project_id: str, request: AddMarkerMutation):
    try:
        before = service.get(project_id)
        prior_ids = {marker.id for marker in before.timeline.markers}
        result = service.add_marker(project_id, request.expected_revision, request.start_frame, request.end_frame,
                                    request.name, request.description, request.type, request.production.model_dump(),
                                    origin="rest", actor={"type": "human"}, guard_tokens=request.guard_tokens)
        response = _rest_project(result)
        response["marker_id"] = next(marker["id"] for marker in response["timeline"]["markers"] if marker["id"] not in prior_ids)
        return response
    except StaleRevisionError as exc:
        raise HTTPException(409, {"code": "STALE_REVISION", "message": str(exc), "current_revision": exc.current_revision}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GuardError as exc:
        raise _guard_http_error(exc) from exc
    except ValidationError as exc:
        raise HTTPException(400, {"code": "INVALID_MARKER", "message": str(exc)}) from exc


@router.post("/projects/{project_id}/markers/update")
def update_marker(project_id: str, request: UpdateMarkerMutation):
    try:
        result = service.update_marker(project_id, request.expected_revision, request.marker_id, request.changes,
                                       origin="rest", actor={"type": "human"}, guard_tokens=request.guard_tokens)
        response = _rest_project(result)
        response["marker_id"] = request.marker_id
        return response
    except StaleRevisionError as exc:
        raise HTTPException(409, {"code": "STALE_REVISION", "message": str(exc), "current_revision": exc.current_revision}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GuardError as exc:
        raise _guard_http_error(exc) from exc
    except ValidationError as exc:
        code = "NO_CHANGES" if "no changes" in str(exc).lower() else "MARKER_NOT_FOUND" if "Marker does not exist" in str(exc) else "INVALID_MARKER"
        raise HTTPException(400, {"code": code, "message": str(exc)}) from exc


@router.post("/projects/{project_id}/markers/delete")
def delete_marker(project_id: str, request: DeleteMarkerMutation):
    try:
        result = service.delete_marker(project_id, request.expected_revision, request.marker_id,
                                       origin="rest", actor={"type": "human"}, guard_tokens=request.guard_tokens)
        response = _rest_project(result)
        response["deleted_marker_id"] = request.marker_id
        return response
    except StaleRevisionError as exc:
        raise HTTPException(409, {"code": "STALE_REVISION", "message": str(exc), "current_revision": exc.current_revision}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except GuardError as exc:
        raise _guard_http_error(exc) from exc
    except ValidationError as exc:
        code = "MARKER_NOT_FOUND" if "Marker does not exist" in str(exc) else "INVALID_MARKER"
        raise HTTPException(400, {"code": code, "message": str(exc)}) from exc


def _render_response(result):
    return {"path": result.path, "render_type": result.render_type, "revision": result.revision, "duration_frames": result.duration_frames}


@router.post("/projects/{project_id}/render-preview")
def render_preview(project_id: str, request: RenderRequest):
    try:
        result = service.render_preview(project_id, request.expected_revision)
        return {**_render_response(result), "url": f"/api/projects/{project_id}/renders/preview/{result.revision}"}
    except (StaleRevisionError, ValidationError, FileNotFoundError) as exc:
        raise _mutation_error(exc) from exc
    except Exception as exc:
        from app.rendering.ffmpeg import RenderError
        if isinstance(exc, RenderError):
            raise HTTPException(400, str(exc)) from exc
        raise


@router.post("/projects/{project_id}/export")
def export_project(project_id: str, request: RenderRequest):
    try:
        result = service.export_project(project_id, request.expected_revision)
        return {**_render_response(result), "url": f"/api/projects/{project_id}/renders/export/{result.revision}"}
    except (StaleRevisionError, ValidationError, FileNotFoundError) as exc:
        raise _mutation_error(exc) from exc
    except Exception as exc:
        from app.rendering.ffmpeg import RenderError
        if isinstance(exc, RenderError):
            raise HTTPException(400, str(exc)) from exc
        raise


@router.get("/projects/{project_id}/renders/current/{render_type}")
def current_render(project_id: str, render_type: str):
    try:
        return service.current_render(project_id, render_type)
    except (FileNotFoundError, ValidationError) as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/projects/{project_id}/renders/{render_type}/{revision}")
def rendered_media(project_id: str, render_type: str, revision: int):
    if render_type not in ("preview", "export"):
        raise HTTPException(404, "Unknown render type")
    try:
        path = service.render_path(project_id, render_type, revision)
    except ValidationError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, "Rendered media does not exist")
    return FileResponse(path, media_type="video/mp4")
