from __future__ import annotations

import json
import pytest
from fastapi.testclient import TestClient

from app.domain.models import Asset, FrameRate, ValidationError
from app.domain.operations import new_project, register_asset
from app.mcp_resources import ResourceReadError, validate_resource_uri
from app.mcp_server import mcp
from app.persistence.project_store import ProjectStore
from app.services.projects import ProjectService
from app.services.projections import revision_projection
from app.main import app
import app.api.routes as routes
import app.mcp_resources as resource_module
import asyncio


def seeded_service(tmp_path):
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    project = register_asset(new_project("Read surface"), Asset("asset", "/safe/media.mp4", "media.mp4", "h264", 320, 180, FrameRate(24, 1), 100))
    service.store.save(project)
    return service, project


def test_mcp_exposes_exactly_fifteen_tools_and_eight_resources():
    assert len(mcp._tool_manager.list_tools()) == 15
    assert {tool.name for tool in mcp._tool_manager.list_tools()} >= {"query_timeline"}
    assert len(mcp._resource_manager.list_resources()) == 1
    assert len(mcp._resource_manager.list_templates()) == 7
    assert all(template.mime_type == "application/json" for template in mcp._resource_manager.list_templates())
    assert all(tool.output_schema for tool in mcp._tool_manager.list_tools())
    timeline_tool = next(tool for tool in mcp._tool_manager.list_tools() if tool.name == "get_timeline")
    unstructured, structured = timeline_tool.fn_metadata.convert_result({"project_id": "project_1", "ok": True})
    assert unstructured[0].text.startswith("{")
    assert structured["project_id"] == "project_1"


def test_resource_uri_validation_rejects_unsafe_forms():
    assert validate_resource_uri("textsequence://projects") == []
    assert validate_resource_uri("textsequence://projects/project_1/timeline") == ["project_1", "timeline"]
    for uri in (
        "textsequence://projects/project_1/",
        "textsequence://projects/project_1/timeline?x=1",
        "textsequence://projects/project_1/timeline#head",
        "textsequence://projects/project_1/../timeline",
        "textsequence://projects/project_1/assets/a%2Fb",
        "other://projects/project_1/timeline",
    ):
        with pytest.raises(ResourceReadError) as exc:
            validate_resource_uri(uri)
        assert exc.value.code == "INVALID_RESOURCE_URI"


def test_query_timeline_uses_frame_semantics_and_safe_projections(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    project = service.split(project.id, clip_id, 40, 0)
    result = service.query_timeline(project.id, {"entity_types": ["clip"], "frame": 40})
    assert [clip["timeline_start_frame"] for clip in result["clips"]] == [40]
    assert result["result_count"] == 1
    assert "path" not in str(result)
    with pytest.raises(ValidationError):
        service.query_timeline(project.id, {"entity_types": ["clip"]})
    with pytest.raises(ValidationError):
        service.query_timeline(project.id, {"entity_types": ["clip"], "frame": True, "asset_id": "asset"})
    with pytest.raises(ValidationError):
        service.query_timeline(project.id, {"entity_types": ["clip"], "frame": 1, "frame_range": {"start_frame": 0, "end_frame": 2}})


def test_revision_reads_are_head_reachable_and_legacy_history_is_not_promoted(tmp_path):
    service, project = seeded_service(tmp_path)
    first = service.split(project.id, project.timeline.tracks[0].clips[0].id, 40, 0)
    available, records = service.revision_records(project.id)
    assert available is True
    assert [record.metadata.revision_number for record in records] == [1, 0]
    assert service.revision_record(project.id, first.revision_id).metadata.revision_id == first.revision_id
    assert "path" not in str(revision_projection(service.revision_record(project.id, first.revision_id)))

    legacy_root = tmp_path / "legacy"
    legacy_service, legacy_project = seeded_service(legacy_root)
    raw = legacy_service.store.path_for(legacy_project.id)
    directory = legacy_service.store.directory_for(legacy_project.id)
    import shutil
    shutil.copyfile(directory / "revisions" / f"{legacy_project.revision_id}.json", raw)
    raw.write_text(__import__("json").dumps(__import__("json").loads(raw.read_text())["snapshot"]))
    shutil.rmtree(directory)
    assert legacy_service.revision_records(legacy_project.id) == (False, [])


def test_rest_read_routes_return_shared_safe_shapes(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    monkeypatch.setattr(routes, "service", service)
    project = service.add_marker(project.id, 0, 10, 20, "Edit", marker_type="shot")
    client = TestClient(app)
    timeline = client.get(f"/api/projects/{project.id}/timeline")
    assert timeline.status_code == 200
    assert timeline.json()["markers"][0]["start_frame"] == 10
    query = client.post(f"/api/projects/{project.id}/timeline/query", json={"entity_types": ["marker"], "marker_type": "shot"})
    assert query.status_code == 200
    assert query.json()["result_count"] == 1
    revisions = client.get(f"/api/projects/{project.id}/revisions")
    assert revisions.status_code == 200
    assert revisions.json()["history_available"] is True
    revision_id = revisions.json()["revisions"][0]["revision_id"]
    revision = client.get(f"/api/projects/{project.id}/revisions/{revision_id}")
    assert revision.status_code == 200
    assert "path" not in str(revision.json())


def test_registered_mcp_resource_reads_are_json_envelopes(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    monkeypatch.setattr(resource_module, "application", type("App", (), {"projects": service})())
    uri = f"textsequence://projects/{project.id}/timeline"
    resource = asyncio.run(mcp._resource_manager.get_resource(uri))
    payload = __import__("json").loads(asyncio.run(resource.read()))
    assert payload["resource_type"] == "timeline"
    assert payload["uri"] == uri
    assert "path" not in str(payload)


def test_rest_read_integrity_failures_are_sanitized(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    monkeypatch.setattr(routes, "service", service)
    directory = service.store.directory_for(project.id)
    head = json.loads((directory / "head.json").read_text())
    revision_path = directory / "revisions" / f"{head['revision_id']}.json"
    record = json.loads(revision_path.read_text())
    record["metadata"]["snapshot_sha256"] = "0" * 64
    revision_path.write_text(json.dumps(record))
    with pytest.raises(ValidationError) as internal:
        service.get(project.id)
    raw_error = str(internal.value)

    client = TestClient(app)
    responses = [
        client.post(f"/api/projects/{project.id}/timeline/query", json={"entity_types": ["clip"], "frame": 1}),
        client.get(f"/api/projects/{project.id}/timeline"),
        client.get(f"/api/projects/{project.id}/revisions"),
        client.get(f"/api/projects/{project.id}/revisions/{head['revision_id']}"),
    ]
    for response in responses:
        body = response.text
        assert response.status_code == 500
        assert response.json()["detail"]["code"] == "INTEGRITY_ERROR"
        assert "Project integrity validation failed" in body
        assert raw_error not in body
        assert "/Users/" not in body
        assert "/var/" not in body
        assert str(directory) not in body
        assert revision_path.name not in body
