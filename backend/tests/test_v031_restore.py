from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import shutil

import pytest
from fastapi.testclient import TestClient

import app.api.routes as routes
import app.mcp_server as server_module
from app.domain.models import (
    Asset, AssetProductionMetadata, ClipProductionMetadata, ExternalReference,
    FrameRate, Marker, MarkerProductionMetadata, ValidationError, project_to_dict,
    project_from_dict,
)
from app.domain.operations import new_project, register_asset
from app.main import app
from app.mcp_server import mcp, restore_revision as mcp_restore_revision
from app.persistence.project_store import ProjectStore
from app.persistence.revisions import RevisionMetadata, revision_hash
from app.restore_models import RestoreRevisionRequest
from app.services.projects import ProjectService
from app.services.restore import RestoreError


def seeded_service(tmp_path):
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    project = new_project("Restore project")
    project.external_refs = [ExternalReference("production", "project-1", "project")]
    project.timeline.external_refs = [ExternalReference("production", "timeline-1", "timeline")]
    project = register_asset(
        project,
        Asset(
            "asset", "/missing/original-source.mp4", "source.mp4", "h264", 320, 180,
            FrameRate(24, 1), 120,
            AssetProductionMetadata(
                shot_ids=["shot-a"], dialogue_line_ids=["line-a"],
                generation_job_id="job-a",
                external_refs=[ExternalReference("production", "asset-1", "asset")],
            ),
        ),
    )
    project.timeline.tracks[0].clips[0].production = ClipProductionMetadata(
        shot_ids=["shot-a"], dialogue_line_ids=["line-a"],
        external_refs=[ExternalReference("production", "clip-1", "clip")],
    )
    service.store.save(project)
    return service, project


def state_dict(project):
    value = project_to_dict(project)
    value.pop("revision", None)
    value.pop("revision_id", None)
    return value


def rename(candidate, name):
    candidate.name = name
    return candidate


def test_restore_is_forward_only_and_restores_full_canonical_snapshot(tmp_path):
    service, project = seeded_service(tmp_path)
    base_id = project.revision_id

    target = service._commit_operation(
        project.id, 0,
        lambda candidate: _make_rich_target(candidate),
        "system", {"type": "system"}, "test_target", "Create restore target",
    )
    later = service._commit_operation(
        project.id, target.revision,
        lambda candidate: rename(candidate, "Later state"),
        "system", {"type": "system"}, "test_later", "Create later state",
    )
    head = service._commit_operation(
        project.id, later.revision,
        lambda candidate: rename(candidate, "HEAD state"),
        "system", {"type": "system"}, "test_head", "Create HEAD state",
    )
    target_snapshot = service.revision_record(project.id, target.revision_id).snapshot
    preserved = {
        revision_id: (service.store.directory_for(project.id) / "revisions" / f"{revision_id}.json").read_bytes()
        for revision_id in (base_id, target.revision_id, later.revision_id, head.revision_id)
    }

    result = service.restore_revision(
        project.id, target.revision_id,
        {"expected_revision": head.revision, "expected_revision_id": head.revision_id},
        origin="rest", actor={"type": "human"},
    )

    assert result.status == "restored"
    assert result.revision == head.revision + 1
    assert result.parent_revision_id == head.revision_id
    assert result.restored_from_revision_id == target.revision_id
    assert result.project_id == project.id
    assert state_dict(service.get(project.id)) == state_dict(project_from_dict(target_snapshot))
    assert service.get(project.id).timeline.id == project.timeline.id
    for revision_id, contents in preserved.items():
        path = service.store.directory_for(project.id) / "revisions" / f"{revision_id}.json"
        assert path.read_bytes() == contents

    available, records = service.revision_records(project.id)
    assert available is True
    assert [record.metadata.revision_id for record in records] == [
        result.revision_id, head.revision_id, later.revision_id, target.revision_id, base_id,
    ]
    assert records[0].metadata.operation == "restore"
    assert records[0].metadata.restored_from_revision_id == target.revision_id

    restored_again = service.restore_revision(
        project.id, head.revision_id,
        {"expected_revision": result.revision, "expected_revision_id": result.revision_id},
        origin="mcp", actor={"type": "agent"},
    )
    assert restored_again.revision == result.revision + 1
    assert restored_again.parent_revision_id == result.revision_id
    assert restored_again.restored_from_revision_id == head.revision_id


