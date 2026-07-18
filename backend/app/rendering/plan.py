from __future__ import annotations

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
class RenderPlan:
    fps: tuple[int, int]
    width: int
    height: int
    duration_frames: int
    segments: List[RenderSegment]


def compile_render_plan(project: Project) -> RenderPlan:
    project.validate()
    if project.fps is None:
        raise ValidationError("Cannot render a project without an FPS")
    video_tracks = [track for track in project.tracks if track.kind == "video"]
    clips = sorted((clip for track in video_tracks for clip in track.clips), key=lambda clip: (clip.timeline_start_frame, clip.id))
    if not clips:
        raise ValidationError("Cannot render a timeline with no clips")
    for previous, current in zip(clips, clips[1:]):
        previous_end = previous.timeline_start_frame + previous.duration_frames
        if current.timeline_start_frame < previous_end:
            raise TimelineConflictError(f"Clips {previous.id} and {current.id} overlap")
    assets = {asset.id: asset for asset in project.assets}
    first_asset = assets[clips[0].asset_id]
    segments: List[RenderSegment] = []
    cursor = 0
    for clip in clips:
        if clip.timeline_start_frame > cursor:
            segments.append(GapSegment(timeline_start_frame=cursor, duration_frames=clip.timeline_start_frame - cursor))
        asset = assets[clip.asset_id]
        segments.append(ClipSegment(
            clip_id=clip.id,
            asset_id=asset.id,
            source_path=asset.path,
            source_in_frame=clip.source_in_frame,
            source_out_frame=clip.source_out_frame,
            timeline_start_frame=clip.timeline_start_frame,
        ))
        cursor = clip.timeline_start_frame + clip.duration_frames
    return RenderPlan(
        fps=project.fps.as_tuple(),
        width=first_asset.width,
        height=first_asset.height,
        duration_frames=cursor,
        segments=segments,
    )
