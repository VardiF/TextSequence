import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.domain.models import Asset, ClipProductionMetadata, FrameRate, ValidationError, project_from_dict, project_to_dict
from app.domain.operations import new_project
from app.domain.operations import register_asset, split_clip
from app.media.probe import probe_media
from app.persistence.project_store import ProjectStore
from app.persistence.revisions import RevisionMetadata, revision_hash
from app.services.projects import ProjectService


FIXTURES = Path(__file__).parent / "fixtures" / "v1"


def read_fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _record_paths(directory):
    records = {}
    for path in (directory / "revisions").glob("*.json"):
        record = json.loads(path.read_text())
        records[record["metadata"]["revision_number"]] = path
    return records


def _fresh_linear_history(tmp_path):
    store = ProjectStore(tmp_path)
    project = register_asset(new_project("Linear"), Asset(
        "asset_linear", "/tmp/linear.mp4", "linear.mp4", "h264", 1, 1,
        FrameRate(24, 1), 100,
    ))
    store.create_initial(project)
    service = ProjectService(store, tmp_path / "runtime")
    first = service.split(project.id, project.timeline.tracks[0].clips[0].id, 40, 0)
    second = service.split(project.id, first.timeline.tracks[0].clips[0].id, 20, 1)
    return store, second.id


def _migrated_linear_history(tmp_path):
    source = read_fixture("imported.json")
    source["tracks"][0]["clips"].append({
        "id": "clip_v1_second", "asset_id": "asset_v1", "source_in_frame": 0,
        "source_out_frame": 20, "timeline_start_frame": 60,
    })
    (tmp_path / "project_v1_imported.json").write_text(json.dumps(source))
    store = ProjectStore(tmp_path)
    service = ProjectService(store, tmp_path / "runtime")
    first = service.delete("project_v1_imported", "clip_v1", 4)
    second = service.delete("project_v1_imported", "clip_v1_second", 5)
    return store, second.id


def _resign_record(record):
    metadata = RevisionMetadata(**record["metadata"])
    snapshot = project_from_dict(record["snapshot"])
    record["metadata"]["snapshot_sha256"] = revision_hash(metadata, snapshot)


def _rewrite_head_revision(directory, mutate, resign=False):
    head = json.loads((directory / "head.json").read_text())
    path = directory / "revisions" / f"{head['revision_id']}.json"
    record = json.loads(path.read_text())
    mutate(record, directory)
    if resign:
        _resign_record(record)
    path.write_text(json.dumps(record))


def test_v1_fixture_migrates_to_canonical_v3_without_top_level_tracks():
    source = read_fixture("imported.json")
    migrated = project_from_dict(source)
    document = project_to_dict(migrated)
    assert document["schema_version"] == 3
    assert set(document) == {"schema_version", "id", "name", "fps", "revision", "revision_id", "external_refs", "assets", "timeline"}
    assert document["revision"] == 4
    assert document["timeline"]["id"].startswith("timeline_")
    assert document["timeline"]["video_canvas"] == {"width": 320, "height": 180}
    assert document["assets"][0]["kind"] == "video"
    assert document["timeline"]["tracks"][0]["clips"][0]["source_out_frame"] == 48
    assert source["tracks"]
    repeated = project_to_dict(project_from_dict(source))
    assert repeated == document


def test_migration_rejects_unknown_fields_and_future_schema():
    unknown = read_fixture("unknown_field.json")
    with pytest.raises(ValidationError, match=r"project\.future_note"):
        project_from_dict(unknown)
    future = read_fixture("future_schema.json")
    with pytest.raises(ValidationError, match="Unsupported future schema_version"):
        project_from_dict(future)


def test_first_successful_mutation_promotes_legacy_and_preserves_source(tmp_path):
    project_path = tmp_path / "project_v1_imported.json"
    source_bytes = json.dumps(read_fixture("imported.json"), indent=2).encode() + b"\n"
    project_path.write_bytes(source_bytes)
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    current = service.get("project_v1_imported")
    assert current.revision == 4
    edited = service.delete("project_v1_imported", "clip_v1", 4)
    assert edited.revision == 5
    directory = tmp_path / "project_v1_imported"
    assert (directory / "head.json").is_file()
    assert (directory / "legacy-v1.json").read_bytes() == source_bytes
    assert project_path.read_bytes() == source_bytes
    assert len(list((directory / "revisions").glob("*.json"))) == 2


def test_noop_silence_does_not_promote_or_advance_revision(tmp_path, monkeypatch):
    source = read_fixture("empty.json")
    (tmp_path / "project_v1_empty.json").write_text(json.dumps(source))
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    monkeypatch.setattr(service, "_analyze_project_silence", lambda *_args: [])
    result = service.remove_silence("project_v1_empty", 0)
    assert result["revision"] == 0
    assert not (tmp_path / "project_v1_empty").exists()