def _make_rich_target(candidate):
    candidate.name = "Historical target"
    candidate.fps = FrameRate(30, 1)
    asset = candidate.assets[0]
    asset.path = "/historical/missing-target.mov"
    asset.name = "historical.mov"
    asset.codec = "hevc"
    asset.width = 640
    asset.height = 360
    asset.fps = FrameRate(30, 1)
    asset.duration_frames = 180
    asset.production = AssetProductionMetadata(
        shot_ids=["shot-b"], dialogue_line_ids=["line-b"], generation_job_id="job-b",
        external_refs=[ExternalReference("production", "asset-b", "asset")],
    )
    candidate.timeline.name = "Historical timeline"
    candidate.timeline.external_refs = [ExternalReference("production", "timeline-b", "timeline")]
    track = candidate.timeline.tracks[0]
    track.name = "Historical V1"
    clip = track.clips[0]
    clip.source_in_frame = 10
    clip.source_out_frame = 150
    clip.timeline_start_frame = 7
    clip.production = ClipProductionMetadata(
        shot_ids=["shot-b"], dialogue_line_ids=["line-b"],
        external_refs=[ExternalReference("production", "clip-b", "clip")],
    )
    candidate.timeline.markers = [Marker(
        "marker_0123456789abcdef0123456789abcdef", 12, 30, "Historical marker",
        "description", "shot", MarkerProductionMetadata(
            shot_ids=["shot-b"], dialogue_line_ids=["line-b"],
            external_refs=[ExternalReference("production", "marker-b", "marker")],
        ),
    )]
    return candidate


def test_restore_diff_matches_preview_and_historical_diff(tmp_path):
    service, project = seeded_service(tmp_path)
    target = service.split(project.id, project.timeline.tracks[0].clips[0].id, 40, 0)
    head = service.move(project.id, target.timeline.tracks[0].clips[0].id, 120, target.revision)
    preview = service.diff_revisions(project.id, head.revision_id, target.revision_id)

    restored = service.restore_revision(
        project.id, target.revision_id,
        {"expected_revision": head.revision, "expected_revision_id": head.revision_id},
        origin="rest", actor={"type": "human"},
    )
    historical = service.diff_revisions(project.id, head.revision_id, restored.revision_id)
    assert restored.diff.summary == preview.summary == historical.summary
    assert restored.diff.changes == preview.changes == historical.changes
    assert "/Users/" not in json.dumps(restored.model_dump(mode="json"))
    assert "/var/" not in json.dumps(restored.model_dump(mode="json"))


def test_restore_no_changes_for_current_or_older_identical_state(tmp_path):
    service, project = seeded_service(tmp_path)
    changed = service._commit_operation(project.id, 0, lambda candidate: rename(candidate, "Changed"),
                                        "system", {"type": "system"}, "test_change", "Change state")
    identical = service._commit_operation(project.id, changed.revision,
                                          lambda candidate: rename(candidate, project.name),
                                          "system", {"type": "system"}, "test_revert", "Return state")
    before = sorted(path.read_bytes() for path in service.store.directory_for(project.id).joinpath("revisions").glob("*.json"))
    with pytest.raises(RestoreError) as current:
        service.restore_revision(project.id, identical.revision_id,
                                 {"expected_revision": identical.revision, "expected_revision_id": identical.revision_id},
                                 origin="rest", actor={"type": "human"})
    assert current.value.code == "NO_CHANGES"
    with pytest.raises(RestoreError) as older:
        service.restore_revision(project.id, project.revision_id,
                                 {"expected_revision": identical.revision, "expected_revision_id": identical.revision_id},
                                 origin="rest", actor={"type": "human"})
    assert older.value.code == "NO_CHANGES"
    assert service.get(project.id).revision == identical.revision
    assert sorted(path.read_bytes() for path in service.store.directory_for(project.id).joinpath("revisions").glob("*.json")) == before


def test_restore_target_resolution_is_reachable_only_and_legacy_is_non_mutating(tmp_path):
    service, project = seeded_service(tmp_path)
    revision_path = next(service.store.directory_for(project.id).joinpath("revisions").glob("*.json"))
    orphan = service.store.directory_for(project.id) / "revisions" / "revision_orphan.json"
    orphan.write_bytes(revision_path.read_bytes())
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    with pytest.raises(RestoreError) as missing:
        service.restore_revision(project.id, "revision_orphan",
                                 {"expected_revision": project.revision, "expected_revision_id": project.revision_id},
                                 origin="rest", actor={"type": "human"})
    assert missing.value.code == "REVISION_NOT_FOUND"
    with pytest.raises(RestoreError) as malformed:
        service.restore_revision(project.id, "not-a-revision",
                                 {"expected_revision": project.revision, "expected_revision_id": project.revision_id},
                                 origin="rest", actor={"type": "human"})
    assert malformed.value.code == "INVALID_ARGUMENT"
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before

    legacy_root = tmp_path / "legacy"
    legacy_service, legacy_project = seeded_service(legacy_root)
    directory = legacy_service.store.directory_for(legacy_project.id)
    flat = legacy_service.store.path_for(legacy_project.id)
    flat.write_text(json.dumps(project_to_dict(legacy_project)))
    shutil.rmtree(directory)
    with pytest.raises(RestoreError) as unavailable:
        legacy_service.restore_revision(legacy_project.id, legacy_project.revision_id,
                                        {"expected_revision": legacy_project.revision, "expected_revision_id": legacy_project.revision_id},
                                        origin="rest", actor={"type": "human"})
    assert unavailable.value.code == "HISTORY_UNAVAILABLE"
    assert flat.is_file()
    assert not directory.exists()


