from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Optional
from uuid import uuid4


class ValidationError(ValueError):
    pass


class TimelineConflictError(ValidationError):
    pass


def validate_revision_id(revision_id: str) -> None:
    """Reject revision identifiers that could escape the revision store."""
    if not isinstance(revision_id, str) or not re.fullmatch(r"revision_[A-Za-z0-9_-]{1,127}", revision_id):
        raise ValidationError("Invalid revision id")


@dataclass
class FrameRate:
    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValidationError("Frame rate must be positive")

    def as_tuple(self) -> tuple[int, int]:
        return self.numerator, self.denominator


@dataclass
class ExternalReference:
    system: str
    id: str
    kind: str = ""

    def __post_init__(self) -> None:
        if not self.system or not self.id:
            raise ValidationError("External references need a system and id")


@dataclass
class AssetProductionMetadata:
    shot_ids: list[str] = field(default_factory=list)
    dialogue_line_ids: list[str] = field(default_factory=list)
    generation_job_id: Optional[str] = None
    external_refs: list[ExternalReference] = field(default_factory=list)


@dataclass
class ClipProductionMetadata:
    shot_ids: list[str] = field(default_factory=list)
    dialogue_line_ids: list[str] = field(default_factory=list)
    external_refs: list[ExternalReference] = field(default_factory=list)


@dataclass
class MarkerProductionMetadata:
    shot_ids: list[str] = field(default_factory=list)
    dialogue_line_ids: list[str] = field(default_factory=list)
    external_refs: list[ExternalReference] = field(default_factory=list)


def empty_asset_production() -> AssetProductionMetadata:
    return AssetProductionMetadata()


def empty_clip_production() -> ClipProductionMetadata:
    return ClipProductionMetadata()


def empty_marker_production() -> MarkerProductionMetadata:
    return MarkerProductionMetadata()


@dataclass
class Asset:
    id: str
    path: str
    name: str
    codec: str
    width: int
    height: int
    fps: FrameRate
    duration_frames: int
    production: AssetProductionMetadata = field(default_factory=empty_asset_production)


@dataclass
class Clip:
    id: str
    asset_id: str
    source_in_frame: int
    source_out_frame: int
    timeline_start_frame: int
    production: ClipProductionMetadata = field(default_factory=empty_clip_production)

    def __post_init__(self) -> None:
        if self.source_in_frame < 0 or self.source_out_frame <= self.source_in_frame:
            raise ValidationError("Clip source range must be non-empty and non-negative")
        if self.timeline_start_frame < 0:
            raise ValidationError("Timeline start must be non-negative")

    @property
    def duration_frames(self) -> int:
        return self.source_out_frame - self.source_in_frame


@dataclass
class Track:
    id: str
    name: str
    kind: str = "video"
    clips: list[Clip] = field(default_factory=list)


@dataclass
class Marker:
    id: str
    start_frame: int
    end_frame: Optional[int] = None
    name: str = ""
    description: str = ""
    type: str = "generic"
    production: MarkerProductionMetadata = field(default_factory=empty_marker_production)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not re.fullmatch(r"marker_[0-9a-f]{32}", self.id):
            raise ValidationError("Invalid marker id")
        if isinstance(self.start_frame, bool) or not isinstance(self.start_frame, int) or self.start_frame < 0:
            raise ValidationError("Marker start_frame must be a non-negative integer")
        if self.end_frame is not None:
            if isinstance(self.end_frame, bool) or not isinstance(self.end_frame, int):
                raise ValidationError("Marker end_frame must be an integer or null")
            if self.end_frame <= self.start_frame:
                raise ValidationError("Marker end_frame must be greater than start_frame")
        if not isinstance(self.name, str):
            raise ValidationError("Marker name must be a string")
        self.name = self.name.strip()
        if not 1 <= len(self.name) <= 160:
            raise ValidationError("Marker name must be 1-160 characters")
        if not isinstance(self.description, str) or len(self.description) > 2000:
            raise ValidationError("Marker description must be at most 2000 characters")
        if not isinstance(self.type, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.type):
            raise ValidationError("Invalid marker type")
        if not isinstance(self.production, MarkerProductionMetadata):
            raise ValidationError("Invalid marker production metadata")


def marker_sort_key(marker: Marker | dict[str, Any]) -> tuple[int, int, str]:
    if isinstance(marker, Marker):
        end_frame = marker.end_frame
        return marker.start_frame, marker.start_frame if end_frame is None else end_frame, marker.id
    end_frame = marker.get("end_frame")
    return marker["start_frame"], marker["start_frame"] if end_frame is None else end_frame, marker["id"]


@dataclass
class Timeline:
    id: str
    name: str = "Main timeline"
    external_refs: list[ExternalReference] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)