def test_orphan_revision_is_ignored_and_head_is_authoritative(tmp_path):
    store = ProjectStore(tmp_path)
    project = store.create_initial(new_project("Head"))
    directory = tmp_path / project.id
    orphan = directory / "revisions" / "revision_orphan.json"
    orphan.write_text(json.dumps({"not": "a head"}))
    assert store.load(project.id).revision == 0


def test_fresh_history_validates_as_a_linear_chain(tmp_path):
    store, project_id = _fresh_linear_history(tmp_path)
    assert store.load(project_id).revision == 2


def test_tampered_normal_revision_cannot_become_a_migration_root(tmp_path):
    store, project_id = _fresh_linear_history(tmp_path)
    directory = tmp_path / project_id
    paths = _record_paths(directory)
    path = paths[1]
    record = json.loads(path.read_text())
    record["metadata"]["parent_revision_id"] = None
    record["metadata"]["operation"] = "migration"
    path.write_text(json.dumps(record))

    with pytest.raises(ValidationError, match="integrity"):
        store.load(project_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("parent_revision_id", None),
        ("operation", "tampered-operation"),
        ("revision_id", "revision_tampered"),
        ("revision_number", 999),
        ("project_id", "project_tampered"),
    ],
)
def test_revision_integrity_digest_binds_immutable_metadata(tmp_path, field, value):
    store, project_id = _fresh_linear_history(tmp_path)
    path = _record_paths(tmp_path / project_id)[1]
    record = json.loads(path.read_text())
    record["metadata"][field] = value
    path.write_text(json.dumps(record))

    with pytest.raises(ValidationError):
        store.load(project_id)


def test_migrated_history_allows_nonzero_baseline_and_consecutive_parents(tmp_path):
    store, project_id = _migrated_linear_history(tmp_path)
    records = _record_paths(tmp_path / project_id)
    assert json.loads(records[4].read_text())["metadata"]["parent_revision_id"] is None
    assert json.loads(records[5].read_text())["metadata"]["parent_revision_id"] == json.loads(records[4].read_text())["metadata"]["revision_id"]
    assert json.loads(records[6].read_text())["metadata"]["parent_revision_id"] == json.loads(records[5].read_text())["metadata"]["revision_id"]
    assert store.load(project_id).revision == 6


@pytest.mark.parametrize("corruption", ["skip", "self", "forward", "missing", "number", "cycle"])
def test_corrupt_parent_chains_are_rejected(tmp_path, corruption):
    store, project_id = _fresh_linear_history(tmp_path)
    directory = tmp_path / project_id
    paths = _record_paths(directory)
    revision_one = json.loads(paths[1].read_text())["metadata"]["revision_id"]
    revision_two = json.loads(paths[2].read_text())["metadata"]["revision_id"]
    revision_zero = json.loads(paths[0].read_text())["metadata"]["revision_id"]

    def mutate(record, _directory):
        if corruption == "skip":
            record["metadata"]["parent_revision_id"] = revision_zero
        elif corruption == "self":
            record["metadata"]["parent_revision_id"] = revision_two
        elif corruption == "forward":
            parent = json.loads(paths[1].read_text())
            parent["metadata"]["parent_revision_id"] = revision_two
            paths[1].write_text(json.dumps(parent))
        elif corruption == "missing":
            record["metadata"]["parent_revision_id"] = "revision_missing"
        elif corruption == "number":
            record["metadata"]["parent_revision_id"] = revision_one
            parent = json.loads(paths[1].read_text())
            parent["metadata"]["revision_number"] = 0
            paths[1].write_text(json.dumps(parent))
        elif corruption == "cycle":
            record["metadata"]["parent_revision_id"] = revision_one
            parent = json.loads(paths[1].read_text())
            parent["metadata"]["parent_revision_id"] = revision_two
            paths[1].write_text(json.dumps(parent))

    _rewrite_head_revision(directory, mutate, resign=True)
    with pytest.raises(ValidationError):
        store.load(project_id)


def test_valid_head_ignores_a_higher_numbered_orphan_revision(tmp_path):
    store, project_id = _fresh_linear_history(tmp_path)
    directory = tmp_path / project_id
    paths = _record_paths(directory)
    orphan = json.loads(paths[2].read_text())
    orphan["metadata"]["revision_id"] = "revision_orphan_high"
    orphan["snapshot"]["revision"] = 999
    orphan["snapshot"]["revision_id"] = "revision_orphan_high"
    (directory / "revisions" / "revision_orphan_high.json").write_text(json.dumps(orphan))
    assert store.load(project_id).revision == 2


def test_parent_from_another_project_is_rejected(tmp_path):
    store, project_id = _fresh_linear_history(tmp_path)
    other = store.create_initial(new_project("Other"))
    other_record = next((tmp_path / other.id / "revisions").glob("*.json")).read_text()
    other_data = json.loads(other_record)
    other_revision_id = other_data["metadata"]["revision_id"]
    directory = tmp_path / project_id
    (directory / "revisions" / f"{other_revision_id}.json").write_text(other_record)
    paths = _record_paths(directory)
    head = json.loads(paths[2].read_text())
    head["metadata"]["parent_revision_id"] = other_revision_id
    _resign_record(head)
    paths[2].write_text(json.dumps(head))
    with pytest.raises(ValidationError, match="another project"):
        store.load(project_id)


