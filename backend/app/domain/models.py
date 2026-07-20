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
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.numerator, self.denominator)):
            raise ValidationError("Frame rate values must be integers")
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
    kind: str = "video"

    def __post_init__(self) -> None:
        if self.kind != "video":
            raise ValidationError("Asset kind must be video")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.width, self.height, self.duration_frames)):
            raise ValidationError("Asset dimensions and duration must be integers")
        if self.width <= 0 or self.height <= 0 or self.duration_frames <= 0:
            raise ValidationError("Asset dimensions and duration must be positive")


@dataclass
class Clip:
    id: str
    asset_id: str
    source_in_frame: int
    source_out_frame: int
    timeline_start_frame: int
    production: ClipProductionMetadata = field(default_factory=empty_clip_production)
    kind: str = "video"

    def __post_init__(self) -> None:
        if self.kind != "video":
            raise ValidationError("Clip kind must be video")
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.source_in_frame, self.source_out_frame, self.timeline_start_frame)):
            raise ValidationError("Clip frame positions must be integers")
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
    external_refs: list[ExternalReference] = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind != "video":
            raise ValidationError("Track kind must be video")


@dataclass
class VideoCanvas:
    width: int
    height: int

    def __post_init__(self) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int) for value in (self.width, self.height)):
            raise ValidationError("Video canvas dimensions must be integers")
        if self.width <= 0 or self.height <= 0:
            raise ValidationError("Video canvas dimensions must be positive")


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
    video_canvas: Optional[VideoCanvas] = None
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
    schema_version: int = 3

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
        if self.schema_version != 3:
            raise ValidationError("Unsupported project schema version")
        if not self.id or not self.revision_id or self.timeline is None or not self.timeline.id:
            raise ValidationError("Project identity is incomplete")
        validate_revision_id(self.revision_id)
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise ValidationError("Revision cannot be negative")
        if not 1 <= len(self.tracks) <= 64:
            raise ValidationError("A project must contain 1-64 video tracks")
        if self.timeline.video_canvas is not None and not isinstance(self.timeline.video_canvas, VideoCanvas):
            raise ValidationError("Invalid video canvas")
        ids: list[str] = [self.id, self.timeline.id]
        ids.extend(a.id for a in self.assets)
        ids.extend(t.id for t in self.tracks)
        if len({track.id for track in self.tracks}) != len(self.tracks):
            raise ValidationError("Track IDs must be unique")
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
        for refs, label in ((self.external_refs, "project"), (self.timeline.external_refs, "timeline")):
            if any(not isinstance(ref, ExternalReference) or not ref.system or not ref.id for ref in refs):
                raise ValidationError(f"{label} external_refs are invalid")
            keys = [(ref.system, ref.id, ref.kind) for ref in refs]
            if len(keys) != len(set(keys)):
                raise ValidationError(f"{label} external_refs must be unique")
        has_video = bool(self.assets or any(track.clips for track in self.tracks))
        # Domain constructors used by older callers may append an asset before
        # validation. Establish the canonical canvas deterministically in that
        # in-memory case; persisted v3 parsing rejects a missing canvas before
        # reaching this compatibility convenience.
        if has_video and self.timeline.video_canvas is None and self.assets:
            first_asset = self.assets[0]
            self.timeline.video_canvas = VideoCanvas(first_asset.width, first_asset.height)
        if has_video and self.timeline.video_canvas is None:
            raise ValidationError("Video canvas is required when video exists")
        if not has_video and self.timeline.video_canvas is not None:
            # Older in-memory mutation helpers may remove the last asset while
            # retaining the derived canvas. Canonical serialization normalizes
            # that transient state to the required null value.
            self.timeline.video_canvas = None
        asset_ids = {a.id for a in self.assets}
        asset_by_id = {a.id: a for a in self.assets}
        for track in self.tracks:
            if track.kind != "video":
                raise ValidationError("Track kind must be video")
            if any(not isinstance(ref, ExternalReference) or not ref.system or not ref.id for ref in track.external_refs):
                raise ValidationError("Track external_refs are invalid")
            track_keys = [(ref.system, ref.id, ref.kind) for ref in track.external_refs]
            if len(track_keys) != len(set(track_keys)):
                raise ValidationError("Track external_refs must be unique")
            ordered_clips = sorted(track.clips, key=lambda clip: (clip.timeline_start_frame, clip.id))
            if track.clips != ordered_clips:
                raise ValidationError("Clips must be sorted by timeline position and ID")
            for previous, current in zip(ordered_clips, ordered_clips[1:]):
                previous_end = previous.timeline_start_frame + previous.duration_frames
                if current.timeline_start_frame < previous_end:
                    raise TimelineConflictError(f"Clips {previous.id} and {current.id} overlap on {track.name}")
            for clip in track.clips:
                if clip.kind != "video":
                    raise ValidationError("Clip kind must be video")
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


