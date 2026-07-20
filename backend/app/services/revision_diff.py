"""Pure deterministic comparison of two validated canonical Project values."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from app.domain.models import Project
from app.revision_diff_models import (
    AssetChanges, ClipChanges, EntityModification, FieldChange, MarkerChanges,
    EntityTypeSummary, ProjectChanges, RedactedFieldChange, RevisionChanges,
    RevisionDiffByEntityType, RevisionDiffSummary,
    SafeAsset, SafeAssetProduction, SafeClip, SafeExternalReference, SafeFrameRate,
    SafeMarker, SafeProduction, SafeTrack, TimelineChanges, TrackChanges,
    ValueFieldChange,
)


class RevisionDiffIntegrityError(ValueError):
    code = "INTEGRITY_ERROR"


class RevisionDiffError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class RevisionHistoryUnavailableError(RevisionDiffError):
    def __init__(self):
        super().__init__("HISTORY_UNAVAILABLE", "Revision history is unavailable for this project")


def _fps(value) -> dict[str, int] | None:
    return None if value is None else {"numerator": value.numerator, "denominator": value.denominator}


def _refs(values) -> list[dict[str, str]]:
    return [{"system": ref.system, "id": ref.id, "kind": ref.kind} for ref in values]


def _production(value, *, asset: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "shot_ids": list(value.shot_ids),
        "dialogue_line_ids": list(value.dialogue_line_ids),
        "external_refs": _refs(value.external_refs),
    }
    if asset:
        result["generation_job_id"] = value.generation_job_id
    return result


def _value(path: str, before: Any, after: Any) -> ValueFieldChange:
    return ValueFieldChange(path=path, kind="value", before=before, after=after)


def _field_changes(before: dict[str, Any], after: dict[str, Any], fields: Iterable[str]) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for path in fields:
        if before[path] != after[path]:
            changes.append(_value(f"/{path}", before[path], after[path]))
    return changes


def _asset_state(asset) -> SafeAsset:
    return SafeAsset(
        id=asset.id, kind=asset.kind, name=asset.name, codec=asset.codec, width=asset.width, height=asset.height,
        fps=SafeFrameRate(**_fps(asset.fps)), duration_frames=asset.duration_frames,
        production=SafeAssetProduction(**_production(asset.production, asset=True)),
    )


def _track_state(track, position: int) -> SafeTrack:
    return SafeTrack(id=track.id, name=track.name, kind=track.kind, position=position,
                     external_refs=[SafeExternalReference(system=ref.system, id=ref.id, kind=ref.kind) for ref in track.external_refs])


def _clip_state(clip, track_id: str) -> SafeClip:
    return SafeClip(
        id=clip.id, kind=clip.kind, track_id=track_id, asset_id=clip.asset_id,
        source_in_frame=clip.source_in_frame, source_out_frame=clip.source_out_frame,
        timeline_start_frame=clip.timeline_start_frame,
        production=SafeProduction(**_production(clip.production)),
    )


def _marker_state(marker) -> SafeMarker:
    return SafeMarker(
        id=marker.id, start_frame=marker.start_frame, end_frame=marker.end_frame,
        name=marker.name, description=marker.description, type=marker.type,
        production=SafeProduction(**_production(marker.production)),
    )


def _modified(before: dict[str, Any], after: dict[str, Any], fields: Iterable[str], entity_id: str) -> EntityModification | None:
    changes = _field_changes(before, after, fields)
    return EntityModification(id=entity_id, fields=changes) if changes else None


def _entity_changes(
    before: dict[str, Any], after: dict[str, Any], fields: Iterable[str],
    state: Callable[[Any], Any],
) -> tuple[list[Any], list[Any], list[EntityModification]]:
    added = [state(after[key]) for key in sorted(set(after) - set(before))]
    removed = [state(before[key]) for key in sorted(set(before) - set(after))]
    modified: list[EntityModification] = []
    for key in sorted(set(before) & set(after)):
        change = _modified(before[key][0], after[key][0], fields, key)
        if change is not None:
            modified.append(change)
    return added, removed, modified


def _asset_maps(project: Project):
    return {
        asset.id: ({
            "kind": asset.kind, "name": asset.name, "codec": asset.codec, "width": asset.width, "height": asset.height,
            "fps": _fps(asset.fps), "duration_frames": asset.duration_frames,
            "production/shot_ids": list(asset.production.shot_ids),
            "production/dialogue_line_ids": list(asset.production.dialogue_line_ids),
            "production/generation_job_id": asset.production.generation_job_id,
            "production/external_refs": _refs(asset.production.external_refs),
            "source_location": asset.path,
        }, asset)
        for asset in project.assets
    }


def _track_maps(project: Project):
    return {
        track.id: ({"name": track.name, "kind": track.kind, "position": position,
                    "external_refs": _refs(track.external_refs)}, (track, position))
        for position, track in enumerate(project.timeline.tracks)
    }


def _clip_maps(project: Project):
    result = {}
    for track in project.timeline.tracks:
        for clip in track.clips:
            result[clip.id] = ({
                "kind": clip.kind, "track_id": track.id, "asset_id": clip.asset_id,
                "source_in_frame": clip.source_in_frame, "source_out_frame": clip.source_out_frame,
                "timeline_start_frame": clip.timeline_start_frame,
                "production/shot_ids": list(clip.production.shot_ids),
                "production/dialogue_line_ids": list(clip.production.dialogue_line_ids),
                "production/external_refs": _refs(clip.production.external_refs),
            }, (clip, track.id))
    return result


def _marker_maps(project: Project):
    return {
        marker.id: ({
            "start_frame": marker.start_frame, "end_frame": marker.end_frame,
            "name": marker.name, "description": marker.description, "type": marker.type,
            "production/shot_ids": list(marker.production.shot_ids),
            "production/dialogue_line_ids": list(marker.production.dialogue_line_ids),
            "production/external_refs": _refs(marker.production.external_refs),
        }, marker)
        for marker in project.timeline.markers
    }


def summarize_changes(changes: RevisionChanges) -> RevisionDiffSummary:
    entity_groups = (changes.assets, changes.tracks, changes.clips, changes.markers)
    by_entity_type = RevisionDiffByEntityType(
        assets=EntityTypeSummary(added=len(changes.assets.added), removed=len(changes.assets.removed), modified=len(changes.assets.modified)),
        tracks=EntityTypeSummary(added=len(changes.tracks.added), removed=len(changes.tracks.removed), modified=len(changes.tracks.modified)),
        clips=EntityTypeSummary(added=len(changes.clips.added), removed=len(changes.clips.removed), modified=len(changes.clips.modified)),
        markers=EntityTypeSummary(added=len(changes.markers.added), removed=len(changes.markers.removed), modified=len(changes.markers.modified)),
    )
    return RevisionDiffSummary(
        entities_added=sum(len(group.added) for group in entity_groups),
        entities_removed=sum(len(group.removed) for group in entity_groups),
        entities_modified=sum(len(group.modified) for group in entity_groups),
        fields_modified=len(changes.project.fields) + len(changes.timeline.fields) + sum(len(item.fields) for group in entity_groups for item in group.modified),
        project_fields_modified=len(changes.project.fields),
        timeline_fields_modified=len(changes.timeline.fields),
        by_entity_type=by_entity_type,
    )


def diff_projects(before: Project, after: Project) -> RevisionChanges:
    """Compare only approved canonical fields; never performs I/O or persistence."""
    if before.id != after.id:
        raise RevisionDiffIntegrityError("Compared projects have different identities")
    if before.timeline.id != after.timeline.id:
        raise RevisionDiffIntegrityError("Compared timelines have different identities")

    project_before = {"name": before.name, "fps": _fps(before.fps), "external_refs": _refs(before.external_refs)}
    project_after = {"name": after.name, "fps": _fps(after.fps), "external_refs": _refs(after.external_refs)}
    timeline_before = {"name": before.timeline.name, "external_refs": _refs(before.timeline.external_refs),
                       "video_canvas": None if before.timeline.video_canvas is None else {"width": before.timeline.video_canvas.width, "height": before.timeline.video_canvas.height}}
    timeline_after = {"name": after.timeline.name, "external_refs": _refs(after.timeline.external_refs),
                      "video_canvas": None if after.timeline.video_canvas is None else {"width": after.timeline.video_canvas.width, "height": after.timeline.video_canvas.height}}
    project_fields = _field_changes(project_before, project_after, ("name", "fps", "external_refs"))
    canvas_fields = ()
    if before.assets or any(track.clips for track in before.timeline.tracks):
        if after.assets or any(track.clips for track in after.timeline.tracks):
            canvas_fields = ("video_canvas",)
    timeline_fields = _field_changes(timeline_before, timeline_after, ("name", "external_refs", *canvas_fields))

    assets_before, assets_after = _asset_maps(before), _asset_maps(after)
    asset_fields = ("kind", "name", "codec", "width", "height", "fps", "duration_frames",
                    "production/shot_ids", "production/dialogue_line_ids",
                    "production/generation_job_id", "production/external_refs")
    asset_added, asset_removed, asset_modified = _entity_changes(
        assets_before, assets_after, asset_fields,
        lambda value: _asset_state(value[1]),
    )
    for asset_id in sorted(set(assets_before) & set(assets_after)):
        before_values, before_asset = assets_before[asset_id]
        after_values, after_asset = assets_after[asset_id]
        if before_asset.path != after_asset.path:
            item = next((entry for entry in asset_modified if entry.id == asset_id), None)
            redacted = RedactedFieldChange(path="/source_location", kind="redacted", values_redacted=True)
            if item is None:
                asset_modified.append(EntityModification(id=asset_id, fields=[redacted]))
            else:
                item.fields.append(redacted)
    asset_modified.sort(key=lambda item: item.id)

    tracks_before, tracks_after = _track_maps(before), _track_maps(after)
    track_fields = ("name", "kind", "position", "external_refs")
    track_added, track_removed, track_modified = _entity_changes(
        tracks_before, tracks_after, track_fields,
        lambda value: _track_state(value[1][0], value[1][1]),
    )

    clips_before, clips_after = _clip_maps(before), _clip_maps(after)
    clip_fields = ("kind", "track_id", "asset_id", "source_in_frame", "source_out_frame", "timeline_start_frame",
                   "production/shot_ids", "production/dialogue_line_ids", "production/external_refs")
    clip_added, clip_removed, clip_modified = _entity_changes(
        clips_before, clips_after, clip_fields,
        lambda value: _clip_state(value[1][0], value[1][1]),
    )

    markers_before, markers_after = _marker_maps(before), _marker_maps(after)
    marker_fields = ("start_frame", "end_frame", "name", "description", "type",
                     "production/shot_ids", "production/dialogue_line_ids", "production/external_refs")
    marker_added, marker_removed, marker_modified = _entity_changes(
        markers_before, markers_after, marker_fields, lambda value: _marker_state(value[1]),
    )

    changes = RevisionChanges(
        project=ProjectChanges(fields=project_fields), timeline=TimelineChanges(fields=timeline_fields),
        assets=AssetChanges(added=asset_added, removed=asset_removed, modified=asset_modified),
        tracks=TrackChanges(added=track_added, removed=track_removed, modified=track_modified),
        clips=ClipChanges(added=clip_added, removed=clip_removed, modified=clip_modified),
        markers=MarkerChanges(added=marker_added, removed=marker_removed, modified=marker_modified),
    )
    return changes
