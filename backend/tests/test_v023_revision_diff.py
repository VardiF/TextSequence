from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError as PydanticValidationError

from app.domain.models import Asset, FrameRate, project_to_dict
from app.domain.operations import new_project, register_asset
from app.main import app
from app.persistence.project_store import ProjectStore, RevisionNotFoundError
from app.revision_diff_models import RedactedFieldChange
from app.services.projects import ProjectService
from app.services.revision_diff import RevisionDiffError
import app.api.routes as routes
from app.mcp_server import diff_revisions as mcp_diff_revisions, mcp


def seeded_service(tmp_path):
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    project = register_asset(
        new_project("Diff project"),
        Asset("asset", "/Users/example/source.mp4", "source.mp4", "h264", 320, 180, FrameRate(24, 1), 100),
    )
    service.store.save(project)
    return service, project


def test_revision_diff_is_directional_deterministic_and_same_revision_is_empty(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    changed = service.split(project.id, clip_id, 40, 0)

    forward = service.diff_revisions(project.id, project.revision_id, changed.revision_id)
    assert forward.direction == "forward"
    assert forward.project_id == project.id
    assert forward.timeline_id == project.timeline.id
    assert forward.summary.model_dump(mode="json") == {
        "entities_added": 1, "entities_removed": 0, "entities_modified": 1,
        "fields_modified": 1, "project_fields_modified": 0, "timeline_fields_modified": 0,
        "by_entity_type": {"assets": {"added": 0, "removed": 0, "modified": 0},
                            "tracks": {"added": 0, "removed": 0, "modified": 0},
                            "clips": {"added": 1, "removed": 0, "modified": 1},
                            "markers": {"added": 0, "removed": 0, "modified": 0}},
    }
    assert forward.changes.clips.added[0].id != clip_id
    assert all("duration" not in change.path for item in forward.changes.clips.modified for change in item.fields)

    reverse = service.diff_revisions(project.id, changed.revision_id, project.revision_id)
    assert reverse.direction == "reverse"
    assert reverse.changes.clips.removed[0].id != clip_id
    reverse_modified = next(item for item in reverse.changes.clips.modified if item.id == clip_id)
    assert next(field for field in reverse_modified.fields if field.path == "/source_out_frame").after == 100
    assert reverse.summary.entities_added == forward.summary.entities_removed
    assert reverse.summary.entities_removed == forward.summary.entities_added
    assert reverse.summary.entities_modified == forward.summary.entities_modified
    assert reverse.summary.fields_modified == forward.summary.fields_modified

    same = service.diff_revisions(project.id, changed.revision_id, changed.revision_id)
    assert same.direction == "same"
    assert same.summary.model_dump(mode="json") == {
        "entities_added": 0, "entities_removed": 0, "entities_modified": 0,
        "fields_modified": 0, "project_fields_modified": 0, "timeline_fields_modified": 0,
        "by_entity_type": {entity: {"added": 0, "removed": 0, "modified": 0} for entity in ("assets", "tracks", "clips", "markers")},
    }
    assert same.changes.clips.added == []
    assert same.changes.clips.removed == []
    assert same.changes.clips.modified == []


def test_path_only_asset_change_is_redacted_and_added_removed_values_are_safe(tmp_path):
    service, project = seeded_service(tmp_path)
    original_revision = project.revision_id

    changed = service._commit_operation(
        project.id, 0, lambda candidate: _change_asset_path(candidate, "/var/media/other.mp4"),
        "system", {"type": "system"}, "test_path_change", "Test path-only change",
    )
    diff = service.diff_revisions(project.id, original_revision, changed.revision_id)
    asset_change = diff.changes.assets.modified[0]
    assert diff.summary.entities_modified == 1
    assert diff.summary.fields_modified == 1
    assert diff.summary.by_entity_type.assets.modified == 1
    assert len(asset_change.fields) == 1
    assert isinstance(asset_change.fields[0], RedactedFieldChange)
    assert asset_change.fields[0].path == "/source_location"
    serialized = json.dumps(diff.model_dump(mode="json"))
    assert "/Users/" not in serialized
    assert "/var/" not in serialized
    assert "source.mp4" not in serialized
    assert "other.mp4" not in serialized

    removed = service._commit_operation(
        project.id, changed.revision, lambda candidate: _remove_asset(candidate),
        "system", {"type": "system"}, "test_asset_remove", "Test asset removal",
    )
    removed_diff = service.diff_revisions(project.id, changed.revision_id, removed.revision_id)
    assert removed_diff.changes.assets.removed[0].id == "asset"
    assert removed_diff.summary.entities_removed == 2
    assert removed_diff.summary.fields_modified == 0
    assert removed_diff.summary.by_entity_type.assets.removed == 1
    assert removed_diff.summary.by_entity_type.clips.removed == 1
    assert "/source_location" not in json.dumps(removed_diff.model_dump(mode="json"))
    with pytest.raises(PydanticValidationError):
        RedactedFieldChange.model_validate({"path": "/source_location", "kind": "redacted", "values_redacted": True, "before": "/var/secret"})


def test_diff_compares_explicit_project_timeline_track_clip_and_marker_fields(tmp_path):
    service, project = seeded_service(tmp_path)
    initial_id = project.revision_id

    def edit(candidate):
        candidate.name = "Renamed"
        candidate.external_refs = []
        candidate.timeline.name = "Edited timeline"
        candidate.timeline.tracks[0].name = "Edited V1"
        clip = candidate.timeline.tracks[0].clips[0]
        clip.source_in_frame = 4
        clip.production.shot_ids.append("shot_1")
        return candidate

    changed = service._commit_operation(project.id, 0, edit, "system", {"type": "system"}, "test_diff", "Test field diff")
    diff = service.diff_revisions(project.id, initial_id, changed.revision_id)
    assert {field.path for field in diff.changes.project.fields} == {"/name"}
    assert {field.path for field in diff.changes.timeline.fields} == {"/name"}
    assert {field.path for field in diff.changes.tracks.modified[0].fields} == {"/name"}
    assert {field.path for field in diff.changes.clips.modified[0].fields} == {"/source_in_frame", "/production/shot_ids"}
    assert diff.summary.project_fields_modified == 1
    assert diff.summary.timeline_fields_modified == 1
    assert diff.summary.fields_modified == 5
    assert diff.summary.entities_modified == 2
    assert diff.summary.by_entity_type.tracks.modified == 1
    assert diff.summary.by_entity_type.clips.modified == 1
    serialized = json.dumps(diff.model_dump(mode="json"))
    assert "revision_id" not in serialized.split('"changes"', 1)[1]
    assert "timeline_end" not in serialized


def test_diff_loads_and_validates_reachable_history_once(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    changed = service.split(project.id, project.timeline.tracks[0].clips[0].id, 40, 0)
    calls = 0
    original = service.store._validate_reachable_chain

    def counted(directory, record):
        nonlocal calls
        calls += 1
        return original(directory, record)

    monkeypatch.setattr(service.store, "_validate_reachable_chain", counted)
    service.diff_revisions(project.id, project.revision_id, changed.revision_id)
    assert calls == 1


def test_diff_rejects_unreachable_missing_and_legacy_history_without_writes(tmp_path):
    service, project = seeded_service(tmp_path)
    head_before = (service.store.directory_for(project.id) / "head.json").read_bytes()
    with pytest.raises(RevisionNotFoundError):
        service.diff_revisions(project.id, project.revision_id, "revision_missing")
    head_path = service.store.directory_for(project.id) / "revisions" / f"{project.revision_id}.json"
    (service.store.directory_for(project.id) / "revisions" / "revision_orphan.json").write_bytes(head_path.read_bytes())
    with pytest.raises(RevisionNotFoundError):
        service.diff_revisions(project.id, project.revision_id, "revision_orphan")
    assert (service.store.directory_for(project.id) / "head.json").read_bytes() == head_before

    legacy_root = tmp_path / "legacy"
    legacy_service, legacy_project = seeded_service(legacy_root)
    directory = legacy_service.store.directory_for(legacy_project.id)
    flat_path = legacy_service.store.path_for(legacy_project.id)
    flat_path.write_text(json.dumps(project_to_dict(legacy_project)))
    import shutil
    shutil.rmtree(directory)
    with pytest.raises(RevisionDiffError) as unavailable:
        legacy_service.diff_revisions(legacy_project.id, legacy_project.revision_id, legacy_project.revision_id)
    assert unavailable.value.code == "HISTORY_UNAVAILABLE"
    assert not directory.exists()


def test_timeline_identity_mismatch_is_integrity_error(tmp_path):
    service, project = seeded_service(tmp_path)
    initial_id = project.revision_id

    def change_timeline(candidate):
        candidate.timeline.id = "timeline_other"
        return candidate

    changed = service._commit_operation(project.id, 0, change_timeline, "system", {"type": "system"}, "test_timeline_id", "Test timeline identity")
    with pytest.raises(RevisionDiffError) as error:
        service.diff_revisions(project.id, initial_id, changed.revision_id)
    assert error.value.code == "INTEGRITY_ERROR"


def test_rest_and_mcp_diff_surfaces_return_safe_machine_readable_results(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    changed = service.split(project.id, project.timeline.tracks[0].clips[0].id, 40, 0)
    monkeypatch.setattr(routes, "service", service)
    response = TestClient(app).get(
        f"/api/projects/{project.id}/revisions/{project.revision_id}/diff/{changed.revision_id}"
    )
    assert response.status_code == 200
    assert response.json()["direction"] == "forward"
    assert response.json()["ok"] is True
    assert "/Users/" not in response.text

    import app.mcp_server as server_module
    monkeypatch.setattr(server_module, "application", type("App", (), {"projects": service})())
    result = mcp_diff_revisions(project.id, project.revision_id, changed.revision_id)
    assert result["ok"] is True
    assert result["direction"] == "forward"
    assert response.json()["summary"] == result["summary"]
    assert set(result["summary"]) == {
        "entities_added", "entities_removed", "entities_modified", "fields_modified",
        "project_fields_modified", "timeline_fields_modified", "by_entity_type",
    }
    diff_tool = mcp._tool_manager.get_tool("diff_revisions")
    legacy_content, structured = diff_tool.fn_metadata.convert_result(result)
    assert json.loads(legacy_content[0].text)["summary"] == result["summary"]
    assert structured["result"]["summary"] == result["summary"]
    assert len(mcp._tool_manager.list_tools()) == 23


def _change_asset_path(candidate, path):
    candidate.assets[0].path = path
    return candidate


def _remove_asset(candidate):
    candidate.timeline.tracks[0].clips.clear()
    candidate.assets.clear()
    return candidate
