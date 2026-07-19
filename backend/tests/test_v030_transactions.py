from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import shutil
import pytest
from fastapi.testclient import TestClient

import app.api.routes as routes
from app.domain.models import Asset, FrameRate, ValidationError
from app.domain.operations import new_project, register_asset
from app.main import app
from app.mcp_server import commit_transaction as mcp_commit_transaction
from app.mcp_server import prepare_transaction as mcp_prepare_transaction
from app.persistence.project_store import ProjectStore
from app.services.projects import ProjectService
from app.services.transactions import TransactionError


def seeded_service(tmp_path):
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    project = register_asset(
        new_project("Transaction project"),
        Asset("asset", "/Users/example/source.mp4", "source.mp4", "h264", 320, 180, FrameRate(24, 1), 120),
    )
    service.store.save(project)
    return service, project


def split_move_request(project, clip_id):
    return {
        "expected_revision": project.revision,
        "operations": [
            {"op": "split_clip", "clip": {"kind": "id", "id": clip_id}, "timeline_frame": 40, "result_ref": "right"},
            {"op": "move_clip", "clip": {"kind": "result", "ref": "right"}, "timeline_start_frame": 60},
        ],
    }


def test_prepare_is_deterministic_and_side_effect_free(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    request = split_move_request(project, clip_id)
    before_files = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    first = service.prepare_transaction(project.id, request)
    second = service.prepare_transaction(project.id, request)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert service.store.load(project.id).revision == project.revision
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before_files


def test_split_move_commit_has_one_revision_and_matches_revision_diff(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    prepared = service.prepare_transaction(project.id, split_move_request(project, clip_id))
    committed = service.commit_transaction(project.id, {
        "transaction_hash": prepared.transaction_hash,
        "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json"),
    })

    assert committed.revision == 1
    assert committed.parent_revision_id == project.revision_id
    assert committed.operation_results == prepared.operation_results
    assert committed.diff == prepared.diff
    history_diff = service.diff_revisions(project.id, project.revision_id, committed.revision_id)
    assert history_diff.summary == committed.diff.summary
    assert history_diff.changes == committed.diff.changes
    records = service.revision_records(project.id)[1]
    assert records[0].metadata.operation == "transaction"
    assert records[0].metadata.summary == "Apply transaction (2 operations)"
    assert len(records) == 2


def test_marker_add_update_result_reference_commits_once(tmp_path):
    service, project = seeded_service(tmp_path)
    request = {
        "expected_revision": 0,
        "operations": [
            {"op": "add_marker", "result_ref": "marker", "start_frame": 20, "name": "Shot one"},
            {"op": "update_marker", "marker": {"kind": "result", "ref": "marker"},
             "changes": {"description": "Updated"}},
        ],
    }
    prepared = service.prepare_transaction(project.id, request)
    marker_id = prepared.prepared_transaction.operations[0].marker_id
    committed = service.commit_transaction(project.id, {
        "transaction_hash": prepared.transaction_hash,
        "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json"),
    })
    marker = committed.timeline["markers"][0]
    assert marker["id"] == marker_id
    assert marker["description"] == "Updated"
    assert committed.revision == 1


@pytest.mark.parametrize("payload, cause", [
    ({"expected_revision": 0, "operations": []}, "INVALID_REQUEST"),
    ({"expected_revision": 0, "operations": [{"op": "move_clip", "clip": {"kind": "result", "ref": "later"}, "timeline_start_frame": 20}]}, "UNKNOWN_RESULT_REF"),
])
def test_prepare_rejects_strict_invalid_transaction(payload, cause, tmp_path):
    service, project = seeded_service(tmp_path)
    with pytest.raises(TransactionError) as exc:
        service.prepare_transaction(project.id, payload)
    assert exc.value.code == "INVALID_TRANSACTION"
    assert exc.value.cause_code == cause


def test_prepare_rejects_wrong_entity_type_and_duplicate_result_ref(tmp_path):
    service, project = seeded_service(tmp_path)
    marker_request = {"expected_revision": 0, "operations": [
        {"op": "add_marker", "result_ref": "same", "start_frame": 10, "name": "A"},
        {"op": "add_marker", "result_ref": "same", "start_frame": 20, "name": "B"},
    ]}
    with pytest.raises(TransactionError, match="duplicated"):
        service.prepare_transaction(project.id, marker_request)
    clip_id = project.timeline.tracks[0].clips[0].id
    wrong_type = {"expected_revision": 0, "operations": [
        {"op": "add_marker", "result_ref": "marker", "start_frame": 10, "name": "A"},
        {"op": "move_clip", "clip": {"kind": "result", "ref": "marker"}, "timeline_start_frame": 20},
    ]}
    with pytest.raises(TransactionError) as exc:
        service.prepare_transaction(project.id, wrong_type)
    assert exc.value.cause_code == "WRONG_ENTITY_TYPE"
    assert clip_id


def test_tampered_hash_and_stale_commit_do_not_mutate(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    prepared = service.prepare_transaction(project.id, split_move_request(project, clip_id))
    payload = {"transaction_hash": "0" * 64, "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json")}
    with pytest.raises(TransactionError) as exc:
        service.commit_transaction(project.id, payload)
    assert exc.value.cause_code == "HASH_MISMATCH"
    current = service.move(project.id, clip_id, 10, 0)
    with pytest.raises(TransactionError) as exc:
        service.commit_transaction(project.id, {
            "transaction_hash": prepared.transaction_hash,
            "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json"),
        })
    assert exc.value.code == "REVISION_CONFLICT"
    assert exc.value.current_revision == current.revision