def test_malformed_revision_id_cannot_escape_revision_directory(tmp_path):
    store = ProjectStore(tmp_path, revision_id_factory=lambda: "../../escaped")
    service = ProjectService(store, tmp_path / "runtime")
    project = register_asset(new_project("Revision IDs"), Asset(
        "asset_revision_ids", "/tmp/revision-ids.mp4", "revision-ids.mp4", "h264", 1, 1,
        FrameRate(24, 1), 100,
    ))
    store.create_initial(project)

    with pytest.raises(ValidationError, match="Invalid revision id"):
        service.split(project.id, project.timeline.tracks[0].clips[0].id, 50, expected_revision=0)

    assert not (tmp_path / "escaped.json").exists()
    assert store.load(project.id).revision == 0


def test_split_copies_typed_clip_production_metadata_without_aliasing():
    project = register_asset(new_project("Metadata"), Asset("asset_meta", "/tmp/meta.mp4", "meta.mp4", "h264", 1, 1, FrameRate(24, 1), 100))
    clip = project.timeline.tracks[0].clips[0]
    clip.production = ClipProductionMetadata(shot_ids=["shot-1"], dialogue_line_ids=["line-1"])
    edited = split_clip(project, clip.id, 40)
    left, right = sorted(edited.timeline.tracks[0].clips, key=lambda item: item.timeline_start_frame)
    assert left.production.shot_ids == right.production.shot_ids == ["shot-1"]
    assert left.production is not right.production
    right.production.shot_ids.append("shot-2")
    assert left.production.shot_ids == ["shot-1"]


def test_create_failure_leaves_no_partial_directory(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path)
    monkeypatch.setattr(store, "_write_head", lambda *_args: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(OSError, match="injected"):
        store.create_initial(new_project("Failure"))
    assert list(tmp_path.iterdir()) == []


def test_promotion_failure_keeps_legacy_and_committed_baseline(tmp_path, monkeypatch):
    source = read_fixture("imported.json")
    source_path = tmp_path / "project_v1_imported.json"
    source_path.write_text(json.dumps(source))
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    store = service.store
    original = store._write_revision
    calls = {"count": 0}

    def fail_candidate(directory, record):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("candidate write failed")
        return original(directory, record)

    monkeypatch.setattr(store, "_write_revision", fail_candidate)
    with pytest.raises(OSError, match="candidate write failed"):
        service.delete("project_v1_imported", "clip_v1", 4)
    directory = tmp_path / "project_v1_imported"
    assert source_path.is_file()
    assert (directory / "legacy-v1.json").is_file()
    assert store.load("project_v1_imported").revision == 4


def test_real_copied_v1_project_renders_promotes_restarts_and_removes_silence(tmp_path, silence_media_path):
    asset = probe_media(str(silence_media_path))
    legacy = {
        "id": "project_real_v1",
        "name": "Real copied v1",
        "fps": {"numerator": asset.fps.numerator, "denominator": asset.fps.denominator},
        "revision": 4,
        "assets": [{"id": "asset_real_v1", "path": asset.path, "name": asset.name, "codec": asset.codec,
                    "width": asset.width, "height": asset.height,
                    "fps": {"numerator": asset.fps.numerator, "denominator": asset.fps.denominator},
                    "duration_frames": asset.duration_frames}],
        "tracks": [{"id": "track_real_v1", "name": "V1", "kind": "video", "clips": [{
            "id": "clip_real_v1", "asset_id": "asset_real_v1", "source_in_frame": 0,
                    "source_out_frame": asset.duration_frames, "timeline_start_frame": 0}]}],
    }
    source = tmp_path / "project_real_v1.json"
    source.write_text(json.dumps(legacy, indent=2) + "\n")
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    before = service.get("project_real_v1")
    assert before.revision == 4
    assert not (tmp_path / "project_real_v1").exists()
    rendered = service.render_preview("project_real_v1", 4)
    assert Path(rendered.path).is_file()
    edited = service.trim("project_real_v1", "clip_real_v1", 4, source_out_frame=asset.duration_frames - 1)
    assert edited.revision == 5
    directory = tmp_path / "project_real_v1"
    assert (directory / "legacy-v1.json").is_file()
    head = json.loads((directory / "head.json").read_text())
    assert head["revision"] == 5
    restarted = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    assert restarted.get("project_real_v1").revision == 5
    analysis = restarted.analyze_silence("project_real_v1")
    assert analysis["summary"]["detected_silences"] >= 1
    removed = restarted.remove_silence("project_real_v1", 5)
    assert removed["revision"] == 6