@dataclass
class Project:
    id: str
    name: str
    fps: Optional[FrameRate] = None
    revision: int = 0
    revision_id: str = ""
    external_refs: list[ExternalReference] = field(default_factory=list)
    assets: list[Asset] = field(default_factory=list)
    timeline: Optional[Timeline] = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        if self.timeline is None:
            self.timeline = Timeline(id=f"timeline_{uuid4().hex}", tracks=[])
        if not self.revision_id:
            self.revision_id = f"revision_{uuid4().hex}"

    @property
    def tracks(self) -> list[Track]:
        """Temporary Python compatibility view; timeline.tracks is canonical."""
        assert self.timeline is not None
        return self.timeline.tracks

    @tracks.setter
    def tracks(self, value: list[Track]) -> None:
        assert self.timeline is not None
        self.timeline.tracks = value

    def validate(self) -> None:
        if self.schema_version != 2:
            raise ValidationError("Unsupported project schema version")
        if not self.id or not self.revision_id or self.timeline is None or not self.timeline.id:
            raise ValidationError("Project identity is incomplete")
        validate_revision_id(self.revision_id)
        if self.revision < 0:
            raise ValidationError("Revision cannot be negative")
        ids: list[str] = [self.id, self.timeline.id]
        ids.extend(a.id for a in self.assets)
        ids.extend(t.id for t in self.tracks)
        ids.extend(c.id for t in self.tracks for c in t.clips)
        for marker in self.timeline.markers:
            if not isinstance(marker, Marker):
                raise ValidationError("Timeline markers must be typed Marker values")
            marker.__post_init__()
            _validate_marker_production(marker.production)
        ids.extend(marker.id for marker in self.timeline.markers)
        if any(not item for item in ids) or len(ids) != len(set(ids)):
            raise ValidationError("All project entities need unique opaque IDs")
        if self.fps is None and self.assets:
            raise ValidationError("A project with assets must have an FPS")
        if self.fps:
            for asset in self.assets:
                if asset.fps.as_tuple() != self.fps.as_tuple():
                    raise ValidationError("Asset FPS must match project FPS")
        asset_ids = {a.id for a in self.assets}
        asset_by_id = {a.id: a for a in self.assets}
        for track in self.tracks:
            ordered_clips = sorted(track.clips, key=lambda clip: (clip.timeline_start_frame, clip.id))
            for previous, current in zip(ordered_clips, ordered_clips[1:]):
                previous_end = previous.timeline_start_frame + previous.duration_frames
                if current.timeline_start_frame < previous_end:
                    raise TimelineConflictError(f"Clips {previous.id} and {current.id} overlap on {track.name}")
            for clip in track.clips:
                if clip.asset_id not in asset_ids:
                    raise ValidationError("Clip references an unknown asset")
                if clip.source_in_frame < 0 or clip.source_out_frame <= clip.source_in_frame:
                    raise ValidationError("Clip source range must be non-empty and non-negative")
                if clip.timeline_start_frame < 0:
                    raise ValidationError("Timeline start must be non-negative")
                if clip.source_out_frame > asset_by_id[clip.asset_id].duration_frames:
                    raise ValidationError("Clip source range exceeds asset duration")


def project_to_dict(project: Project) -> dict[str, Any]:
    project.validate()
    data = asdict(project)
    data["timeline"]["markers"] = sorted(data["timeline"]["markers"], key=marker_sort_key)
    # `tracks` is intentionally absent: timeline.tracks is the only canonical collection.
    return data


def _external_refs(items: list[dict[str, Any]] | None) -> list[ExternalReference]:
    references = []
    for item in (items or []):
        if not isinstance(item, dict):
            raise ValidationError("External references must be objects")
        _reject_unknown(item, {"system", "id", "kind"}, "external_refs")
        references.append(ExternalReference(**item))
    return references


def _asset_production(data: dict[str, Any] | None) -> AssetProductionMetadata:
    data = data or {}
    _reject_unknown(data, {"shot_ids", "dialogue_line_ids", "generation_job_id", "external_refs"}, "production")
    return AssetProductionMetadata(
        shot_ids=list(data.get("shot_ids", [])),
        dialogue_line_ids=list(data.get("dialogue_line_ids", [])),
        generation_job_id=data.get("generation_job_id"),
        external_refs=_external_refs(data.get("external_refs")),
    )


def _clip_production(data: dict[str, Any] | None) -> ClipProductionMetadata:
    data = data or {}
    _reject_unknown(data, {"shot_ids", "dialogue_line_ids", "external_refs"}, "production")
    return ClipProductionMetadata(
        shot_ids=list(data.get("shot_ids", [])),
        dialogue_line_ids=list(data.get("dialogue_line_ids", [])),
        external_refs=_external_refs(data.get("external_refs")),
    )


