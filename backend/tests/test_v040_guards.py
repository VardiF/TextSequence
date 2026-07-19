from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain.models import Asset, FrameRate
from app.domain.operations import register_asset
from app.guard_models import GuardError, GuardStateError
from app.persistence.project_store import ProjectStore
from app.services.projects import ProjectService
from app.services.restore import RestoreError
from app.services.transactions import TransactionError
from app.main import app


def make_service(tmp_path: Path) -> tuple[ProjectService, object]:
    service = ProjectService(ProjectStore(tmp_path / "projects"), tmp_path / "runtime", tmp_path / "media")
    project = service.create("Guard test")
    return service, project


def add_media(service: ProjectService, project, tmp_path: Path):
    return service._commit_operation(
        project.id, 0,
        lambda candidate: register_asset(candidate, Asset(
            "asset_guard", str(tmp_path / "source.mp4"), "source.mp4", "h264", 320, 180,
            FrameRate(24, 1), 96,
        )),
        "system", {"type": "system"}, "import_media", "Import media",
    )


def test_scope_normalization_and_owner_metadata_is_not_authorization(tmp_path: Path):
    service, project = make_service(tmp_path)
    project = add_media(service, project, tmp_path)
    clip = project.timeline.tracks[0].clips[0]
    first = service.guards.acquire(
        project.id, {"type": "human", "id": "owner-a"},
        {"kind": "selection", "clip_ids": [clip.id, clip.id], "marker_ids": [],
         "frame_ranges": [{"start_frame": 20, "end_frame": 30}, {"start_frame": 30, "end_frame": 40}]},
    )
    listed = service.guards.list(project.id)
    assert listed["guards"][0]["scope"] == {
        "kind": "selection", "clip_ids": [clip.id], "marker_ids": [],
        "frame_ranges": [{"start_frame": 20, "end_frame": 40}],
    }
    with pytest.raises(GuardError) as conflict:
        service.guards.acquire(project.id, {"type": "agent", "id": "owner-b"},
                               {"kind": "selection", "clip_ids": [clip.id], "marker_ids": [], "frame_ranges": []})
    assert conflict.value.code == "GUARD_CONFLICT"
    assert first["guard_token"] not in str(listed)


def test_capabilities_from_different_descriptive_owners_authorize_all_conflicts(tmp_path: Path):
    service, project = make_service(tmp_path)
    project = add_media(service, project, tmp_path)
    clip_a = project.timeline.tracks[0].clips[0]
    project = service.split(project.id, clip_a.id, 48, project.revision)
    clips = project.timeline.tracks[0].clips
    guard_a = service.guards.acquire(project.id, {"type": "human", "id": "a"},
                                     {"kind": "selection", "clip_ids": [clips[0].id], "marker_ids": [], "frame_ranges": []})
    guard_b = service.guards.acquire(project.id, {"type": "agent", "id": "b"},
                                     {"kind": "selection", "clip_ids": [clips[1].id], "marker_ids": [], "frame_ranges": []})
    prepared = service.prepare_transaction(project.id, {
        "expected_revision": project.revision,
        "operations": [
            {"op": "delete_clip", "clip": {"kind": "id", "id": clips[0].id}},
            {"op": "delete_clip", "clip": {"kind": "id", "id": clips[1].id}},
        ],
    })
    payload = {"transaction_hash": prepared.transaction_hash,
               "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json")}
    with pytest.raises(TransactionError) as blocked:
        service.commit_transaction(project.id, payload)
    assert blocked.value.code == "GUARD_CONFLICT"
    with pytest.raises(TransactionError) as partially_authorized:
        service.commit_transaction(project.id, {**payload, "guard_tokens": [guard_a["guard_token"]]})
    assert partially_authorized.value.code == "GUARD_CONFLICT"
    committed = service.commit_transaction(project.id, {**payload, "guard_tokens": [guard_a["guard_token"], guard_b["guard_token"]]})
    assert committed.status == "committed"