def _canvas_for_assets(assets: list[dict[str, Any]], tracks: list[dict[str, Any]]) -> dict[str, int] | None:
    if not assets and not any(track.get("clips") for track in tracks):
        return None
    if not assets:
        raise ValidationError("Video canvas cannot be derived without assets")
    first = assets[0]
    return {"width": first["width"], "height": first["height"]}


def _deterministic_v1_track_id(project_id: str) -> str:
    import hashlib
    return f"track_{hashlib.sha256(f'textsequence:v3:track:{project_id}:V1'.encode()).hexdigest()[:32]}"


def _project_from_v2(data: dict[str, Any]) -> Project:
    _reject_unknown(data, {"schema_version", "id", "name", "fps", "revision", "revision_id", "external_refs", "assets", "timeline", "tracks", "timeline_id"}, "project")
    if data.get("schema_version") != 2:
        raise ValidationError("Invalid schema_version")
    fps_data = data.get("fps")
    fps = FrameRate(**fps_data) if fps_data else None
    assets = []
    for index, item in enumerate(data.get("assets", [])):
        _reject_unknown(item, {"id", "path", "name", "codec", "width", "height", "fps", "duration_frames", "production"}, f"project.assets[{index}]")
        assets.append(Asset(**{**item, "fps": FrameRate(**item["fps"]), "production": _asset_production(item.get("production")), "kind": "video"}))
    timeline_data = data.get("timeline")
    if not isinstance(timeline_data, dict):
        raise ValidationError("Invalid field at project.timeline")
    _reject_unknown(timeline_data, {"id", "name", "external_refs", "tracks", "markers"}, "project.timeline")
    tracks = []
    for track_index, item in enumerate(timeline_data.get("tracks", [])):
        _reject_unknown(item, {"id", "name", "kind", "external_refs", "clips"}, f"project.timeline.tracks[{track_index}]")
        if item.get("kind", "video") != "video":
            raise ValidationError("Unsupported track kind")
        clips = []
        for clip_index, clip_data in enumerate(item.get("clips", [])):
            _reject_unknown(clip_data, {"id", "asset_id", "source_in_frame", "source_out_frame", "timeline_start_frame", "production"}, f"project.timeline.tracks[{track_index}].clips[{clip_index}]")
            clips.append(Clip(**{**clip_data, "production": _clip_production(clip_data.get("production")), "kind": "video"}))
        clips.sort(key=lambda clip: (clip.timeline_start_frame, clip.id))
        tracks.append(Track(id=item["id"], name=item["name"], kind="video", external_refs=_external_refs(item.get("external_refs")), clips=clips))
    if not tracks:
        tracks.append(Track(id=_deterministic_v1_track_id(data["id"]), name="V1"))
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
    timeline = Timeline(id=timeline_data["id"], name=timeline_data.get("name", "Main timeline"), external_refs=_external_refs(timeline_data.get("external_refs")), video_canvas=None, tracks=tracks, markers=sorted(markers, key=marker_sort_key))
    if "timeline_id" in data and data["timeline_id"] != timeline.id:
        raise ValidationError("Project timeline_id does not match timeline.id")
    if "tracks" in data and data["tracks"] != timeline_data.get("tracks"):
        raise ValidationError("Project tracks alias does not match timeline.tracks")
    canvas = _canvas_for_assets(data.get("assets", []), data.get("timeline", {}).get("tracks", []))
    timeline.video_canvas = VideoCanvas(**canvas) if canvas else None
    project = Project(id=data["id"], name=data["name"], fps=fps, revision=data.get("revision", 0), revision_id=data["revision_id"], external_refs=_external_refs(data.get("external_refs")), assets=assets, timeline=timeline, schema_version=3)
    project.validate()
    return project


