from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import List, Union

from app.domain.models import Project, TimelineConflictError, ValidationError


@dataclass(frozen=True)
class ClipSegment:
    clip_id: str
    asset_id: str
    source_path: str
    source_in_frame: int
    source_out_frame: int
    timeline_start_frame: int

    @property
    def duration_frames(self) -> int:
        return self.source_out_frame - self.source_in_frame


@dataclass(frozen=True)
class GapSegment:
    timeline_start_frame: int
    duration_frames: int


RenderSegment = Union[ClipSegment, GapSegment]


@dataclass(frozen=True)
class VideoLayer:
    track_id: str
    position: int
    segments: List[ClipSegment]


@dataclass(frozen=True)
class AudioSource:
    clip_id: str
    asset_id: str
    source_path: str
    source_in_frame: int
    source_out_frame: int
    timeline_start_frame: int


@dataclass(frozen=True)
class RenderPlan:
    fps: tuple[int, int]
    width: int
    height: int
    duration_frames: int
    segments: List[RenderSegment]
    layers: List[VideoLayer]
    audio_sources: List[AudioSource]


def compile_render_plan(project: Project) -> RenderPlan:
    project = deepcopy(project)
    for track in project.timeline.tracks:
        track.clips.sort(key=lambda item: (item.timeline_start_frame, item.id))
    project.validate()
    if project.fps is None:
        raise ValidationError("Cannot render a project without an FPS")
    video_tracks = [track for track in project.timeline.tracks if track.kind == "video"]
    clips = sorted((clip for track in video_tracks for clip in track.clips), key=lambda clip: (clip.timeline_start_frame, clip.id))
    if not clips:
        raise ValidationError("Cannot render a timeline with no clips")
    assets = {asset.id: asset for asset in project.assets}
    canvas = project.timeline.video_canvas
    if canvas is None:
        raise ValidationError("Cannot render a project without a video canvas")
    segments: List[RenderSegment] = []
    layers: list[VideoLayer] = []
    audio_sources: list[AudioSource] = []
    cursor = max((clip.timeline_start_frame + clip.duration_frames for clip in clips), default=0)
    for position, track in enumerate(video_tracks):
        layer_clips: list[ClipSegment] = []
        for clip in sorted(track.clips, key=lambda item: (item.timeline_start_frame, item.id)):
            asset = assets[clip.asset_id]
            segment = ClipSegment(clip.id, asset.id, asset.path, clip.source_in_frame, clip.source_out_frame, clip.timeline_start_frame)
            layer_clips.append(segment)
            audio_sources.append(AudioSource(clip.id, asset.id, asset.path, clip.source_in_frame, clip.source_out_frame, clip.timeline_start_frame))
        layers.append(VideoLayer(track.id, position, layer_clips))
    # Keep the legacy flat view for one-track callers and existing API tests.
    if len(layers) == 1:
        cursor_for_segments = 0
        for segment in layers[0].segments:
            if segment.timeline_start_frame > cursor_for_segments:
                segments.append(GapSegment(cursor_for_segments, segment.timeline_start_frame - cursor_for_segments))
            segments.append(segment)
            cursor_for_segments = segment.timeline_start_frame + segment.duration_frames
    return RenderPlan(
        fps=project.fps.as_tuple(),
        width=canvas.width,
        height=canvas.height,
        duration_frames=cursor,
        segments=segments,
        layers=layers,
        audio_sources=audio_sources,
    )
