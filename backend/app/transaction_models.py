"""Strict public contracts for stateless v0.3 transactions."""
from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_serializer, model_validator

from app.revision_diff_models import RevisionChanges, RevisionDiffSummary


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IdReference(_StrictModel):
    kind: Literal["id"]
    id: StrictStr


class ResultReference(_StrictModel):
    kind: Literal["result"]
    ref: Annotated[StrictStr, Field(pattern=r"[A-Za-z][A-Za-z0-9_-]{0,63}")]


EntityReference = Annotated[Union[IdReference, ResultReference], Field(discriminator="kind")]


class MarkerExternalReference(_StrictModel):
    system: StrictStr
    id: StrictStr
    kind: StrictStr = ""


class MarkerProduction(_StrictModel):
    shot_ids: list[StrictStr] = Field(default_factory=list)
    dialogue_line_ids: list[StrictStr] = Field(default_factory=list)
    external_refs: list[MarkerExternalReference] = Field(default_factory=list)


class MarkerChanges(_StrictModel):
    start_frame: StrictInt | None = None
    end_frame: StrictInt | None = None
    name: StrictStr | None = None
    description: StrictStr | None = None
    type: StrictStr | None = None
    production: MarkerProduction | None = None

    @model_validator(mode="after")
    def has_change(self) -> "MarkerChanges":
        if not self.model_fields_set:
            raise ValueError("Marker changes cannot be empty")
        for field_name in ("start_frame", "name", "description", "type", "production"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"Marker change {field_name} cannot be null")
        return self

    @model_serializer(mode="plain")
    def serialize_set_fields(self) -> dict[str, Any]:
        # Prepared transactions must preserve omitted-vs-null semantics for
        # partial marker updates across the prepare/commit wire boundary.
        return {name: getattr(self, name) for name in self.model_fields_set}


class SplitOperation(_StrictModel):
    op: Literal["split_clip"]
    clip: EntityReference
    timeline_frame: StrictInt
    result_ref: Annotated[StrictStr, Field(pattern=r"[A-Za-z][A-Za-z0-9_-]{0,63}")]


class MoveOperation(_StrictModel):
    op: Literal["move_clip"]
    clip: EntityReference
    timeline_start_frame: StrictInt


class TrimOperation(_StrictModel):
    op: Literal["trim_clip"]
    clip: EntityReference
    source_in_frame: StrictInt | None = None
    source_out_frame: StrictInt | None = None

    @model_validator(mode="after")
    def has_edge(self) -> "TrimOperation":
        if self.source_in_frame is None and self.source_out_frame is None:
            raise ValueError("Trim requires source_in_frame or source_out_frame")
        return self


class DeleteClipOperation(_StrictModel):
    op: Literal["delete_clip"]
    clip: EntityReference


class AddMarkerOperation(_StrictModel):
    op: Literal["add_marker"]
    result_ref: Annotated[StrictStr, Field(pattern=r"[A-Za-z][A-Za-z0-9_-]{0,63}")]
    start_frame: StrictInt
    end_frame: StrictInt | None = None
    name: StrictStr
    description: StrictStr = ""
    type: StrictStr = "generic"
    production: MarkerProduction = Field(default_factory=MarkerProduction)


class UpdateMarkerOperation(_StrictModel):
    op: Literal["update_marker"]
    marker: EntityReference
    changes: MarkerChanges


class DeleteMarkerOperation(_StrictModel):
    op: Literal["delete_marker"]
    marker: EntityReference


RawOperation = Annotated[Union[
    SplitOperation, MoveOperation, TrimOperation, DeleteClipOperation,
    AddMarkerOperation, UpdateMarkerOperation, DeleteMarkerOperation,
], Field(discriminator="op")]


class PrepareTransactionRequest(_StrictModel):
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    operations: Annotated[list[RawOperation], Field(min_length=1, max_length=100)]


class ResolvedSplitOperation(_StrictModel):
    op: Literal["split_clip"]
    clip_id: StrictStr
    timeline_frame: StrictInt
    new_clip_id: StrictStr
    result_ref: StrictStr


class ResolvedMoveOperation(_StrictModel):
    op: Literal["move_clip"]
    clip_id: StrictStr
    timeline_start_frame: StrictInt


class ResolvedTrimOperation(_StrictModel):
    op: Literal["trim_clip"]
    clip_id: StrictStr
    source_in_frame: StrictInt | None = None
    source_out_frame: StrictInt | None = None


class ResolvedDeleteClipOperation(_StrictModel):
    op: Literal["delete_clip"]
    clip_id: StrictStr


class ResolvedAddMarkerOperation(_StrictModel):
    op: Literal["add_marker"]
    result_ref: StrictStr
    marker_id: StrictStr
    start_frame: StrictInt
    end_frame: StrictInt | None = None
    name: StrictStr
    description: StrictStr
    type: StrictStr
    production: MarkerProduction


class ResolvedUpdateMarkerOperation(_StrictModel):
    op: Literal["update_marker"]
    marker_id: StrictStr
    changes: MarkerChanges


class ResolvedDeleteMarkerOperation(_StrictModel):
    op: Literal["delete_marker"]
    marker_id: StrictStr


PreparedOperation = Annotated[Union[
    ResolvedSplitOperation, ResolvedMoveOperation, ResolvedTrimOperation,
    ResolvedDeleteClipOperation, ResolvedAddMarkerOperation,
    ResolvedUpdateMarkerOperation, ResolvedDeleteMarkerOperation,
], Field(discriminator="op")]


class PreparedTransaction(_StrictModel):
    contract_version: Literal[1]
    project_id: StrictStr
    base_revision: Annotated[StrictInt, Field(ge=0)]
    base_revision_id: StrictStr
    operations: Annotated[list[PreparedOperation], Field(min_length=1, max_length=100)]


class CommitTransactionRequest(_StrictModel):
    transaction_hash: Annotated[StrictStr, Field(pattern=r"[0-9a-f]{64}")]
    prepared_transaction: PreparedTransaction


class ProjectStateDiff(_StrictModel):
    summary: RevisionDiffSummary
    changes: RevisionChanges


class OperationResult(_StrictModel):
    operation_index: Annotated[StrictInt, Field(ge=0)]
    op: StrictStr
    affected_ids: list[StrictStr]
    created_ids: list[StrictStr] = Field(default_factory=list)
    result_ref: StrictStr | None = None


class TransactionErrorDetail(_StrictModel):
    code: StrictStr
    message: StrictStr
    operation_index: StrictInt | None = None
    operation: StrictStr | None = None
    cause_code: StrictStr | None = None
    current_revision: StrictInt | None = None
    current_revision_id: StrictStr | None = None


class TransactionErrorOutput(_StrictModel):
    ok: Literal[False]
    error: TransactionErrorDetail


class PrepareTransactionOutput(_StrictModel):
    ok: Literal[True] = True
    status: Literal["prepared"]
    commit_requires_unchanged_base: Literal[True] = True
    transaction_hash: StrictStr
    prepared_transaction: PreparedTransaction
    operation_results: list[OperationResult]
    diff: ProjectStateDiff


class CommitTransactionOutput(_StrictModel):
    ok: Literal[True] = True
    status: Literal["committed"]
    project_id: StrictStr
    revision: StrictInt
    revision_id: StrictStr
    parent_revision_id: StrictStr
    transaction_hash: StrictStr
    operation_results: list[OperationResult]
    diff: ProjectStateDiff
    timeline: dict[str, Any]
