"""Strict contracts for forward-only revision restore."""
from __future__ import annotations

from typing import Any, Literal, Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from app.revision_diff_models import ProjectStateDiff


REVISION_ID_PATTERN = r"revision_[A-Za-z0-9_-]{1,127}"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RestoreRevisionRequest(_StrictModel):
    expected_revision: Annotated[StrictInt, Field(ge=0)]
    expected_revision_id: Annotated[StrictStr, Field(pattern=REVISION_ID_PATTERN)]
    guard_tokens: list[StrictStr] = Field(default_factory=list)


class RestoreRevisionResult(_StrictModel):
    ok: Literal[True] = True
    status: Literal["restored"]
    project_id: StrictStr
    revision: StrictInt
    revision_id: StrictStr
    parent_revision_id: StrictStr
    restored_from_revision_id: StrictStr
    diff: ProjectStateDiff
    timeline: dict[str, Any]


class RestoreErrorDetail(_StrictModel):
    code: StrictStr
    message: StrictStr
    current_revision: StrictInt | None = None
    current_revision_id: StrictStr | None = None
    conflicts: list[dict[str, Any]] = Field(default_factory=list)


class RestoreErrorOutput(_StrictModel):
    ok: Literal[False]
    error: RestoreErrorDetail
