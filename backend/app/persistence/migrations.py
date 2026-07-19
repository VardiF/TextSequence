from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.domain.models import ValidationError

CURRENT_SCHEMA_VERSION = 2
_V1_PROJECT_FIELDS = {"schema_version", "id", "name", "fps", "revision", "assets", "tracks"}
_V1_ASSET_FIELDS = {"id", "path", "name", "codec", "width", "height", "fps", "duration_frames"}
_V1_TRACK_FIELDS = {"id", "name", "kind", "clips"}
_V1_CLIP_FIELDS = {"id", "asset_id", "source_in_frame", "source_out_frame", "timeline_start_frame"}


def _reject_unknown(data: dict[str, Any], allowed: set[str], path: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValidationError(f"Unknown field at {path}.{unknown[0]}")


def _empty_asset_production() -> dict[str, Any]:
    return {"shot_ids": [], "dialogue_line_ids": [], "generation_job_id": None, "external_refs": []}


def _empty_clip_production() -> dict[str, Any]:
    return {"shot_ids": [], "dialogue_line_ids": [], "external_refs": []}


def _timeline_id(project_id: str) -> str:
    return f"timeline_{uuid5(NAMESPACE_URL, f'textsequence:timeline:{project_id}').hex}"


def _baseline_revision_id(project_id: str, revision: int, snapshot_without_revision_id: dict[str, Any]) -> str:
    payload = json.dumps(snapshot_without_revision_id, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(f"migration:{project_id}:{revision}:".encode() + payload).hexdigest()
    return f"revision_migration_{digest[:32]}"


def migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Pure, deterministic v1 -> v2 migration. It never writes the source file."""
    source = deepcopy(data)
    _reject_unknown(source, _V1_PROJECT_FIELDS, "project")
    if "schema_version" in source and source["schema_version"] != 1:
        raise ValidationError("Invalid v1 schema_version")
    if not isinstance(source.get("id"), str) or not source["id"]:
        raise ValidationError("Invalid field at project.id")
    assets = []
    for index, asset in enumerate(source.get("assets", [])):
        _reject_unknown(asset, _V1_ASSET_FIELDS, f"project.assets[{index}]")
        assets.append({**asset, "production": _empty_asset_production()})
    tracks = []
    for track_index, track in enumerate(source.get("tracks", [])):
        _reject_unknown(track, _V1_TRACK_FIELDS, f"project.tracks[{track_index}]")
        clips = []
        for clip_index, clip in enumerate(track.get("clips", [])):
            _reject_unknown(clip, _V1_CLIP_FIELDS, f"project.tracks[{track_index}].clips[{clip_index}]")
            clips.append({**clip, "production": _empty_clip_production()})
        tracks.append({"id": track["id"], "name": track["name"], "kind": track.get("kind", "video"), "clips": clips})
    revision = source.get("revision", 0)
    migrated: dict[str, Any] = {
        "schema_version": 2,
        "id": source["id"],
        "name": source["name"],
        "fps": deepcopy(source.get("fps")),
        "revision": revision,
        "revision_id": "",
        "external_refs": [],
        "assets": assets,
        "timeline": {"id": _timeline_id(source["id"]), "name": "Main timeline", "external_refs": [], "tracks": tracks, "markers": []},
    }
    migrated["revision_id"] = _baseline_revision_id(source["id"], revision, migrated)
    return migrated


def migrate_document(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValidationError("Project document must be an object")
    schema_version = data.get("schema_version", 1)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValidationError("Invalid schema_version")
    if schema_version == 1:
        return migrate_v1_to_v2(data)
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise ValidationError(f"Unsupported future schema_version: {schema_version}")
    if schema_version == CURRENT_SCHEMA_VERSION:
        return deepcopy(data)
    raise ValidationError(f"Unsupported schema_version: {schema_version}")


MIGRATIONS = {1: migrate_v1_to_v2}
