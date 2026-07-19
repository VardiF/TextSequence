from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
from typing import Callable, Optional

from .models import Asset, Clip, FrameRate, Marker, MarkerProductionMetadata, Project, TimelineConflictError, Track, ValidationError, marker_production_from_dict, marker_sort_key


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


def new_marker_id() -> str:
    return f"marker_{uuid4().hex}"


def _find_marker(project: Project, marker_id: str) -> Marker:
    for marker in project.timeline.markers:
        if marker.id == marker_id:
            return marker
    raise ValidationError(f"Marker does not exist: {marker_id}")


def _validate_marker_production(production: MarkerProductionMetadata) -> None:
    values = [*production.shot_ids, *production.dialogue_line_ids]
    if any(not isinstance(value, str) or not value for value in values):
        raise ValidationError("Marker production references must be non-empty strings")
    if len(production.shot_ids) != len(set(production.shot_ids)) or len(production.dialogue_line_ids) != len(set(production.dialogue_line_ids)):
        raise ValidationError("Marker production references must be unique")
    external = [(item.system, item.id, item.kind) for item in production.external_refs]
    if len(external) != len(set(external)):
        raise ValidationError("Marker external references must be unique")


def add_marker(project: Project, marker: Marker) -> Project:
    def edit(edited: Project) -> None:
        if any(existing.id == marker.id for existing in edited.timeline.markers):
            raise ValidationError(f"Marker ID already exists: {marker.id}")
        _validate_marker_production(marker.production)
        edited.timeline.markers.append(deepcopy(marker))
        edited.timeline.markers.sort(key=marker_sort_key)

    return _apply_edit(project, edit)


def update_marker(project: Project, marker_id: str, changes: dict) -> Project:
    if not isinstance(changes, dict):
        raise ValidationError("Marker changes must be an object")
    allowed = {"id", "start_frame", "end_frame", "name", "description", "type", "production"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise ValidationError(f"Unknown marker change: {unknown[0]}")
    if "id" in changes:
        raise ValidationError("Marker ID cannot be updated")

    def edit(edited: Project) -> None:
        current = _find_marker(edited, marker_id)
        production = changes.get("production", current.production)
        if isinstance(production, dict):
            production = marker_production_from_dict(production)
        candidate = Marker(
            id=current.id,
            start_frame=changes.get("start_frame", current.start_frame),
            end_frame=changes.get("end_frame", current.end_frame),
            name=changes.get("name", current.name),
            description=changes.get("description", current.description),
            type=changes.get("type", current.type),
            production=deepcopy(production),
        )
        _validate_marker_production(candidate.production)
        if candidate == current:
            raise ValidationError("Marker update produced no changes")
        index = edited.timeline.markers.index(current)
        edited.timeline.markers[index] = candidate
        edited.timeline.markers.sort(key=marker_sort_key)

    return _apply_edit(project, edit)


def delete_marker(project: Project, marker_id: str) -> Project:
    def edit(edited: Project) -> None:
        _find_marker(edited, marker_id)
        edited.timeline.markers = [marker for marker in edited.timeline.markers if marker.id != marker_id]

    return _apply_edit(project, edit)


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


def split_clip(project: Project, clip_id: str, timeline_frame: int, new_clip_id: Optional[str] = None) -> Project:
    def edit(edited: Project) -> None:
        track, clip = _find_clip(edited, clip_id)
        clip_end = clip.timeline_start_frame + clip.duration_frames
        if timeline_frame <= clip.timeline_start_frame or timeline_frame >= clip_end:
            raise ValidationError("Split frame must be strictly inside the clip")
        source_split = clip.source_in_frame + (timeline_frame - clip.timeline_start_frame)
        original_out = clip.source_out_frame
        clip.source_out_frame = source_split
        track.clips.append(Clip(
            id=new_clip_id or f"clip_{uuid4().hex}",
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
