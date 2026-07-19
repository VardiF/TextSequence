import json

import pytest
from fastapi.testclient import TestClient

from app.api.routes import service as api_service
from app.domain.models import Asset, ExternalReference, FrameRate, Marker, MarkerProductionMetadata, ValidationError, project_from_dict, project_to_dict
from app.domain.operations import add_marker, delete_clip, delete_marker, move_clip, new_marker_id, new_project, register_asset, split_clip, trim_clip, update_marker
from app.domain.silence import SourceRemovalRange, apply_silence_removals
from app.main import app
from app.mcp_server import add_marker as mcp_add_marker, delete_marker as mcp_delete_marker, update_marker as mcp_update_marker
from app.persistence.project_store import ProjectStore
from app.services.projects import ProjectService
from app.services.timeline import timeline_projection


def marker_project():
    project = new_project("Markers")
    asset = Asset("asset_marker", "/safe/marker.mp4", "marker.mp4", "h264", 1, 1, FrameRate(24, 1), 100)
    register_asset(project, asset)
    return project


def make_marker(start=10, end=None, name="Marker", marker_type="generic"):
    return Marker(new_marker_id(), start, end, name, "Description", marker_type,
                  MarkerProductionMetadata(shot_ids=["shot_1"], dialogue_line_ids=[],
                                           external_refs=[ExternalReference("external-production-system", "ref_1", "note")]))


def test_marker_validates_point_range_bool_and_type_rules():
    point = make_marker(10)
    one_frame = make_marker(10, 11, "One frame")
    assert point.end_frame is None
    assert one_frame.end_frame == 11
    with pytest.raises(ValidationError): Marker(new_marker_id(), True, None, "Bad")
    with pytest.raises(ValidationError): Marker(new_marker_id(), 10, False, "Bad")
    with pytest.raises(ValidationError): Marker(new_marker_id(), 10, 10, "Bad")
    with pytest.raises(ValidationError): Marker(new_marker_id(), 10, 12, "Bad", type="Shot")
    with pytest.raises(ValidationError): Marker("marker_bad", 10, None, "Bad")
    trimmed = Marker(new_marker_id(), 1, None, "  Trimmed  ")
    assert trimmed.name == "Trimmed"


def test_marker_serialization_is_strict_and_canonically_ordered():
    project = new_project("Order")
    later = make_marker(30, None, "Later")
    earlier = make_marker(10, 20, "Earlier")
    project.timeline.markers = [later, earlier]
    document = project_to_dict(project)
    assert [item["id"] for item in document["timeline"]["markers"]] == [earlier.id, later.id]
    assert project_to_dict(project_from_dict(document)) == document
    malformed = json.loads(json.dumps(document))
    malformed["timeline"]["markers"][0]["unexpected"] = True
    with pytest.raises(ValidationError, match="Unknown field"):
        project_from_dict(malformed)
    duplicate = marker_project()
    duplicate.timeline.markers = [Marker(new_marker_id(), 1, None, "Duplicate", production=MarkerProductionMetadata(shot_ids=["same", "same"]))]
    with pytest.raises(ValidationError, match="unique"):
        duplicate.validate()
    collision = marker_project()
    collision.timeline.markers = [Marker(new_marker_id(), 1, None, "Collision")]
    collision.id = collision.timeline.markers[0].id
    with pytest.raises(ValidationError, match="unique"):
        collision.validate()


def test_marker_domain_operations_cover_patch_conversion_move_delete_and_noop():
    project = marker_project()
    original = make_marker(10, None, "Point")
    project = add_marker(project, original)
    updated = update_marker(project, original.id, {"end_frame": 20, "name": "Range"})
    changed = updated.timeline.markers[0]
    assert changed.id == original.id
    assert (changed.start_frame, changed.end_frame, changed.name) == (10, 20, "Range")
    moved = update_marker(updated, original.id, {"start_frame": 40, "end_frame": 50})
    assert (moved.timeline.markers[0].start_frame, moved.timeline.markers[0].end_frame) == (40, 50)
    point_again = update_marker(moved, original.id, {"end_frame": None})
    assert point_again.timeline.markers[0].end_frame is None
    with pytest.raises(ValidationError, match="no changes"):
        update_marker(point_again, original.id, {})
    with pytest.raises(ValidationError, match="cannot be updated"):
        update_marker(point_again, original.id, {"id": new_marker_id()})
    removed = delete_marker(point_again, original.id)
    assert removed.timeline.markers == []


