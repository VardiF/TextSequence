"""Strict public contracts for deterministic revision comparisons."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SafeFrameRate(_StrictModel):
    numerator: int
    denominator: int


class SafeExternalReference(_StrictModel):
    system: str
    id: str
    kind: str


class SafeProduction(_StrictModel):
    shot_ids: list[str]
    dialogue_line_ids: list[str]
    external_refs: list[SafeExternalReference]


class SafeAssetProduction(SafeProduction):
    generation_job_id: str | None


class SafeAsset(_StrictModel):
    id: str
    name: str
    codec: str
    width: int
    height: int
    fps: SafeFrameRate
    duration_frames: int
    production: SafeAssetProduction


class SafeTrack(_StrictModel):
    id: str
    name: str
    kind: str
    position: int


class SafeClip(_StrictModel):
    id: str
    track_id: str
    asset_id: str
    source_in_frame: int
    source_out_frame: int
    timeline_start_frame: int
    production: SafeProduction


class SafeMarker(_StrictModel):
    id: str
    start_frame: int
    end_frame: int | None
    name: str
    description: str
    type: str
    production: SafeProduction


class ValueFieldChange(_StrictModel):
    path: str
    kind: Literal["value"]
    before: Any
    after: Any


class RedactedFieldChange(_StrictModel):
    path: Literal["/source_location"]
    kind: Literal["redacted"]
    values_redacted: Literal[True]


FieldChange = Annotated[Union[ValueFieldChange, RedactedFieldChange], Field(discriminator="kind")]


class FieldChanges(_StrictModel):
    fields: list[FieldChange]


class EntityModification(_StrictModel):
    id: str
    fields: list[FieldChange]


class EntityChanges(_StrictModel):
    added: list[Any]
    removed: list[Any]
    modified: list[EntityModification]


class ProjectChanges(_StrictModel):
    fields: list[FieldChange]


class TimelineChanges(_StrictModel):
    fields: list[FieldChange]


class AssetChanges(_StrictModel):
    added: list[SafeAsset]
    removed: list[SafeAsset]
    modified: list[EntityModification]


class TrackChanges(_StrictModel):
    added: list[SafeTrack]
    removed: list[SafeTrack]
    modified: list[EntityModification]


class ClipChanges(_StrictModel):
    added: list[SafeClip]
    removed: list[SafeClip]
    modified: list[EntityModification]


class MarkerChanges(_StrictModel):
    added: list[SafeMarker]
    removed: list[SafeMarker]
    modified: list[EntityModification]


class RevisionChanges(_StrictModel):
    project: ProjectChanges
    timeline: TimelineChanges
    assets: AssetChanges
    tracks: TrackChanges
    clips: ClipChanges
    markers: MarkerChanges


class EntityTypeSummary(_StrictModel):
    added: int
    removed: int
    modified: int


class RevisionDiffByEntityType(_StrictModel):
    assets: EntityTypeSummary
    tracks: EntityTypeSummary
    clips: EntityTypeSummary
    markers: EntityTypeSummary


class RevisionDiffSummary(_StrictModel):
    entities_added: int
    entities_removed: int
    entities_modified: int
    fields_modified: int
    project_fields_modified: int
    timeline_fields_modified: int
    by_entity_type: RevisionDiffByEntityType


class RevisionDiffMetadata(_StrictModel):
    project_id: str
    revision_id: str
    revision: int
    parent_revision_id: str | None
    created_at: str
    origin: str
    actor: dict[str, str]
    operation: str
    summary: str
    restored_from_revision_id: str | None
    is_head: bool


class RevisionDiffResult(_StrictModel):
    ok: Literal[True] = True
    project_id: str
    timeline_id: str
    direction: Literal["forward", "reverse", "same"]
    from_revision: RevisionDiffMetadata
    to_revision: RevisionDiffMetadata
    summary: RevisionDiffSummary
    changes: RevisionChanges


class RevisionDiffErrorDetail(_StrictModel):
    code: str
    message: str
    current_revision: int | None = None


class RevisionDiffErrorOutput(_StrictModel):
    ok: Literal[False]
    error: RevisionDiffErrorDetail