def test_split_authorized_by_source_guard_does_not_add_generated_child_to_scope(tmp_path: Path):
    service, project = make_service(tmp_path)
    project = add_media(service, project, tmp_path)
    source = project.timeline.tracks[0].clips[0]
    guard = service.guards.acquire(project.id, {"type": "human", "id": "editor"},
                                   {"kind": "selection", "clip_ids": [source.id], "marker_ids": [], "frame_ranges": []})
    split = service.split(project.id, source.id, 48, project.revision, guard_tokens=[guard["guard_token"]])
    children = split.timeline.tracks[0].clips
    assert len(children) == 2
    assert service.guards.list(project.id)["guards"][0]["scope"]["clip_ids"] == [source.id]
    deleted = service.delete(split.id, children[1].id, split.revision)
    assert len(deleted.timeline.tracks[0].clips) == 1


def test_marker_guard_blocks_marker_edit_until_capability_is_supplied(tmp_path: Path):
    service, project = make_service(tmp_path)
    marker_project = service.add_marker(project.id, project.revision, 10, name="Protected")
    marker_id = marker_project.timeline.markers[0].id
    guard = service.guards.acquire(marker_project.id, {"type": "agent", "id": "marker-agent"},
                                   {"kind": "selection", "clip_ids": [], "marker_ids": [marker_id], "frame_ranges": []})
    with pytest.raises(GuardError) as blocked:
        service.update_marker(marker_project.id, marker_project.revision, marker_id, {"description": "blocked"})
    assert blocked.value.code == "GUARD_CONFLICT"
    updated = service.update_marker(marker_project.id, marker_project.revision, marker_id,
                                    {"description": "authorized"}, guard_tokens=[guard["guard_token"]])
    assert updated.timeline.markers[0].description == "authorized"


def test_restore_requires_all_active_guard_capabilities(tmp_path: Path):
    service, project = make_service(tmp_path)
    target = service._commit_operation(project.id, project.revision,
                                       lambda candidate: candidate, "system", {"type": "system"}, "noop", "noop")
    changed = service._commit_operation(project.id, target.revision,
                                        lambda candidate: setattr(candidate, "name", "Changed") or candidate,
                                        "system", {"type": "system"}, "rename", "Rename")
    guard = service.guards.acquire(changed.id, {"type": "human", "id": "human"}, {"kind": "project"})
    request = {"expected_revision": changed.revision, "expected_revision_id": changed.revision_id}
    with pytest.raises(RestoreError) as blocked:
        service.restore_revision(changed.id, target.revision_id, request)
    assert blocked.value.code == "GUARD_CONFLICT"
    restored = service.restore_revision(changed.id, target.revision_id,
                                        {**request, "guard_tokens": [guard["guard_token"]]})
    assert restored.status == "restored"


def test_project_guard_blocks_path_import_before_canonical_commit(tmp_path: Path, monkeypatch):
    service, project = make_service(tmp_path)
    guard = service.guards.acquire(project.id, {"type": "agent", "id": "import-agent"}, {"kind": "project"})
    from app.domain.models import Asset
    monkeypatch.setattr("app.services.projects.probe_media", lambda path: Asset(
        "asset_import", path, "import.mp4", "h264", 320, 180, FrameRate(24, 1), 48,
    ))
    with pytest.raises(GuardError) as blocked:
        service.import_media(project.id, str(tmp_path / "import.mp4"))
    assert blocked.value.code == "GUARD_CONFLICT"
    assert service.get(project.id).revision == project.revision
    imported = service.import_media(project.id, str(tmp_path / "import.mp4"), guard_tokens=[guard["guard_token"]])
    assert imported.revision == project.revision + 1