def _project_from_v3(data: dict[str, Any]) -> Project:
    _reject_unknown(data, {"schema_version", "id", "name", "fps", "revision", "revision_id", "external_refs", "assets", "timeline", "tracks", "timeline_id"}, "project")
    if data.get("schema_version") != 3:
        raise ValidationError("Invalid schema_version")
    required = {"schema_version", "id", "name", "fps", "revision", "revision_id", "external_refs", "assets", "timeline"} - set(data)
    if required:
        raise ValidationError(f"Missing project field: {sorted(required)[0]}")
    fps_data = data.get("fps")
    fps = FrameRate(**fps_data) if fps_data else None
    assets = []
    for index, item in enumerate(data.get("assets", [])):
        _reject_unknown(item, {"id", "kind", "path", "name", "codec", "width", "height", "fps", "duration_frames", "production"}, f"project.assets[{index}]")
        if item.get("kind") != "video":
            raise ValidationError("Asset kind must be video")
        assets.append(Asset(**{**item, "fps": FrameRate(**item["fps"]), "production": _asset_production(item.get("production"))}))
    timeline_data = data.get("timeline")
    if not isinstance(timeline_data, dict):
        raise ValidationError("Invalid field at project.timeline")
    _reject_unknown(timeline_data, {"id", "name", "external_refs", "video_canvas", "tracks", "markers"}, "project.timeline")
    required_timeline = {"id", "name", "external_refs", "video_canvas", "tracks", "markers"} - set(timeline_data)
    if required_timeline:
        raise ValidationError(f"Missing timeline field: {sorted(required_timeline)[0]}")
    canvas_data = timeline_data.get("video_canvas")
    canvas = VideoCanvas(**canvas_data) if canvas_data is not None else None
    if (assets or any(item.get("clips") for item in timeline_data.get("tracks", []))) and canvas is None:
        raise ValidationError("Video canvas is required when video exists")
    if not assets and not any(item.get("clips") for item in timeline_data.get("tracks", [])) and canvas is not None:
        raise ValidationError("Video canvas requires video content")
    tracks = []
    for track_index, item in enumerate(timeline_data.get("tracks", [])):
        _reject_unknown(item, {"id", "name", "kind", "external_refs", "clips"}, f"project.timeline.tracks[{track_index}]")
        required_track = {"id", "name", "kind", "external_refs", "clips"} - set(item)
        if required_track:
            raise ValidationError(f"Missing track field: {sorted(required_track)[0]}")
        if item.get("kind") != "video":
            raise ValidationError("Track kind must be video")
        clips = []
        for clip_index, clip_data in enumerate(item.get("clips", [])):
            _reject_unknown(clip_data, {"id", "kind", "asset_id", "source_in_frame", "source_out_frame", "timeline_start_frame", "production"}, f"project.timeline.tracks[{track_index}].clips[{clip_index}]")
            required_clip = {"id", "kind", "asset_id", "source_in_frame", "source_out_frame", "timeline_start_frame", "production"} - set(clip_data)
            if required_clip:
                raise ValidationError(f"Missing clip field: {sorted(required_clip)[0]}")
            if clip_data.get("kind") != "video":
                raise ValidationError("Clip kind must be video")
            clips.append(Clip(**{**clip_data, "production": _clip_production(clip_data.get("production"))}))
        tracks.append(Track(id=item["id"], name=item["name"], kind=item["kind"], external_refs=_external_refs(item.get("external_refs")), clips=clips))
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
    timeline = Timeline(id=timeline_data["id"], name=timeline_data.get("name", "Main timeline"), external_refs=_external_refs(timeline_data.get("external_refs")), video_canvas=canvas, tracks=tracks, markers=sorted(markers, key=marker_sort_key))
    project = Project(id=data["id"], name=data["name"], fps=fps, revision=data.get("revision", 0), revision_id=data["revision_id"], external_refs=_external_refs(data.get("external_refs")), assets=assets, timeline=timeline, schema_version=3)
    if "timeline_id" in data and data["timeline_id"] != timeline.id:
        raise ValidationError("Project timeline_id does not match timeline.id")
    if "tracks" in data and data["tracks"] != timeline_data.get("tracks"):
        raise ValidationError("Project tracks alias does not match timeline.tracks")
    project.validate()
    return project


def project_from_dict(data: dict[str, Any]) -> Project:
    if not isinstance(data, dict):
        raise ValidationError("Project document must be an object")
    schema_version = data.get("schema_version", 1)
    if schema_version == 3:
        return _project_from_v3(data)
    if schema_version == 2:
        from app.persistence.migrations import migrate_v2_to_v3
        return _project_from_v3(migrate_v2_to_v3(data))
    from app.persistence.migrations import migrate_document
    return _project_from_v3(migrate_document(data))
