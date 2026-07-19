from __future__ import annotations

import re
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

from app.application import application
from app.domain.models import ValidationError
from app.persistence.project_store import RevisionNotFoundError
from app.services.projections import (asset_projection, clip_projection, marker_projection,
                                      project_projection, project_summary_projection,
                                      revision_metadata_projection, revision_projection)
from app.services.timeline import timeline_projection

RESOURCE_SCHEME = "textsequence"
RESOURCE_AUTHORITY = "projects"
_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"


class ResourceReadError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_resource_uri(uri: str) -> list[str]:
    if not isinstance(uri, str):
        raise ResourceReadError("INVALID_RESOURCE_URI", "Resource URI must be a string")
    parsed = urlsplit(uri)
    if parsed.scheme != RESOURCE_SCHEME or parsed.netloc != RESOURCE_AUTHORITY or parsed.query or parsed.fragment:
        raise ResourceReadError("INVALID_RESOURCE_URI", "Invalid TextSequence resource URI")
    if "%" in parsed.path or "\\" in parsed.path or parsed.path.endswith("/"):
        raise ResourceReadError("INVALID_RESOURCE_URI", "Invalid TextSequence resource URI")
    parts = parsed.path.strip("/").split("/") if parsed.path else []
    if any(part in {"", ".", ".."} or not re.fullmatch(_ID, part) for part in parts):
        raise ResourceReadError("INVALID_RESOURCE_URI", "Invalid TextSequence resource URI")
    valid = (
        parts == [] or
        (len(parts) == 1 and re.fullmatch(_ID, parts[0])) or
        (len(parts) == 2 and re.fullmatch(_ID, parts[0]) and parts[1] in {"timeline", "revisions"}) or
        (len(parts) == 3 and re.fullmatch(_ID, parts[0]) and parts[1] in {"assets", "clips", "markers", "revisions"} and re.fullmatch(_ID, parts[2]))
    )
    if not valid:
        raise ResourceReadError("INVALID_RESOURCE_URI", "Invalid TextSequence resource URI")
    return parts


def _envelope(resource_type: str, uri: str, data, state: dict | None = None) -> dict:
    return {"resource_type": resource_type, "uri": uri, "state": state or {}, "data": data}


def _project(project_id: str):
    try:
        return application.projects.get(project_id)
    except FileNotFoundError as exc:
        raise ResourceReadError("RESOURCE_NOT_FOUND", "Project does not exist") from exc
    except ValidationError as exc:
        raise ResourceReadError("INTEGRITY_ERROR", "Project integrity validation failed") from exc


def _entity(project_id: str, kind: str, entity_id: str):
    project = _project(project_id)
    if kind == "assets":
        asset = next((item for item in project.assets if item.id == entity_id), None)
        if asset is not None: return project, asset_projection(asset)
    elif kind == "clips":
        for track in project.timeline.tracks:
            clip = next((item for item in track.clips if item.id == entity_id), None)
            if clip is not None: return project, clip_projection(clip, track, {a.id: a for a in project.assets})
    elif kind == "markers":
        marker = next((item for item in project.timeline.markers if item.id == entity_id), None)
        if marker is not None: return project, marker_projection(marker)
    raise ResourceReadError("RESOURCE_NOT_FOUND", "Resource entity does not exist")


def register_resources(mcp: FastMCP) -> None:
    @mcp.resource("textsequence://projects", mime_type="application/json")
    def projects_resource() -> dict:
        uri = "textsequence://projects"
        validate_resource_uri(uri)
        try:
            projects = [project_summary_projection(project) for project in application.projects.list()]
        except ValidationError as exc:
            raise ResourceReadError("INTEGRITY_ERROR", "Project integrity validation failed") from exc
        return _envelope("projects", uri, projects)

    @mcp.resource("textsequence://projects/{project_id}", mime_type="application/json")
    def project_resource(project_id: str) -> dict:
        uri = f"textsequence://projects/{project_id}"
        validate_resource_uri(uri)
        project = _project(project_id)
        return _envelope("project", uri, project_projection(project), {"kind": "head", "revision": project.revision, "revision_id": project.revision_id})

    @mcp.resource("textsequence://projects/{project_id}/timeline", mime_type="application/json")
    def timeline_resource(project_id: str) -> dict:
        uri = f"textsequence://projects/{project_id}/timeline"
        validate_resource_uri(uri)
        project = _project(project_id)
        return _envelope("timeline", uri, timeline_projection(project), {"kind": "head", "revision": project.revision, "revision_id": project.revision_id})

    @mcp.resource("textsequence://projects/{project_id}/assets/{asset_id}", mime_type="application/json")
    def asset_resource(project_id: str, asset_id: str) -> dict:
        uri = f"textsequence://projects/{project_id}/assets/{asset_id}"
        validate_resource_uri(uri)
        project, data = _entity(project_id, "assets", asset_id)
        return _envelope("asset", uri, data, {"kind": "head", "revision": project.revision})

    @mcp.resource("textsequence://projects/{project_id}/clips/{clip_id}", mime_type="application/json")
    def clip_resource(project_id: str, clip_id: str) -> dict:
        uri = f"textsequence://projects/{project_id}/clips/{clip_id}"
        validate_resource_uri(uri)
        project, data = _entity(project_id, "clips", clip_id)
        return _envelope("clip", uri, data, {"kind": "head", "revision": project.revision})

    @mcp.resource("textsequence://projects/{project_id}/markers/{marker_id}", mime_type="application/json")
    def marker_resource(project_id: str, marker_id: str) -> dict:
        uri = f"textsequence://projects/{project_id}/markers/{marker_id}"
        validate_resource_uri(uri)
        project, data = _entity(project_id, "markers", marker_id)
        return _envelope("marker", uri, data, {"kind": "head", "revision": project.revision})

    @mcp.resource("textsequence://projects/{project_id}/revisions", mime_type="application/json")
    def revisions_resource(project_id: str) -> dict:
        uri = f"textsequence://projects/{project_id}/revisions"
        validate_resource_uri(uri)
        project = _project(project_id)
        try:
            available, records = application.projects.revision_records(project_id)
        except ValidationError as exc:
            raise ResourceReadError("INTEGRITY_ERROR", "Project integrity validation failed") from exc
        data = {"history_available": available,
                "revisions": [revision_metadata_projection(record.metadata, is_head=index == 0) for index, record in enumerate(records)]}
        return _envelope("revisions", uri, data, {"kind": "head", "revision": project.revision, "revision_id": project.revision_id})

    @mcp.resource("textsequence://projects/{project_id}/revisions/{revision_id}", mime_type="application/json")
    def revision_resource(project_id: str, revision_id: str) -> dict:
        uri = f"textsequence://projects/{project_id}/revisions/{revision_id}"
        validate_resource_uri(uri)
        project = _project(project_id)
        try:
            record = application.projects.revision_record(project_id, revision_id)
        except RevisionNotFoundError as exc:
            raise ResourceReadError("RESOURCE_NOT_FOUND", "Revision does not exist") from exc
        except ValidationError as exc:
            raise ResourceReadError("INTEGRITY_ERROR", "Project integrity validation failed") from exc
        return _envelope("revision", uri, revision_projection(record, is_head=record.metadata.revision_id == project.revision_id),
                         {"kind": "revision", "revision": record.metadata.revision_number, "revision_id": record.metadata.revision_id})