def test_prepare_is_guard_agnostic_but_commit_rechecks_current_guards(tmp_path: Path):
    service, project = make_service(tmp_path)
    project = add_media(service, project, tmp_path)
    clip = project.timeline.tracks[0].clips[0]
    request = {"expected_revision": project.revision,
               "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": clip.id}, "timeline_start_frame": 12}]}
    without_guard = service.prepare_transaction(project.id, request)
    guard = service.guards.acquire(project.id, {"type": "agent", "id": "agent"}, {"kind": "project"})
    with_guard = service.prepare_transaction(project.id, request)
    assert without_guard.transaction_hash == with_guard.transaction_hash
    assert without_guard.diff == with_guard.diff
    payload = {"transaction_hash": with_guard.transaction_hash,
               "prepared_transaction": with_guard.prepared_transaction.model_dump(mode="json")}
    with pytest.raises(TransactionError) as blocked:
        service.commit_transaction(project.id, payload)
    assert blocked.value.code == "GUARD_CONFLICT"
    committed = service.commit_transaction(project.id, {**payload, "guard_tokens": [guard["guard_token"]]})
    assert committed.status == "committed"


def test_range_guard_blocks_geometry_and_project_guard_blocks_restore(tmp_path: Path):
    service, project = make_service(tmp_path)
    project = add_media(service, project, tmp_path)
    clip = project.timeline.tracks[0].clips[0]
    range_guard = service.guards.acquire(project.id, {"type": "human", "id": "human"},
                                         {"kind": "selection", "clip_ids": [], "marker_ids": [],
                                          "frame_ranges": [{"start_frame": 110, "end_frame": 130}]})
    with pytest.raises(GuardError) as blocked:
        service.move(project.id, clip.id, 55, project.revision)
    assert blocked.value.code == "GUARD_CONFLICT"
    moved = service.move(project.id, clip.id, 10, project.revision)
    assert moved.revision == project.revision + 1
    service.guards.release(project.id, range_guard["guard_id"], range_guard["guard_token"])
    target = service._commit_operation(project.id, moved.revision, lambda candidate: candidate, "system", {"type": "system"}, "noop", "noop")
    assert target.revision == moved.revision


def test_guard_leases_survive_restart_and_expiry_is_not_renewable(tmp_path: Path):
    service, project = make_service(tmp_path)
    guard = service.guards.acquire(project.id, {"type": "agent", "id": "agent"}, {"kind": "project"})
    restarted = ProjectService(ProjectStore(tmp_path / "projects"), tmp_path / "runtime", tmp_path / "media")
    assert restarted.guards.list(project.id)["guards"][0]["guard_id"] == guard["guard_id"]
    record = restarted.guards.store.load(project.id)[0]
    restarted.guards.store.save(project.id, [replace(record, created_at="2000-01-01T00:00:00Z", expires_at="2000-01-01T00:01:00Z")])
    assert restarted.guards.list(project.id)["guards"] == []
    with pytest.raises(GuardError) as missing:
        restarted.guards.renew(project.id, guard["guard_id"], guard["guard_token"])
    assert missing.value.code == "GUARD_NOT_FOUND"


def test_corrupt_guard_state_fails_closed_without_revision_change(tmp_path: Path):
    service, project = make_service(tmp_path)
    project = add_media(service, project, tmp_path)
    clip = project.timeline.tracks[0].clips[0]
    state_path = tmp_path / "runtime" / "guards" / f"{project.id}.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"guard_schema_version": 999}')
    with pytest.raises(GuardStateError):
        service.move(project.id, clip.id, 12, project.revision)
    assert service.get(project.id).revision == project.revision


@pytest.mark.parametrize("document", [
    {"guard_schema_version": 1, "project_id": "project_other", "guards": []},
    {"guard_schema_version": 1, "project_id": "project_test", "guards": [], "unexpected": True},
    {"guard_schema_version": 2, "project_id": "project_test", "guards": []},
])
def test_invalid_guard_state_variants_fail_closed(tmp_path: Path, document: dict):
    service, project = make_service(tmp_path)
    path = tmp_path / "runtime" / "guards" / f"{project.id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**document, "project_id": document.get("project_id", project.id)}))
    with pytest.raises(GuardStateError):
        service.guards.list(project.id)


