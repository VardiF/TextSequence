from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


class ValidationError(ValueError):
    pass


class TimelineConflictError(ValidationError):
    pass


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
class Asset:
    id: str
    path: str
    name: str
    codec: str
    width: int
    height: int
    fps: FrameRate
    duration_frames: int


@dataclass
class Clip:
    id: str
    asset_id: str
    source_in_frame: int
    source_out_frame: int
    timeline_start_frame: int

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
class Project:
    id: str
    name: str
    fps: Optional[FrameRate] = None
    revision: int = 0
    assets: list[Asset] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)

    def validate(self) -> None:
        ids: list[str] = [self.id]
        ids.extend(a.id for a in self.assets)
        ids.extend(t.id for t in self.tracks)
        ids.extend(c.id for t in self.tracks for c in t.clips)
        if any(not item for item in ids) or len(ids) != len(set(ids)):
            raise ValidationError("All project entities need unique opaque IDs")
        if self.revision < 0:
            raise ValidationError("Revision cannot be negative")
        if self.fps is None and self.assets:
            raise ValidationError("A project with assets must have an FPS")
        if self.fps:
            for asset in self.assets:
                if asset.fps.as_tuple() != self.fps.as_tuple():
                    raise ValidationError("Asset FPS must match project FPS")
        asset_ids = {a.id for a in self.assets}
        for track in self.tracks:
            ordered_clips = sorted(track.clips, key=lambda clip: clip.timeline_start_frame)
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
                asset = next(a for a in self.assets if a.id == clip.asset_id)
                if clip.source_out_frame > asset.duration_frames:
                    raise ValidationError("Clip source range exceeds asset duration")


def project_to_dict(project: Project) -> dict[str, Any]:
    return asdict(project)


def project_from_dict(data: dict[str, Any]) -> Project:
    fps_data = data.get("fps")
    fps = FrameRate(**fps_data) if fps_data else None
    assets = [Asset(**{**item, "fps": FrameRate(**item["fps"])}) for item in data.get("assets", [])]
    tracks = [Track(**{**item, "clips": [Clip(**clip) for clip in item.get("clips", [])]}) for item in data.get("tracks", [])]
    project = Project(id=data["id"], name=data["name"], fps=fps, revision=data.get("revision", 0), assets=assets, tracks=tracks)
    project.validate()
    return project