def test_rest_and_mcp_transaction_surfaces_are_logically_equivalent(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    monkeypatch.setattr(routes, "service", service)
    clip_id = project.timeline.tracks[0].clips[0].id
    request = split_move_request(project, clip_id)
    rest = TestClient(app).post(f"/api/projects/{project.id}/transactions/prepare", json=request)
    assert rest.status_code == 200
    monkeypatch.setattr("app.mcp_server.application", type("App", (), {"projects": service})())
    mcp = mcp_prepare_transaction(project.id, request["expected_revision"], request["operations"])
    assert rest.json() == mcp
    committed = mcp_commit_transaction(project.id, mcp["transaction_hash"], mcp["prepared_transaction"])
    assert committed["status"] == "committed"
    assert committed["revision"] == 1
    assert service.store.load(project.id).revision == 1


def test_failed_transaction_does_not_write_or_promote_legacy_project(tmp_path):
    service, project = seeded_service(tmp_path)
    # Make a flat legacy copy with the same canonical bytes and remove the directory.
    directory = service.store.directory_for(project.id)
    record = next((directory / "revisions").glob("*.json"))
    snapshot = __import__("json").loads(record.read_text())["snapshot"]
    flat = service.store.path_for(project.id)
    flat.write_text(json.dumps(snapshot))
    shutil.rmtree(directory)
    clip_id = project.timeline.tracks[0].clips[0].id
    request = {"expected_revision": 0, "operations": [
        {"op": "move_clip", "clip": {"kind": "id", "id": clip_id}, "timeline_start_frame": 10},
        {"op": "delete_marker", "marker": {"kind": "id", "id": "marker_missing"}},
    ]}
    with pytest.raises(TransactionError):
        service.prepare_transaction(project.id, request)
    assert flat.is_file()
    assert not directory.exists()


def test_successful_legacy_transaction_promotes_baseline_then_advances_once(tmp_path):
    service, project = seeded_service(tmp_path)
    directory = service.store.directory_for(project.id)
    record = next(directory.joinpath("revisions").glob("*.json"))
    snapshot = json.loads(record.read_text())["snapshot"]
    flat = service.store.path_for(project.id)
    flat.write_text(json.dumps(snapshot))
    shutil.rmtree(directory)
    clip_id = project.timeline.tracks[0].clips[0].id
    prepared = service.prepare_transaction(project.id, {
        "expected_revision": 0,
        "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": clip_id}, "timeline_start_frame": 10}],
    })
    committed = service.commit_transaction(project.id, {
        "transaction_hash": prepared.transaction_hash,
        "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json"),
    })
    assert committed.revision == 1
    assert flat.is_file()
    available, records = service.revision_records(project.id)
    assert available is True
    assert [record.metadata.operation for record in records] == ["transaction", "migration"]


def test_transaction_head_failure_removes_unreachable_candidate(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    prepared = service.prepare_transaction(project.id, {
        "expected_revision": 0,
        "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": clip_id}, "timeline_start_frame": 10}],
    })
    before = sorted((service.store.directory_for(project.id) / "revisions").glob("*.json"))
    original_write_head = service.store._write_head

    def fail_after_head(*args):
        original_write_head(*args)
        raise OSError("head failure")

    monkeypatch.setattr(service.store, "_write_head", fail_after_head)
    with pytest.raises(TransactionError) as exc:
        service.commit_transaction(project.id, {
            "transaction_hash": prepared.transaction_hash,
            "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json"),
        })
    assert exc.value.code == "PERSISTENCE_ERROR"
    assert sorted((service.store.directory_for(project.id) / "revisions").glob("*.json")) == before
    assert service.store.load(project.id).revision == 0


def test_rest_transaction_integrity_error_is_sanitized(tmp_path, monkeypatch):
    service, project = seeded_service(tmp_path)
    monkeypatch.setattr(routes, "service", service)
    monkeypatch.setattr(service.store, "load_with_source",
                        lambda _project_id: (_ for _ in ()).throw(
                            ValidationError("Invalid directory-backed project: /Users/private/project: digest mismatch")))
    response = TestClient(app).post(f"/api/projects/{project.id}/transactions/prepare", json={
        "expected_revision": 0,
        "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": "clip_missing"}, "timeline_start_frame": 10}],
    })
    assert response.status_code == 500
    assert response.json()["detail"] == {"code": "INTEGRITY_ERROR", "message": "Project integrity validation failed"}
    assert "/Users/private/project" not in response.text


def test_two_commits_of_one_prepared_transaction_have_one_winner(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    prepared = service.prepare_transaction(project.id, {
        "expected_revision": 0,
        "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": clip_id}, "timeline_start_frame": 10}],
    })
    payload = {"transaction_hash": prepared.transaction_hash,
               "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json")}

    def attempt():
        try:
            return service.commit_transaction(project.id, payload)
        except TransactionError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(not isinstance(outcome, TransactionError) for outcome in outcomes) == 1
    conflict = next(outcome for outcome in outcomes if isinstance(outcome, TransactionError))
    assert conflict.code == "REVISION_CONFLICT"
    assert service.store.load(project.id).revision == 1
    assert len(service.revision_records(project.id)[1]) == 2