def test_restore_rest_mcp_parity_and_strict_request(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    target = service._commit_operation(project.id, 0, lambda candidate: rename(candidate, "Target"),
                                       "system", {"type": "system"}, "test_target", "Target")
    head = service._commit_operation(project.id, target.revision, lambda candidate: rename(candidate, "Head"),
                                     "system", {"type": "system"}, "test_head", "Head")
    monkeypatch.setattr(routes, "service", service)
    client = TestClient(app)
    response = client.post(
        f"/api/projects/{project.id}/revisions/{target.revision_id}/restore",
        json={"expected_revision": head.revision, "expected_revision_id": head.revision_id},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "restored"
    assert response.json()["restored_from_revision_id"] == target.revision_id

    other_service, other_project = seeded_service(tmp_path / "mcp")
    other_target = other_service._commit_operation(other_project.id, 0, lambda candidate: rename(candidate, "Target"),
                                                   "system", {"type": "system"}, "test_target", "Target")
    other_head = other_service._commit_operation(other_project.id, other_target.revision, lambda candidate: rename(candidate, "Head"),
                                                 "system", {"type": "system"}, "test_head", "Head")
    monkeypatch.setattr(server_module, "application", type("App", (), {"projects": other_service})())
    mcp_result = mcp_restore_revision(other_project.id, other_target.revision_id, other_head.revision, other_head.revision_id)
    assert mcp_result["status"] == "restored"
    assert mcp_result["diff"] == response.json()["diff"]
    strict = client.post(
        f"/api/projects/{project.id}/revisions/{target.revision_id}/restore",
        json={"expected_revision": 999, "expected_revision_id": response.json()["revision_id"], "extra": True},
    )
    assert strict.status_code == 422
    assert len(mcp._tool_manager.list_tools()) == 23


def test_restore_rest_errors_use_stable_codes_and_no_raw_paths(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    target = service._commit_operation(project.id, 0, lambda candidate: rename(candidate, "Target"),
                                       "system", {"type": "system"}, "test_target", "Target")
    head = service._commit_operation(project.id, target.revision, lambda candidate: rename(candidate, "Head"),
                                     "system", {"type": "system"}, "test_head", "Head")
    monkeypatch.setattr(routes, "service", service)
    client = TestClient(app)
    stale = client.post(
        f"/api/projects/{project.id}/revisions/{target.revision_id}/restore",
        json={"expected_revision": head.revision, "expected_revision_id": target.revision_id},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "REVISION_CONFLICT"
    missing = client.post(
        f"/api/projects/{project.id}/revisions/revision_missing/restore",
        json={"expected_revision": head.revision, "expected_revision_id": head.revision_id},
    )
    assert missing.status_code == 404
    assert missing.json()["detail"] == {"code": "REVISION_NOT_FOUND", "message": "Revision does not exist"}

    record_path = service.store.directory_for(project.id) / "revisions" / f"{head.revision_id}.json"
    record = json.loads(record_path.read_text())
    record["metadata"]["snapshot_sha256"] = "0" * 64
    record_path.write_text(json.dumps(record))
    integrity = client.post(
        f"/api/projects/{project.id}/revisions/{target.revision_id}/restore",
        json={"expected_revision": head.revision, "expected_revision_id": head.revision_id},
    )
    body = integrity.text
    assert integrity.status_code == 500
    assert integrity.json()["detail"] == {"code": "INTEGRITY_ERROR", "message": "Project integrity validation failed"}
    assert "/Users/" not in body and "/var/" not in body
    assert str(service.store.directory_for(project.id)) not in body
    assert record_path.name not in body


def test_restore_conflict_and_concurrent_same_base_have_one_winner(tmp_path):
    service, project = seeded_service(tmp_path)
    target = service._commit_operation(project.id, 0, lambda candidate: rename(candidate, "Target"),
                                       "system", {"type": "system"}, "test_target", "Target")
    head = service._commit_operation(project.id, target.revision, lambda candidate: rename(candidate, "Head"),
                                     "system", {"type": "system"}, "test_head", "Head")
    args = (project.id, target.revision_id, {"expected_revision": head.revision, "expected_revision_id": head.revision_id})

    def attempt():
        try:
            return service.restore_revision(*args, origin="mcp", actor={"type": "agent"})
        except RestoreError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(not isinstance(outcome, RestoreError) for outcome in outcomes) == 1
    conflict = next(outcome for outcome in outcomes if isinstance(outcome, RestoreError))
    assert conflict.code == "REVISION_CONFLICT"
    assert service.get(project.id).revision == head.revision + 1
    assert len(service.revision_records(project.id)[1]) == 4


def test_restore_persistence_failure_cleans_candidate_and_preserves_head(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    target = service._commit_operation(project.id, 0, lambda candidate: rename(candidate, "Target"),
                                       "system", {"type": "system"}, "test_target", "Target")
    head = service._commit_operation(project.id, target.revision, lambda candidate: rename(candidate, "Head"),
                                     "system", {"type": "system"}, "test_head", "Head")
    directory = service.store.directory_for(project.id)
    head_bytes = (directory / "head.json").read_bytes()
    before = sorted(path.name for path in (directory / "revisions").glob("*.json"))
    original = service.store._write_head

    def fail_after_write(*args):
        original(*args)
        raise OSError("local head failure")

    monkeypatch.setattr(service.store, "_write_head", fail_after_write)
    with pytest.raises(RestoreError) as error:
        service.restore_revision(project.id, target.revision_id,
                                 {"expected_revision": head.revision, "expected_revision_id": head.revision_id},
                                 origin="rest", actor={"type": "human"})
    assert error.value.code == "PERSISTENCE_ERROR"
    assert (directory / "head.json").read_bytes() == head_bytes
    assert sorted(path.name for path in (directory / "revisions").glob("*.json")) == before
    assert service.get(project.id).revision == head.revision


def test_restore_integrity_errors_are_sanitized_and_provenance_is_validated(tmp_path):
    service, project = seeded_service(tmp_path)
    target = service._commit_operation(project.id, 0, lambda candidate: rename(candidate, "Target"),
                                       "system", {"type": "system"}, "test_target", "Target")
    head = service._commit_operation(project.id, target.revision, lambda candidate: rename(candidate, "Head"),
                                     "system", {"type": "system"}, "test_head", "Head")
    directory = service.store.directory_for(project.id)
    head_path = directory / "head.json"
    record_path = directory / "revisions" / f"{head.revision_id}.json"
    record = json.loads(record_path.read_text())
    record["metadata"]["snapshot_sha256"] = "0" * 64
    record_path.write_text(json.dumps(record))
    with pytest.raises(RestoreError) as error:
        service.restore_revision(project.id, target.revision_id,
                                 {"expected_revision": head.revision, "expected_revision_id": head.revision_id},
                                 origin="rest", actor={"type": "human"})
    assert error.value.code == "INTEGRITY_ERROR"
    assert "/Users/" not in error.value.message
    assert "/var/" not in error.value.message
    assert str(directory) not in error.value.message
    assert head_path.name not in error.value.message

    service, project = seeded_service(tmp_path / "provenance")
    target = service._commit_operation(project.id, 0, lambda candidate: rename(candidate, "Target"),
                                       "system", {"type": "system"}, "test_target", "Target")
    head = service._commit_operation(project.id, target.revision, lambda candidate: rename(candidate, "Head"),
                                     "system", {"type": "system"}, "test_head", "Head")
    record_path = service.store.directory_for(project.id) / "revisions" / f"{head.revision_id}.json"
    record = json.loads(record_path.read_text())
    record["metadata"]["restored_from_revision_id"] = "revision_not_reachable"
    snapshot_project = project_from_dict(record["snapshot"])
    unsigned = RevisionMetadata(**{**record["metadata"], "snapshot_sha256": ""})
    record["metadata"]["snapshot_sha256"] = revision_hash(unsigned, snapshot_project)
    record_path.write_text(json.dumps(record))
    with pytest.raises(ValidationError, match="Restore provenance"):
        service.store.load(project.id)


def test_transaction_after_restore_and_restore_of_transaction_revision(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    prepared = service.prepare_transaction(project.id, {
        "expected_revision": project.revision,
        "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": clip_id}, "timeline_start_frame": 10}],
    })
    transaction = service.commit_transaction(project.id, {
        "transaction_hash": prepared.transaction_hash,
        "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json"),
    })
    restored = service.restore_revision(
        project.id, project.revision_id,
        {"expected_revision": transaction.revision, "expected_revision_id": transaction.revision_id},
        origin="rest", actor={"type": "human"},
    )
    assert restored.restored_from_revision_id == project.revision_id
    clip = service.get(project.id).timeline.tracks[0].clips[0]
    follow_up = service.move(project.id, clip.id, 5, restored.revision)
    later_restore = service.restore_revision(
        project.id, restored.revision_id,
        {"expected_revision": follow_up.revision, "expected_revision_id": follow_up.revision_id},
        origin="mcp", actor={"type": "agent"},
    )
    assert later_restore.restored_from_revision_id == restored.revision_id
    assert later_restore.revision == follow_up.revision + 1
