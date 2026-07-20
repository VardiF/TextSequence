from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.models import Project, ValidationError, marker_sort_key
from app.services.projections import clip_projection, marker_projection


class QueryValidationError(ValidationError):
    pass


@dataclass(frozen=True)
class TimelineQuery:
    entity_types: tuple[str, ...] = ()
    frame: int | None = None
    frame_range: tuple[int, int] | None = None
    asset_id: str | None = None
    marker_type: str | None = None
    shot_id: str | None = None
    dialogue_line_id: str | None = None
    external_ref: dict[str, str] | None = None


def _nonnegative(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QueryValidationError(f"{name} must be a non-negative integer")
    return value


def normalize_query(value: dict[str, Any]) -> TimelineQuery:
    if not isinstance(value, dict):
        raise QueryValidationError("query must be an object")
    allowed = {"entity_types", "frame", "frame_range", "asset_id", "marker_type", "shot_id", "dialogue_line_id", "external_ref"}
    unknown = set(value) - allowed
    if unknown:
        raise QueryValidationError("query contains unsupported fields")
    entity_types = value.get("entity_types", ())
    if isinstance(entity_types, str) or not isinstance(entity_types, (list, tuple)):
        raise QueryValidationError("entity_types must be a list")
    entity_types = tuple(entity_types)
    if not entity_types or any(item not in {"clip", "marker"} for item in entity_types):
        raise QueryValidationError("entity_types must contain clip or marker")
    frame = value.get("frame")
    frame_range = value.get("frame_range")
    if frame is not None and frame_range is not None:
        raise QueryValidationError("frame and frame_range are mutually exclusive")
    if frame is not None:
        frame = _nonnegative(frame, "frame")
    if frame_range is not None:
        if not isinstance(frame_range, dict) or set(frame_range) != {"start_frame", "end_frame"}:
            raise QueryValidationError("frame_range must contain start_frame and end_frame")
        start = _nonnegative(frame_range["start_frame"], "start_frame")
        end = _nonnegative(frame_range["end_frame"], "end_frame")
        if end <= start:
            raise QueryValidationError("frame_range end_frame must be greater than start_frame")
        frame_range = (start, end)
    external = value.get("external_ref")
    if external is not None:
        if not isinstance(external, dict) or set(external) != {"system", "id", "kind"} or not isinstance(external["system"], str) or not external["system"] or not isinstance(external["id"], str) or not external["id"] or not isinstance(external["kind"], str):
            raise QueryValidationError("external_ref must contain system, id, and kind")
        external = dict(external)
    substantive = (frame is not None or frame_range is not None or any(value.get(key) is not None for key in ("asset_id", "marker_type", "shot_id", "dialogue_line_id", "external_ref")))
    if not substantive:
        raise QueryValidationError("query requires at least one substantive filter")
    return TimelineQuery(entity_types, frame, frame_range, value.get("asset_id"), value.get("marker_type"),
                         value.get("shot_id"), value.get("dialogue_line_id"), external)


def _production_matches(value, query: TimelineQuery) -> bool:
    if query.shot_id is not None and query.shot_id not in value.shot_ids: return False
    if query.dialogue_line_id is not None and query.dialogue_line_id not in value.dialogue_line_ids: return False
    if query.external_ref is not None and not any(all(getattr(ref, key) == query.external_ref[key] for key in query.external_ref) for ref in value.external_refs): return False
    return True


def _range_matches(start: int, end: int, query: TimelineQuery) -> bool:
    if query.frame is not None and not (start <= query.frame < end): return False
    if query.frame_range is not None and not (start < query.frame_range[1] and query.frame_range[0] < end): return False
    return True


def query_timeline(project: Project, raw_query: dict[str, Any]) -> dict:
    query = normalize_query(raw_query)
    assets = {asset.id: asset for asset in project.assets}
    clips = []
    markers = []
    if "clip" in query.entity_types:
        for track in project.timeline.tracks:
            for clip in track.clips:
                if query.asset_id is not None and clip.asset_id != query.asset_id: continue
                if not _range_matches(clip.timeline_start_frame, clip.timeline_start_frame + clip.duration_frames, query): continue
                if not _production_matches(clip.production, query): continue
                clips.append(clip_projection(clip, track, assets))
    if "marker" in query.entity_types:
        for marker in sorted(project.timeline.markers, key=marker_sort_key):
            end = marker.end_frame if marker.end_frame is not None else marker.start_frame + 1
            if query.marker_type is not None and marker.type != query.marker_type: continue
            if not _range_matches(marker.start_frame, end, query): continue
            if not _production_matches(marker.production, query): continue
            markers.append(marker_projection(marker))
    clips.sort(key=lambda item: (item["timeline_start_frame"], item["id"]))
    return {"project_id": project.id, "revision": project.revision, "revision_id": project.revision_id,
            "schema_version": project.schema_version,
            "video_canvas": None if project.timeline.video_canvas is None else {"width": project.timeline.video_canvas.width, "height": project.timeline.video_canvas.height},
            "query": raw_query, "clips": clips, "markers": markers, "result_count": len(clips) + len(markers)}
