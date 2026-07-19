"""Public edit-guard value objects and canonical mutation footprints."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.domain.models import Project, project_to_dict


GUARD_SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 600
MIN_TTL_SECONDS = 30
MAX_TTL_SECONDS = 3600
MAX_ACTIVE_GUARDS = 128
MAX_SELECTOR_IDS = 100
MAX_INPUT_RANGES = 100


class GuardError(Exception):
    def __init__(self, code: str, message: str, *, conflicts: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.conflicts = conflicts or []


class GuardStateError(GuardError):
    def __init__(self) -> None:
        super().__init__("GUARD_STATE_ERROR", "Edit guard state could not be validated")


@dataclass(frozen=True, order=True)
class FrameRange:
    start_frame: int
    end_frame: int

    def as_dict(self) -> dict[str, int]:
        return {"start_frame": self.start_frame, "end_frame": self.end_frame}


@dataclass(frozen=True)
class GuardScope:
    kind: str
    clip_ids: tuple[str, ...] = ()
    marker_ids: tuple[str, ...] = ()
    frame_ranges: tuple[FrameRange, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        if self.kind == "project":
            return {"kind": "project"}
        return {
            "kind": "selection",
            "clip_ids": list(self.clip_ids),
            "marker_ids": list(self.marker_ids),
            "frame_ranges": [item.as_dict() for item in self.frame_ranges],
        }


@dataclass(frozen=True)
class Owner:
    type: str
    id: str
    display_label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "display_label": self.display_label}


@dataclass(frozen=True)
class EditGuard:
    guard_id: str
    project_id: str
    owner: Owner
    scope: GuardScope
    purpose: str | None
    created_at: str
    expires_at: str
    capability_sha256: str

    def metadata(self) -> dict[str, Any]:
        return {
            "guard_id": self.guard_id,
            "project_id": self.project_id,
            "owner": self.owner.as_dict(),
            "scope": self.scope.as_dict(),
            "purpose": self.purpose,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    def persisted(self) -> dict[str, Any]:
        return {**self.metadata(), "capability_sha256": self.capability_sha256}


@dataclass(frozen=True)
class MutationFootprint:
    project_wide: bool = False
    clip_ids: tuple[str, ...] = ()
    marker_ids: tuple[str, ...] = ()
    frame_ranges: tuple[FrameRange, ...] = ()

    def normalized(self) -> "MutationFootprint":
        return MutationFootprint(
            project_wide=self.project_wide,
            clip_ids=tuple(sorted(set(self.clip_ids))),
            marker_ids=tuple(sorted(set(self.marker_ids))),
            frame_ranges=tuple(_merge_ranges(self.frame_ranges)),
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("invalid time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("time must be timezone aware")
    return parsed.astimezone(timezone.utc)


def _merge_ranges(ranges: tuple[FrameRange, ...] | list[FrameRange]) -> list[FrameRange]:
    result: list[FrameRange] = []
    for current in sorted(ranges, key=lambda item: (item.start_frame, item.end_frame)):
        if result and current.start_frame <= result[-1].end_frame:
            result[-1] = FrameRange(result[-1].start_frame, max(result[-1].end_frame, current.end_frame))
        else:
            result.append(current)
    return result


def _strict_frame(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GuardError("INVALID_GUARD_SCOPE", "Frame ranges require non-negative integer frames")
    return value


def normalize_owner(value: Any) -> Owner:
    if not isinstance(value, dict) or set(value) - {"type", "id", "display_label"}:
        raise GuardError("INVALID_GUARD_SCOPE", "Owner metadata is invalid")
    owner_type, owner_id = value.get("type"), value.get("id")
    label = value.get("display_label")
    if owner_type not in {"human", "agent"}:
        raise GuardError("INVALID_GUARD_SCOPE", "Owner type is invalid")
    if not isinstance(owner_id, str) or not 1 <= len(owner_id) <= 128:
        raise GuardError("INVALID_GUARD_SCOPE", "Owner id is invalid")
    if label is not None and (not isinstance(label, str) or len(label) > 160):
        raise GuardError("INVALID_GUARD_SCOPE", "Owner display_label is invalid")
    return Owner(owner_type, owner_id, label)


def normalize_scope(value: Any, project: Project) -> GuardScope:
    if not isinstance(value, dict):
        raise GuardError("INVALID_GUARD_SCOPE", "Guard scope is invalid")
    kind = value.get("kind")
    if kind == "project":
        if set(value) != {"kind"}:
            raise GuardError("INVALID_GUARD_SCOPE", "Project scope cannot contain selectors")
        return GuardScope("project")
    if kind != "selection" or set(value) - {"kind", "clip_ids", "marker_ids", "frame_ranges"}:
        raise GuardError("INVALID_GUARD_SCOPE", "Guard scope is invalid")
    clip_ids = value.get("clip_ids", [])
    marker_ids = value.get("marker_ids", [])
    ranges = value.get("frame_ranges", [])
    if not isinstance(clip_ids, list) or not isinstance(marker_ids, list) or not isinstance(ranges, list):
        raise GuardError("INVALID_GUARD_SCOPE", "Guard selectors must be arrays")
    if len(clip_ids) > MAX_SELECTOR_IDS or len(marker_ids) > MAX_SELECTOR_IDS or len(ranges) > MAX_INPUT_RANGES:
        raise GuardError("GUARD_LIMIT_EXCEEDED", "Guard selector limit exceeded")
    if any(not isinstance(item, str) or not item for item in clip_ids + marker_ids):
        raise GuardError("INVALID_GUARD_SCOPE", "Guard entity selectors are invalid")
    known_clips = {clip.id for track in project.timeline.tracks for clip in track.clips}
    known_markers = {marker.id for marker in project.timeline.markers}
    if set(clip_ids) - known_clips:
        raise GuardError("INVALID_GUARD_SCOPE", "Guard clip selector does not exist")
    if set(marker_ids) - known_markers:
        raise GuardError("INVALID_GUARD_SCOPE", "Guard marker selector does not exist")
    parsed_ranges: list[FrameRange] = []
    for item in ranges:
        if not isinstance(item, dict) or set(item) != {"start_frame", "end_frame"}:
            raise GuardError("INVALID_GUARD_SCOPE", "Guard frame range is invalid")
        start, end = _strict_frame(item["start_frame"]), _strict_frame(item["end_frame"])
        if end <= start:
            raise GuardError("INVALID_GUARD_SCOPE", "Guard frame ranges must be non-empty")
        parsed_ranges.append(FrameRange(start, end))
    normalized = GuardScope(
        "selection", tuple(sorted(set(clip_ids))), tuple(sorted(set(marker_ids))), tuple(_merge_ranges(parsed_ranges))
    )
    if not normalized.clip_ids and not normalized.marker_ids and not normalized.frame_ranges:
        raise GuardError("INVALID_GUARD_SCOPE", "Selection scope cannot be empty")
    return normalized


def normalize_ttl(value: Any) -> int:
    if value is None:
        return DEFAULT_TTL_SECONDS
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_TTL_SECONDS <= value <= MAX_TTL_SECONDS:
        raise GuardError("INVALID_GUARD_TTL", "Guard TTL must be an integer from 30 to 3600 seconds")
    return value


def normalize_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(token, str) or not token for token in value):
        raise GuardError("INVALID_GUARD_AUTHORIZATION", "Guard capabilities are invalid")
    return list(dict.fromkeys(value))


def _canonical_without_revision(project: Project) -> dict[str, Any]:
    value = project_to_dict(project)
    value.pop("revision", None)
    value.pop("revision_id", None)
    return value


def mutation_footprint(before: Project, final: Project) -> MutationFootprint:
    """Derive the atomic before-to-final canonical effect, ignoring revision identity."""
    before_data, final_data = _canonical_without_revision(before), _canonical_without_revision(final)
    project_wide = (
        before_data["id"] != final_data["id"] or before_data["name"] != final_data["name"] or
        before_data["fps"] != final_data["fps"] or before_data["external_refs"] != final_data["external_refs"] or
        before_data["assets"] != final_data["assets"] or
        before_data["timeline"]["id"] != final_data["timeline"]["id"] or
        before_data["timeline"]["name"] != final_data["timeline"]["name"] or
        before_data["timeline"]["external_refs"] != final_data["timeline"]["external_refs"] or
        [(track["id"], track["name"], track["kind"]) for track in before_data["timeline"]["tracks"]] !=
        [(track["id"], track["name"], track["kind"]) for track in final_data["timeline"]["tracks"]]
    )
    before_clips = {clip["id"]: {**clip, "_track_id": track["id"]}
                    for track in before_data["timeline"]["tracks"] for clip in track["clips"]}
    final_clips = {clip["id"]: {**clip, "_track_id": track["id"]}
                   for track in final_data["timeline"]["tracks"] for clip in track["clips"]}
    before_markers = {marker["id"]: marker for marker in before_data["timeline"]["markers"]}
    final_markers = {marker["id"]: marker for marker in final_data["timeline"]["markers"]}
    clip_spans = []
    clip_ids = []
    for entity_id in sorted(set(before_clips) | set(final_clips)):
        if before_clips.get(entity_id) != final_clips.get(entity_id):
            clip_ids.append(entity_id)
            for value in (before_clips.get(entity_id), final_clips.get(entity_id)):
                if value is not None:
                    clip_spans.append(FrameRange(value["timeline_start_frame"], value["timeline_start_frame"] + value["source_out_frame"] - value["source_in_frame"]))
    marker_spans = []
    marker_ids = []
    for entity_id in sorted(set(before_markers) | set(final_markers)):
        if before_markers.get(entity_id) != final_markers.get(entity_id):
            marker_ids.append(entity_id)
            for value in (before_markers.get(entity_id), final_markers.get(entity_id)):
                if value is not None:
                    marker_spans.append(FrameRange(value["start_frame"], value["end_frame"] if value["end_frame"] is not None else value["start_frame"] + 1))
    return MutationFootprint(project_wide, tuple(clip_ids), tuple(marker_ids), tuple(_merge_ranges(clip_spans + marker_spans))).normalized()
