from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from app.domain.models import Asset, Clip, FrameRate, TimelineConflictError, project_from_dict, project_to_dict
from app.domain.operations import add_track, move_clip, new_project, register_asset
from app.persistence.migrations import migrate_v2_to_v3
from app.persistence.project_store import ProjectStore
from app.persistence.revisions import RevisionMetadata, RevisionRecord, revision_hash
from app.rendering.plan import compile_render_plan
from app.services.projects import ProjectService
from app.services.transactions import TransactionError


def seeded(tmp_path):
    service = ProjectService(ProjectStore(tmp_path))
    project = register_asset(
        new_project("Multi-track"),
        Asset("asset", "/tmp/source.mp4", "source.mp4", "h264", 320, 180, FrameRate(24, 1), 120),
    )
    service.store.save(project)
    return service, project


def test_v2_migration_is_deterministic_and_preserves_order_and_ids():
    source = {
        "schema_version": 2, "id": "project_v2", "name": "Legacy", "fps": {"numerator": 24, "denominator": 1},
        "revision": 3, "revision_id": "revision_legacy", "external_refs": [],
        "assets": [{"id": "asset_v2", "path": "/tmp/a.mp4", "name": "a", "codec": "h264", "width": 320, "height": 180,
                    "fps": {"numerator": 24, "denominator": 1}, "duration_frames": 100,
                    "production": {"shot_ids": [], "dialogue_line_ids": [], "generation_job_id": None, "external_refs": []}}],
        "timeline": {"id": "timeline_v2", "name": "Main", "external_refs": [], "tracks": [
            {"id": "track_v2", "name": "V1", "kind": "video", "clips": [
                {"id": "clip_b", "asset_id": "asset_v2", "source_in_frame": 20, "source_out_frame": 40, "timeline_start_frame": 40,
                 "production": {"shot_ids": [], "dialogue_line_ids": [], "external_refs": []}},
                {"id": "clip_a", "asset_id": "asset_v2", "source_in_frame": 0, "source_out_frame": 20, "timeline_start_frame": 0,
                 "production": {"shot_ids": [], "dialogue_line_ids": [], "external_refs": []}},
            ]}], "markers": []},
    }
    first = migrate_v2_to_v3(source)
    second = migrate_v2_to_v3(source)
    assert first == second
    assert [clip["id"] for clip in first["timeline"]["tracks"][0]["clips"]] == ["clip_a", "clip_b"]
    assert first["timeline"]["video_canvas"] == {"width": 320, "height": 180}
    assert project_to_dict(project_from_dict(source)) == project_to_dict(project_from_dict(source))
    tampered = copy.deepcopy(source)
    tampered["timeline"]["tracks"][0]["clips"][0]["timeline_start_frame"] = 41
    assert migrate_v2_to_v3(tampered)["timeline"]["tracks"][0]["clips"][0]["timeline_start_frame"] == 0


def test_v2_revision_digest_authenticates_raw_bytes_before_migration():
    snapshot = {"schema_version": 2, "id": "project_raw", "name": "Raw", "fps": None, "revision": 0,
                "revision_id": "revision_raw", "external_refs": [], "assets": [],
                "timeline": {"id": "timeline_raw", "name": "Main", "external_refs": [], "tracks": [], "markers": []}}
    metadata = RevisionMetadata("project_raw", "revision_raw", 0, None, "2026-01-01T00:00:00Z", "system", {"type": "system"}, "migration", "Raw", "")
    metadata = replace(metadata, snapshot_sha256=revision_hash(metadata, snapshot))
    record = RevisionRecord.from_dict({"metadata": {**metadata.__dict__, "revision_number": 0}, "snapshot": snapshot})
    assert record.snapshot == snapshot
    tampered = copy.deepcopy(snapshot)
    tampered["name"] = "Tampered"
    with pytest.raises(Exception, match="digest"):
        RevisionRecord.from_dict({"metadata": metadata.__dict__, "snapshot": tampered})


def test_cross_track_overlap_is_allowed_but_target_collision_is_rejected(tmp_path):
    service, project = seeded(tmp_path)
    multi = service.add_track(project.id, 0, "V2")
    clip = project.timeline.tracks[0].clips[0]
    moved = service.move(project.id, clip.id, 0, multi.revision, target_track_id=multi.timeline.tracks[1].id)
    assert [len(track.clips) for track in moved.timeline.tracks] == [0, 1]
    assert moved.timeline.tracks[1].clips[0].id == clip.id
    collision = copy.deepcopy(moved)
    collision.timeline.tracks[1].clips.append(Clip("clip_2", "asset", 60, 80, 60))
    collision.timeline.tracks[1].clips.sort(key=lambda item: (item.timeline_start_frame, item.id))
    with pytest.raises(TimelineConflictError):
        move_clip(collision, clip.id, 60, collision.timeline.tracks[1].id)


