import pytest

from app.domain.frame_math import frames_to_ffmpeg_time
from app.domain.models import Asset, Clip, FrameRate, TimelineConflictError
from app.domain.operations import new_project
from app.rendering.plan import ClipSegment, GapSegment, compile_render_plan


def render_project(clips):
    project = new_project("Render plan")
    project.fps = FrameRate(24, 1)
    project.assets.append(Asset("asset", "/tmp/source.mp4", "source.mp4", "h264", 320, 180, FrameRate(24, 1), 100))
    project.tracks[0].clips = clips
    return project


def test_single_clip_plan_and_duration():
    plan = compile_render_plan(render_project([Clip("a", "asset", 0, 24, 0)]))
    assert plan.duration_frames == 24
    assert plan.segments == [ClipSegment("a", "asset", "/tmp/source.mp4", 0, 24, 0)]


def test_trimmed_contiguous_clips_are_ordered_by_timeline():
    plan = compile_render_plan(render_project([
        Clip("b", "asset", 40, 60, 20),
        Clip("a", "asset", 10, 30, 0),
    ]))
    assert [segment.clip_id for segment in plan.segments if isinstance(segment, ClipSegment)] == ["a", "b"]
    assert plan.duration_frames == 40


def test_deleted_or_moved_clip_creates_internal_gap_and_leading_gap():
    plan = compile_render_plan(render_project([Clip("b", "asset", 20, 32, 40)]))
    assert plan.duration_frames == 52
    assert isinstance(plan.segments[0], GapSegment)
    assert plan.segments[0].duration_frames == 40
    assert isinstance(plan.segments[1], ClipSegment)


def test_overlapping_clips_are_rejected():
    with pytest.raises(TimelineConflictError):
        compile_render_plan(render_project([Clip("a", "asset", 0, 30, 0), Clip("b", "asset", 30, 60, 20)]))


def test_frame_time_conversion_is_rational_and_deterministic():
    assert frames_to_ffmpeg_time(30000, (30000, 1001)) == "1001"
    assert frames_to_ffmpeg_time(1, (30000, 1001)) == "0.033366666667"