def _marker_production(data: dict[str, Any] | None) -> MarkerProductionMetadata:
    data = data or {}
    if not isinstance(data, dict):
        raise ValidationError("Marker production must be an object")
    _reject_unknown(data, {"shot_ids", "dialogue_line_ids", "external_refs"}, "marker.production")
    return MarkerProductionMetadata(
        shot_ids=list(data.get("shot_ids", [])),
        dialogue_line_ids=list(data.get("dialogue_line_ids", [])),
        external_refs=_external_refs(data.get("external_refs")),
    )


def _validate_marker_production(production: MarkerProductionMetadata) -> None:
    for field_name, values in (("shot_ids", production.shot_ids), ("dialogue_line_ids", production.dialogue_line_ids)):
        if any(not isinstance(value, str) or not value for value in values) or len(values) != len(set(values)):
            raise ValidationError(f"Marker production {field_name} must contain unique non-empty strings")
    external_keys = [(item.system, item.id, item.kind) for item in production.external_refs]
    if len(external_keys) != len(set(external_keys)):
        raise ValidationError("Marker production external_refs must be unique")


def marker_production_from_dict(data: dict[str, Any] | MarkerProductionMetadata | None) -> MarkerProductionMetadata:
    if isinstance(data, MarkerProductionMetadata):
        production = data
    else:
        production = _marker_production(data)
    _validate_marker_production(production)
    return production


def _reject_unknown(data: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValidationError(f"Unknown field at {path}.{unknown[0]}")


def _project_from_v2(data: dict[str, Any]) -> Project:
    _reject_unknown(data, {"schema_version", "id", "name", "fps", "revision", "revision_id", "external_refs", "assets", "timeline", "tracks", "timeline_id"}, "project")
    if data.get("schema_version") != 2:
        raise ValidationError("Invalid schema_version")
    fps_data = data.get("fps")
    fps = FrameRate(**fps_data) if fps_data else None
    assets = []
    for index, item in enumerate(data.get("assets", [])):
        _reject_unknown(item, {"id", "path", "name", "codec", "width", "height", "fps", "duration_frames", "production"}, f"project.assets[{index}]")
        assets.append(Asset(**{**item, "fps": FrameRate(**item["fps"]), "production": _asset_production(item.get("production"))}))
    timeline_data = data.get("timeline")
    if not isinstance(timeline_data, dict):
        raise ValidationError("Invalid field at project.timeline")
    _reject_unknown(timeline_data, {"id", "name", "external_refs", "tracks", "markers"}, "project.timeline")
    tracks = []
    for track_index, item in enumerate(timeline_data.get("tracks", [])):
        _reject_unknown(item, {"id", "name", "kind", "clips"}, f"project.timeline.tracks[{track_index}]")
        clips = []
        for clip_index, clip_data in enumerate(item.get("clips", [])):
            _reject_unknown(clip_data, {"id", "asset_id", "source_in_frame", "source_out_frame", "timeline_start_frame", "production"}, f"project.timeline.tracks[{track_index}].clips[{clip_index}]")
            clips.append(Clip(**{**clip_data, "production": _clip_production(clip_data.get("production"))}))
        tracks.append(Track(id=item["id"], name=item["name"], kind=item.get("kind", "video"), clips=clips))
    markers = []
    for marker_index, marker_data in enumerate(timeline_data.get("markers", [])):
        if not isinstance(marker_data, dict):
            raise ValidationError(f"Invalid marker at project.timeline.markers[{marker_index}]")
        _reject_unknown(marker_data, {"id", "start_frame", "end_frame", "name", "description", "type", "production"}, f"project.timeline.markers[{marker_index}]")
        required = {"id", "start_frame", "name"} - set(marker_data)
        if required:
            raise ValidationError(f"Missing marker field: {sorted(required)[0]}")
        production = _marker_production(marker_data.get("production"))
        _validate_marker_production(production)
        markers.append(Marker(**{**marker_data, "production": production}))
    timeline = Timeline(id=timeline_data["id"], name=timeline_data.get("name", "Main timeline"), external_refs=_external_refs(timeline_data.get("external_refs")), tracks=tracks, markers=sorted(markers, key=marker_sort_key))
    if "timeline_id" in data and data["timeline_id"] != timeline.id:
        raise ValidationError("Project timeline_id does not match timeline.id")
    if "tracks" in data and data["tracks"] != timeline_data.get("tracks"):
        raise ValidationError("Project tracks alias does not match timeline.tracks")
    project = Project(id=data["id"], name=data["name"], fps=fps, revision=data.get("revision", 0), revision_id=data["revision_id"], external_refs=_external_refs(data.get("external_refs")), assets=assets, timeline=timeline, schema_version=2)
    project.validate()
    return project


def project_from_dict(data: dict[str, Any]) -> Project:
    if not isinstance(data, dict):
        raise ValidationError("Project document must be an object")
    schema_version = data.get("schema_version", 1)
    if schema_version != 2:
        from app.persistence.migrations import migrate_document
        data = migrate_document(data)
    return _project_from_v2(data)
