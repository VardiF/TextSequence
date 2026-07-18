from app.domain.models import Asset, Clip, FrameRate, Project, TimelineConflictError, ValidationError, project_from_dict, project_to_dict
from app.domain.operations import delete_clip, move_clip, new_project, register_asset, split_clip, trim_clip


def test_clip_duration_uses_exclusive_source_out():
    clip = Clip("clip", "asset", 10, 40, 0)
    assert clip.duration_frames == 30


def test_register_first_asset_sets_fps_and_creates_clip():
    project = new_project("Demo")
    asset = Asset("asset", "/tmp/demo.mp4", "demo.mp4", "h264", 1920, 1080, FrameRate(30000, 1001), 120)
    register_asset(project, asset)
    assert project.fps.as_tuple() == (30000, 1001)
    assert project.tracks[0].clips[0].source_out_frame == 120


def test_round_trip_preserves_canonical_state():
    project = new_project("Demo")
    data = project_to_dict(project)
    assert project_to_dict(project_from_dict(data)) == data


def test_mismatched_asset_fps_is_rejected():
    project = new_project("Demo")
    register_asset(project, Asset("a", "/tmp/a.mp4", "a", "h264", 1, 1, FrameRate(24, 1), 10))
    try:
        register_asset(project, Asset("b", "/tmp/b.mp4", "b", "h264", 1, 1, FrameRate(25, 1), 10))
    except ValidationError:
        pass
    else:
        raise AssertionError("expected FPS mismatch")


def project_with_three_clips():
    project = new_project("Editing")
    asset = Asset("asset", "/tmp/edit.mp4", "edit.mp4", "h264", 1, 1, FrameRate(24, 1), 300)
    project.assets.append(asset)
    track = project.tracks[0]
    track.clips = [
        Clip("a", "asset", 0, 100, 0),
        Clip("b", "asset", 100, 200, 100),
        Clip("c", "asset", 200, 300, 200),
    ]
    project.fps = FrameRate(24, 1)
    project.validate()
    return project


def test_split_preserves_mapping_and_rejects_boundaries():
    project = project_with_three_clips()
    edited = split_clip(project, "b", 140)
    clips = sorted(edited.tracks[0].clips, key=lambda clip: clip.timeline_start_frame)
    left, right = clips[1], clips[2]
    assert left.id == "b"
    assert (left.source_in_frame, left.source_out_frame, left.timeline_start_frame) == (100, 140, 100)
    assert (right.source_in_frame, right.source_out_frame, right.timeline_start_frame) == (140, 200, 140)
    assert right.id != left.id
    assert edited.revision == 1
    for frame in (100, 200):
        try:
            split_clip(project, "b", frame)
        except ValidationError:
            pass
        else:
            raise AssertionError("expected boundary split rejection")


def test_delete_preserves_gap():
    edited = delete_clip(project_with_three_clips(), "b")
    assert [clip.id for clip in edited.tracks[0].clips] == ["a", "c"]
    assert edited.tracks[0].clips[1].timeline_start_frame == 200


def test_move_rejects_collision_and_allows_gap():
    project = project_with_three_clips()
    try:
        move_clip(project, "c", 150)
    except TimelineConflictError:
        pass
    else:
        raise AssertionError("expected move collision rejection")
    edited = move_clip(project, "c", 350)
    assert next(clip for clip in edited.tracks[0].clips if clip.id == "c").timeline_start_frame == 350


def test_trim_keeps_start_and_rejects_invalid_source_range():
    project = project_with_three_clips()
    edited = trim_clip(project, "b", source_in_frame=120)
    clip = next(clip for clip in edited.tracks[0].clips if clip.id == "b")
    assert (clip.timeline_start_frame, clip.source_in_frame, clip.source_out_frame) == (100, 120, 200)
    for kwargs in ({"source_out_frame": 100}, {"source_in_frame": -1}, {"source_out_frame": 250}):
        try:
            trim_clip(project, "b", **kwargs)
        except ValidationError:
            pass
        else:
            raise AssertionError("expected invalid trim rejection")