def test_markers_remain_absolute_through_clip_operations():
    project = marker_project()
    marker = make_marker(20, 30)
    project = add_marker(project, marker)
    clip_id = project.tracks[0].clips[0].id
    edited = split_clip(project, clip_id, 40)
    edited = move_clip(edited, edited.tracks[0].clips[1].id, 120)
    edited = trim_clip(edited, clip_id, source_in_frame=5)
    edited = delete_clip(edited, clip_id)
    assert [(item.id, item.start_frame, item.end_frame) for item in edited.timeline.markers] == [(marker.id, 20, 30)]


def test_marker_stays_absolute_during_silence_compaction():
    project = marker_project()
    marker = make_marker(20, 30)
    project = add_marker(project, marker)
    edited, *_ = apply_silence_removals(project, [SourceRemovalRange("asset_marker", 5, 10)])
    assert [(item.id, item.start_frame, item.end_frame) for item in edited.timeline.markers] == [(marker.id, 20, 30)]


def test_projection_exposes_sorted_markers_and_separate_content_display_extents():
    project = marker_project()
    project.timeline.markers = [make_marker(180, 220, "Beyond"), make_marker(2, None, "Point")]
    projection = timeline_projection(project)
    assert projection["content_end_frame"] == 100
    assert projection["display_end_frame"] == 220
    assert [item["name"] for item in projection["markers"]] == ["Point", "Beyond"]


def test_marker_service_and_rest_mutations_are_revision_checked(tmp_path):
    store = ProjectStore(tmp_path)
    service = ProjectService(store, tmp_path / "runtime")
    project = service.create("API markers")
    api_service.store = store
    client = TestClient(app)
    added = client.post(f"/api/projects/{project.id}/markers/add", json={
        "expected_revision": 0, "start_frame": 12, "name": "Cut", "type": "edit",
    })
    assert added.status_code == 200
    added_data = added.json()
    marker_id = added_data["marker_id"]
    assert added_data["revision"] == 1
    updated = client.post(f"/api/projects/{project.id}/markers/update", json={
        "expected_revision": 1, "marker_id": marker_id, "changes": {"end_frame": 24, "name": "Cut range"},
    })
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    no_change = client.post(f"/api/projects/{project.id}/markers/update", json={
        "expected_revision": 2, "marker_id": marker_id, "changes": {},
    })
    assert no_change.status_code == 400
    assert no_change.json()["detail"]["code"] == "NO_CHANGES"
    deleted = client.post(f"/api/projects/{project.id}/markers/delete", json={
        "expected_revision": 2, "marker_id": marker_id,
    })
    assert deleted.status_code == 200
    assert deleted.json()["deleted_marker_id"] == marker_id
    assert deleted.json()["revision"] == 3
    stale = client.post(f"/api/projects/{project.id}/markers/add", json={
        "expected_revision": 0, "start_frame": 2, "name": "Stale",
    })
    assert stale.status_code == 409


def test_marker_revision_snapshot_tampering_is_detected(tmp_path):
    store = ProjectStore(tmp_path)
    service = ProjectService(store, tmp_path / "runtime")
    project = service.create("Tamper")
    project = service.add_marker(project.id, 0, 5, name="Safe")
    head = json.loads((tmp_path / project.id / "head.json").read_text())
    record_path = tmp_path / project.id / "revisions" / f"{head['revision_id']}.json"
    record = json.loads(record_path.read_text())
    record["snapshot"]["timeline"]["markers"][0]["name"] = "Tampered"
    record_path.write_text(json.dumps(record))
    with pytest.raises(ValidationError):
        store.load(project.id)


def test_mcp_marker_mutations_return_canonical_results(tmp_path, monkeypatch):
    from app.application import application
    store = ProjectStore(tmp_path)
    monkeypatch.setattr(application, "projects", ProjectService(store, tmp_path / "runtime"))
    project = application.projects.create("MCP markers")
    added = mcp_add_marker(project.id, 0, 8, "MCP point")
    assert added["ok"] is True
    marker_id = added["marker_id"]
    updated = mcp_update_marker(project.id, marker_id, 1, {"end_frame": 16})
    assert updated["ok"] is True
    deleted = mcp_delete_marker(project.id, marker_id, 2)
    assert deleted["ok"] is True