def test_v2_transaction_add_track_result_ref_moves_clip(tmp_path):
    service, project = seeded(tmp_path)
    clip_id = project.timeline.tracks[0].clips[0].id
    prepared = service.prepare_transaction(project.id, {
        "contract_version": 2, "expected_revision": project.revision,
        "operations": [
            {"op": "add_track", "result_ref": "new_track", "name": "Overlay"},
            {"op": "move_clip", "clip": {"kind": "id", "id": clip_id}, "timeline_start_frame": 12,
             "target_track_id": {"kind": "result", "ref": "new_track"}},
        ],
    })
    committed = service.commit_transaction(project.id, {
        "transaction_hash": prepared.transaction_hash,
        "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json"),
    })
    assert committed.diff == prepared.diff
    assert len(committed.timeline["tracks"][1]["clips"]) == 1
    assert committed.timeline["tracks"][1]["clips"][0]["timeline_start_frame"] == 12


def test_v2_same_frame_cross_track_move_prepares_commits_and_matches_history_diff(tmp_path):
    service, project = seeded(tmp_path)
    multi = service.add_track(project.id, project.revision, "V2")
    clip = multi.timeline.tracks[0].clips[0]
    base_revision_id = multi.revision_id
    target_track = multi.timeline.tracks[1]
    request = {
        "contract_version": 2,
        "expected_revision": multi.revision,
        "operations": [{
            "op": "move_clip", "clip": {"kind": "id", "id": clip.id},
            "timeline_start_frame": 0, "target_track_id": {"kind": "id", "id": target_track.id},
        }],
    }

    prepared = service.prepare_transaction(multi.id, request)
    prepared_operation = prepared.prepared_transaction.operations[0]
    assert prepared_operation.target_track_id == target_track.id

    committed = service.commit_transaction(multi.id, {
        "transaction_hash": prepared.transaction_hash,
        "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json"),
    })
    historical = service.diff_revisions(multi.id, base_revision_id, committed.revision_id)
    assert prepared.diff == committed.diff
    assert committed.diff.summary == historical.summary
    assert committed.diff.changes == historical.changes
    assert committed.revision == multi.revision + 1
    assert len(committed.timeline["tracks"]) == 2
    assert committed.timeline["tracks"][0]["clips"] == []
    moved_clip = committed.timeline["tracks"][1]["clips"][0]
    assert moved_clip["id"] == clip.id
    assert moved_clip["timeline_start_frame"] == 0
    assert "/track_id" in {field.path for field in committed.diff.changes.clips.modified[0].fields}


def test_v2_move_noop_rules_and_same_track_frame_move(tmp_path):
    service, project = seeded(tmp_path)
    multi = service.add_track(project.id, project.revision, "V2")
    clip = multi.timeline.tracks[0].clips[0]
    current_track = multi.timeline.tracks[0]

    for target_track_id in (current_track.id, None):
        request = {
            "contract_version": 2,
            "expected_revision": multi.revision,
            "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": clip.id},
                             "timeline_start_frame": 0}],
        }
        if target_track_id is not None:
            request["operations"][0]["target_track_id"] = {"kind": "id", "id": target_track_id}
        with pytest.raises(TransactionError) as no_change:
            service.prepare_transaction(multi.id, request)
        assert no_change.value.code == "NO_CHANGES"

    valid = service.prepare_transaction(multi.id, {
        "contract_version": 2,
        "expected_revision": multi.revision,
        "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": clip.id},
                         "timeline_start_frame": 10, "target_track_id": {"kind": "id", "id": current_track.id}}],
    })
    assert valid.diff.changes.clips.modified[0].fields[0].path == "/timeline_start_frame"


def test_v2_cross_track_collision_is_rejected_without_mutation(tmp_path):
    service, project = seeded(tmp_path)
    multi = service.add_track(project.id, project.revision, "V2")
    first = multi.timeline.tracks[0].clips[0]
    moved = service.move(project.id, first.id, 0, multi.revision, target_track_id=multi.timeline.tracks[1].id)
    with_second = service._commit_operation(
        project.id, moved.revision,
        lambda candidate: register_asset(candidate, Asset(
            "asset_2", "/tmp/second.mp4", "second.mp4", "h264", 320, 180, FrameRate(24, 1), 60,
        ), timeline_start_frame=120),
        "system", {"type": "system"}, "import_media", "Add second media",
    )
    second = with_second.timeline.tracks[0].clips[0]
    request = {
        "contract_version": 2,
        "expected_revision": with_second.revision,
        "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": second.id},
                         "timeline_start_frame": 0,
                         "target_track_id": {"kind": "id", "id": with_second.timeline.tracks[1].id}}],
    }
    with pytest.raises(TransactionError) as collision:
        service.prepare_transaction(project.id, request)
    assert collision.value.cause_code == "TIMELINE_CONFLICT"
    assert service.get(project.id).revision == with_second.revision