def test_rest_guard_surface_returns_token_once_and_blocks_gui_mutation(tmp_path: Path, monkeypatch):
    service, project = make_service(tmp_path)
    project = add_media(service, project, tmp_path)
    clip = project.timeline.tracks[0].clips[0]
    monkeypatch.setattr("app.api.routes.service", service)
    client = TestClient(app)
    acquired = client.post(f"/api/projects/{project.id}/guards", json={
        "owner": {"type": "human", "id": "browser"},
        "scope": {"kind": "selection", "clip_ids": [clip.id], "marker_ids": [], "frame_ranges": []},
    })
    assert acquired.status_code == 200
    token = acquired.json()["guard_token"]
    assert "capability_sha256" not in acquired.text
    guard_state = (tmp_path / "runtime" / "guards" / f"{project.id}.json").read_text()
    assert token not in guard_state
    assert hashlib.sha256(token.encode()).hexdigest() in guard_state
    revision_metadata = json.dumps([record.metadata.__dict__ for record in service.store.reachable_revisions(project.id)[1]])
    assert token not in revision_metadata
    assert hashlib.sha256(token.encode()).hexdigest() not in revision_metadata
    assert token not in client.get(f"/api/projects/{project.id}/guards").text
    blocked = client.post(f"/api/projects/{project.id}/clips/move", json={
        "clip_id": clip.id, "timeline_start_frame": 10, "expected_revision": project.revision,
    })
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "GUARD_CONFLICT"
    assert token not in blocked.text
    allowed = client.post(f"/api/projects/{project.id}/clips/move", json={
        "clip_id": clip.id, "timeline_start_frame": 10, "expected_revision": project.revision,
        "guard_tokens": [token],
    })
    assert allowed.status_code == 200


def test_rest_lease_lifecycle_and_mcp_guard_annotations(tmp_path: Path, monkeypatch):
    service, project = make_service(tmp_path)
    monkeypatch.setattr("app.api.routes.service", service)
    client = TestClient(app)
    acquired = client.post(f"/api/projects/{project.id}/guards", json={
        "owner": {"type": "agent", "id": "lease"}, "scope": {"kind": "project"}, "ttl_seconds": 60,
    }).json()
    renew = client.post(f"/api/projects/{project.id}/guards/{acquired['guard_id']}/renew",
                        json={"guard_token": acquired["guard_token"], "ttl_seconds": 90})
    assert renew.status_code == 200
    assert "guard_token" not in renew.text
    wrong = client.post(f"/api/projects/{project.id}/guards/{acquired['guard_id']}/release",
                        json={"guard_token": "guard_token_wrong"})
    assert wrong.status_code == 403
    released = client.post(f"/api/projects/{project.id}/guards/{acquired['guard_id']}/release",
                           json={"guard_token": acquired["guard_token"]})
    assert released.json()["status"] == "released"
    assert client.post(f"/api/projects/{project.id}/guards/{acquired['guard_id']}/release",
                       json={"guard_token": acquired["guard_token"]}).json()["status"] == "not_active"

    from app.mcp_server import mcp
    expected = {
        "acquire_edit_guard": (False, False, False),
        "renew_edit_guard": (False, False, False),
        "release_edit_guard": (False, True, True),
        "list_edit_guards": (True, False, True),
    }
    for name, values in expected.items():
        annotations = mcp._tool_manager.get_tool(name).annotations
        assert (annotations.readOnlyHint, annotations.destructiveHint, annotations.idempotentHint) == values


def test_mcp_guard_conflict_error_is_structured_and_safe():
    from app.guard_models import GuardError
    from app.mcp_contracts import McpResult
    from app.mcp_server import _error

    result = McpResult.model_validate(_error(GuardError(
        "GUARD_CONFLICT", "This edit is protected by an active edit guard",
        conflicts=[{"guard_id": "guard_abc", "expires_at": "2030-01-01T00:00:00Z"}],
    )))
    assert result.error is not None
    assert result.error.code == "GUARD_CONFLICT"
    assert result.error.conflicts == [{"guard_id": "guard_abc", "expires_at": "2030-01-01T00:00:00Z"}]
    assert "owner" not in str(result)
