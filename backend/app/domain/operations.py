from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
from typing import Callable, Optional

from .models import Asset, Clip, FrameRate, Project, TimelineConflictError, Track, ValidationError


def new_project(name: str) -> Project:
    project = Project(id=f"project_{uuid4().hex}", name=name.strip() or "Untitled project")
    project.tracks.append(Track(id=f"track_{uuid4().hex}", name="V1"))
    return project


def register_asset(project: Project, asset: Asset) -> Project:
    if project.assets and project.fps != asset.fps:
        raise ValidationError("Cross-frame-rate media is unsupported")
    if not project.assets:
        project.fps = FrameRate(asset.fps.numerator, asset.fps.denominator)
    project.assets.append(asset)
    v1 = next((track for track in project.tracks if track.name == "V1"), None)
    if v1 is None:
        v1 = Track(id=f"track_{uuid4().hex}", name="V1")
        project.tracks.append(v1)
    v1.clips.append(Clip(id=f"clip_{uuid4().hex}", asset_id=asset.id, source_in_frame=0, source_out_frame=asset.duration_frames, timeline_start_frame=0))
    project.validate()
    return project


def _find_clip(project: Project, clip_id: str) -> tuple[Track, Clip]:
    for track in project.tracks:
        for clip in track.clips:
            if clip.id == clip_id:
                return track, clip
    raise ValidationError(f"Clip does not exist: {clip_id}")


def _apply_edit(project: Project, edit: Callable[[Project], None]) -> Project:
    edited = deepcopy(project)
    edit(edited)
    edited.validate()
    return edited


def split_clip(project: Project, clip_id: str, timeline_frame: int) -> Project:
    def edit(edited: Project) -> None:
        track, clip = _find_clip(edited, clip_id)
        clip_end = clip.timeline_start_frame + clip.duration_frames
        if timeline_frame <= clip.timeline_start_frame or timeline_frame >= clip_end:
            raise ValidationError("Split frame must be strictly inside the clip")
        source_split = clip.source_in_frame + (timeline_frame - clip.timeline_start_frame)
        original_out = clip.source_out_frame
        clip.source_out_frame = source_split
        track.clips.append(Clip(
            id=f"clip_{uuid4().hex}",
            asset_id=clip.asset_id,
            source_in_frame=source_split,
            source_out_frame=original_out,
            timeline_start_frame=timeline_frame,
            production=deepcopy(clip.production),
        ))

    return _apply_edit(project, edit)


def delete_clip(project: Project, clip_id: str) -> Project:
    def edit(edited: Project) -> None:
        track, _ = _find_clip(edited, clip_id)
        track.clips = [clip for clip in track.clips if clip.id != clip_id]

    return _apply_edit(project, edit)


def _ensure_no_collisions(track: Track) -> None:
    clips = sorted(track.clips, key=lambda item: item.timeline_start_frame)
    for previous, current in zip(clips, clips[1:]):
        if current.timeline_start_frame < previous.timeline_start_frame + previous.duration_frames:
            raise TimelineConflictError(f"Clip {current.id} would overlap clip {previous.id} on {track.name}")


def move_clip(project: Project, clip_id: str, timeline_start_frame: int) -> Project:
    if timeline_start_frame < 0:
        raise ValidationError("Timeline start must be non-negative")

    def edit(edited: Project) -> None:
        track, clip = _find_clip(edited, clip_id)
        clip.timeline_start_frame = timeline_start_frame
        _ensure_no_collisions(track)

    return _apply_edit(project, edit)


def trim_clip(
    project: Project,
    clip_id: str,
    source_in_frame: Optional[int] = None,
    source_out_frame: Optional[int] = None,
) -> Project:
    if source_in_frame is None and source_out_frame is None:
        raise ValidationError("Trim requires source_in_frame or source_out_frame")

    def edit(edited: Project) -> None:
        track, clip = _find_clip(edited, clip_id)
        if source_in_frame is not None:
            clip.source_in_frame = source_in_frame
        if source_out_frame is not None:
            clip.source_out_frame = source_out_frame
        _ensure_no_collisions(track)

    return _apply_edit(project, edit)
