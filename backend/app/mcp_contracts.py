from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class McpError(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    message: str
    current_revision: int | None = None


class McpResult(BaseModel):
    """Typed envelope retaining the existing JSON response keys as extensions."""
    model_config = ConfigDict(extra="allow")
    ok: bool | None = None
    error: McpError | None = None


class ProjectSummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    name: str
    revision: int
    revision_id: str
    timeline_id: str
    fps: dict[str, int] | None = None
    clip_count: int
    asset_count: int
    marker_count: int


class QueryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    revision: int
    revision_id: str
    query: dict[str, Any]
    clips: list[dict[str, Any]]
    markers: list[dict[str, Any]]
    result_count: int