def test_v2_same_frame_cross_track_move_is_guarded_at_commit(tmp_path):
    service, project = seeded(tmp_path)
    multi = service.add_track(project.id, project.revision, "V2")
    clip = multi.timeline.tracks[0].clips[0]
    request = {
        "contract_version": 2,
        "expected_revision": multi.revision,
        "operations": [{"op": "move_clip", "clip": {"kind": "id", "id": clip.id},
                         "timeline_start_frame": 0,
                         "target_track_id": {"kind": "id", "id": multi.timeline.tracks[1].id}}],
    }
    prepared = service.prepare_transaction(project.id, request)
    guard = service.guards.acquire(project.id, {"type": "agent", "id": "reviewer"},
                                   {"kind": "selection", "clip_ids": [clip.id], "marker_ids": [], "frame_ranges": []})
    payload = {"transaction_hash": prepared.transaction_hash,
               "prepared_transaction": prepared.prepared_transaction.model_dump(mode="json")}
    with pytest.raises(TransactionError) as blocked:
        service.commit_transaction(project.id, payload)
    assert blocked.value.code == "GUARD_CONFLICT"
    assert service.get(project.id).revision == multi.revision
    committed = service.commit_transaction(project.id, {**payload, "guard_tokens": [guard["guard_token"]]})
    assert committed.status == "committed"
    assert committed.revision == multi.revision + 1
    assert committed.timeline["tracks"][1]["clips"][0]["id"] == clip.id


def test_render_plan_uses_persisted_canvas_and_global_layer_duration(tmp_path):
    service, project = seeded(tmp_path)
    second = service.add_track(project.id, 0, "Overlay")
    clip = project.timeline.tracks[0].clips[0]
    moved = service.move(project.id, clip.id, 20, second.revision, target_track_id=second.timeline.tracks[1].id)
    plan = compile_render_plan(moved)
    assert plan.width == 320 and plan.height == 180
    assert plan.duration_frames == 140
    assert [layer.track_id for layer in plan.layers] == [track.id for track in moved.timeline.tracks]
    assert plan.audio_sources[0].clip_id == clip.id


def test_track_structure_and_cross_track_move_appear_in_diff(tmp_path):
    service, project = seeded(tmp_path)
    added = service.add_track(project.id, 0, "Overlay", external_refs=[{"system": "edit", "id": "v2", "kind": "track"}])
    clip = project.timeline.tracks[0].clips[0]
    moved = service.move(project.id, clip.id, 5, added.revision, target_track_id=added.timeline.tracks[1].id)
    diff = service.diff_revisions(project.id, project.revision_id, moved.revision_id)
    assert diff.summary.by_entity_type.tracks.added == 1
    assert diff.summary.by_entity_type.clips.modified == 1
    assert "/track_id" in {field.path for field in diff.changes.clips.modified[0].fields}


def test_multiple_asset_names_and_clip_references_survive_reopen(tmp_path):
    service = ProjectService(ProjectStore(tmp_path))
    project = new_project("Named imports")
    for index, name in enumerate(("first-video.mp4", "second-video.mp4", "third-video.mp4"), start=1):
        project = register_asset(
            project,
            Asset(f"asset_{index}", f"/tmp/{name}", name, "h264", 320, 180, FrameRate(24, 1), 48),
        )
    service.store.save(project)
    reopened = service.get(project.id)
    assets = {asset.id: asset.name for asset in reopened.assets}
    clips = [clip for track in reopened.timeline.tracks for clip in track.clips]
    assert assets == {"asset_1": "first-video.mp4", "asset_2": "second-video.mp4", "asset_3": "third-video.mp4"}
    assert len({clip.asset_id for clip in clips}) == 3
    assert [assets[clip.asset_id] for clip in clips] == ["first-video.mp4", "second-video.mp4", "third-video.mp4"]


def test_clip_moves_forward_backward_cross_track_and_boundary_without_false_collision(tmp_path):
    service = ProjectService(ProjectStore(tmp_path))
    project = register_asset(new_project("Move coverage"), Asset("asset", "/tmp/source.mp4", "source.mp4", "h264", 320, 180, FrameRate(24, 1), 120))
    project = register_asset(project, Asset("asset_2", "/tmp/second.mp4", "second-video.mp4", "h264", 320, 180, FrameRate(24, 1), 60), timeline_start_frame=180)
    service.store.create_initial(project)
    first = project.timeline.tracks[0].clips[0]
    moved = service.move(project.id, first.id, 20, project.revision)
    moved = service.move(project.id, first.id, 0, moved.revision)
    second = moved.timeline.tracks[0].clips[1]
    extra = service.add_track(project.id, moved.revision, "V2")
    moved = service.move(project.id, first.id, 60, extra.revision, target_track_id=extra.timeline.tracks[1].id)
    source_track = next(track for track in moved.timeline.tracks if any(clip.id == second.id for clip in track.clips))
    target_track = next(track for track in moved.timeline.tracks if any(clip.id == first.id for clip in track.clips))
    assert source_track.clips[0].id == second.id
    assert target_track.clips[0].id == first.id
    assert target_track.clips[0].timeline_start_frame == 60
    moved = service.move(project.id, first.id, 60, moved.revision, target_track_id=source_track.id)
    source_track = next(track for track in moved.timeline.tracks if any(clip.id == second.id for clip in track.clips))
    assert source_track.clips[0].timeline_start_frame == 60
    with pytest.raises(TimelineConflictError):
        service.move(project.id, first.id, 90, moved.revision, target_track_id=source_track.id)
