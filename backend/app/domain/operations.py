from __future__ import annotations

from copy import deepcopy
from uuid import uuid4
from typing import Callable, Optional

from .models import Asset, Clip, FrameRate, Marker, MarkerProductionMetadata, Project, TimelineConflictError, Track, ValidationError, VideoCanvas, marker_production_from_dict, marker_sort_key


def new_project(name: str) -> Project:
    project = Project(id=f"project_{uuid4().hex}", name=name.strip() or "Untitled project")
    project.tracks.append(Track(id=f"track_{uuid4().hex}", name="V1"))
    return project


def register_asset(project: Project, asset: Asset, target_track_id: str | None = None, timeline_start_frame: int | None = None) -> Project:
    if project.assets and project.fps != asset.fps:
        raise ValidationError("Cross-frame-rate media is unsupported")
    if not project.assets:
        project.fps = FrameRate(asset.fps.numerator, asset.fps.denominator)
    project.assets.append(asset)
    if project.timeline.video_canvas is None:
        project.timeline.video_canvas = VideoCanvas(asset.width, asset.height)
    target = next((track for track in project.tracks if track.id == target_track_id), None) if target_track_id else project.tracks[0]
    if target is None:
        raise ValidationError("Target track does not exist")
    start = 0 if timeline_start_frame is None else timeline_start_frame
    if isinstance(start, bool) or not isinstance(start, int) or start < 0:
        raise ValidationError("Timeline start must be a non-negative integer")
    if timeline_start_frame is None and target.clips:
        start = max(clip.timeline_start_frame + clip.duration_frames for clip in target.clips)
    target.clips.append(Clip(id=f"clip_{uuid4().hex}", asset_id=asset.id, source_in_frame=0, source_out_frame=asset.duration_frames, timeline_start_frame=start))
    target.clips.sort(key=lambda item: (item.timeline_start_frame, item.id))
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


def _find_track(project: Project, track_id: str) -> Track:
    track = next((item for item in project.tracks if item.id == track_id), None)
    if track is None:
        raise ValidationError(f"Track does not exist: {track_id}")
    return track


def new_track_id() -> str:
    return f"track_{uuid4().hex}"


def add_track(project: Project, name: str, position: int | None = None,
              external_refs: list | None = None, track_id: str | None = None) -> Project:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
        raise ValidationError("Track name must be 1-160 characters")
    if position is not None and (isinstance(position, bool) or not isinstance(position, int) or not 0 <= position <= len(project.tracks)):
        raise ValidationError("Track position is invalid")
    def edit(edited: Project) -> None:
        track = Track(id=track_id or new_track_id(), name=name.strip(), kind="video", external_refs=external_refs or [])
        edited.tracks.insert(len(edited.tracks) if position is None else position, track)
    return _apply_edit(project, edit)


def update_track(project: Project, track_id: str, name: str | None = None,
                 external_refs: list | None = None) -> Project:
    def edit(edited: Project) -> None:
        track = _find_track(edited, track_id)
        next_name = track.name if name is None else name.strip()
        next_refs = track.external_refs if external_refs is None else deepcopy(external_refs)
        if not isinstance(next_name, str) or not 1 <= len(next_name) <= 160:
            raise ValidationError("Track name must be 1-160 characters")
        if next_name == track.name and next_refs == track.external_refs:
            raise ValidationError("NO_CHANGES")
        track.name = next_name
        track.external_refs = next_refs
    return _apply_edit(project, edit)


def delete_track(project: Project, track_id: str) -> Project:
    def edit(edited: Project) -> None:
        track = _find_track(edited, track_id)
        if len(edited.tracks) == 1:
            raise ValidationError("Cannot delete the last video track")
        if track.clips:
            raise ValidationError("Track must be empty before deletion")
        edited.tracks = [item for item in edited.tracks if item.id != track_id]
    return _apply_edit(project, edit)


def reorder_track(project: Project, track_id: str, position: int) -> Project:
    if isinstance(position, bool) or not isinstance(position, int) or not 0 <= position < len(project.tracks):
        raise ValidationError("Track position is invalid")
    def edit(edited: Project) -> None:
        index = next((i for i, item in enumerate(edited.tracks) if item.id == track_id), None)
        if index is None:
            raise ValidationError(f"Track does not exist: {track_id}")
        if index == position:
            raise ValidationError("NO_CHANGES")
        track = edited.tracks.pop(index)
        edited.tracks.insert(position, track)
    return _apply_edit(project, edit)


def _apply_edit(project: Project, edit: Callable[[Project], None]) -> Project:
    edited = deepcopy(project)
    edit(edited)
    for track in edited.tracks:
        track.clips.sort(key=lambda item: (item.timeline_start_frame, item.id))
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


def move_clip(project: Project, clip_id: str, timeline_start_frame: int, target_track_id: str | None = None) -> Project:
    if isinstance(timeline_start_frame, bool) or not isinstance(timeline_start_frame, int) or timeline_start_frame < 0:
        raise ValidationError("Timeline start must be non-negative")

    def edit(edited: Project) -> None:
        source_track, clip = _find_clip(edited, clip_id)
        target_track = source_track if target_track_id is None else _find_track(edited, target_track_id)
        source_track.clips = [item for item in source_track.clips if item.id != clip_id]
        clip.timeline_start_frame = timeline_start_frame
        target_track.clips.append(clip)
        target_track.clips.sort(key=lambda item: (item.timeline_start_frame, item.id))
        _ensure_no_collisions(target_track)

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
