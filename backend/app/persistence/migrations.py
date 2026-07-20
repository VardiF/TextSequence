from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from app.domain.models import ValidationError

CURRENT_SCHEMA_VERSION = 3
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


def _v3_track_id(project_id: str) -> str:
    return f"track_{hashlib.sha256(f'textsequence:v3:track:{project_id}:V1'.encode()).hexdigest()[:32]}"


def migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Pure v2 -> v3 migration; source data is never rewritten."""
    source = deepcopy(data)
    _reject_unknown(source, {"schema_version", "id", "name", "fps", "revision", "revision_id", "external_refs", "assets", "timeline", "tracks", "timeline_id"}, "project")
    if source.get("schema_version") != 2:
        raise ValidationError("Invalid v2 schema_version")
    assets = []
    for index, asset in enumerate(source.get("assets", [])):
        _reject_unknown(asset, {"id", "path", "name", "codec", "width", "height", "fps", "duration_frames", "production"}, f"project.assets[{index}]")
        assets.append({**asset, "kind": "video", "production": asset.get("production") or _empty_asset_production()})
    timeline = source.get("timeline")
    if not isinstance(timeline, dict):
        raise ValidationError("Invalid project.timeline")
    _reject_unknown(timeline, {"id", "name", "external_refs", "tracks", "markers"}, "project.timeline")
    tracks = []
    for index, track in enumerate(timeline.get("tracks", [])):
        _reject_unknown(track, {"id", "name", "kind", "clips"}, f"project.timeline.tracks[{index}]")
        if track.get("kind", "video") != "video":
            raise ValidationError("Unsupported track kind")
        clips = []
        for clip_index, clip in enumerate(track.get("clips", [])):
            _reject_unknown(clip, {"id", "asset_id", "source_in_frame", "source_out_frame", "timeline_start_frame", "production"}, f"project.timeline.tracks[{index}].clips[{clip_index}]")
            clips.append({**clip, "kind": "video", "production": clip.get("production") or _empty_clip_production()})
        clips.sort(key=lambda value: (value["timeline_start_frame"], value["id"]))
        tracks.append({"id": track["id"], "name": track["name"], "kind": "video", "external_refs": [], "clips": clips})
    if not tracks:
        tracks = [{"id": _v3_track_id(source["id"]), "name": "V1", "kind": "video", "external_refs": [], "clips": []}]
    canvas = None
    if assets or any(track["clips"] for track in tracks):
        if not assets:
            raise ValidationError("Cannot derive video canvas without assets")
        canvas = {"width": assets[0]["width"], "height": assets[0]["height"]}
    return {
        "schema_version": 3,
        "id": source["id"],
        "name": source["name"],
        "fps": deepcopy(source.get("fps")),
        "revision": source.get("revision", 0),
        "revision_id": source["revision_id"],
        "external_refs": deepcopy(source.get("external_refs") or []),
        "assets": assets,
        "timeline": {
            "id": timeline["id"], "name": timeline.get("name", "Main timeline"),
            "external_refs": deepcopy(timeline.get("external_refs") or []),
            "video_canvas": canvas, "tracks": tracks,
            "markers": deepcopy(timeline.get("markers") or []),
        },
    }


def migrate_document(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValidationError("Project document must be an object")
    schema_version = data.get("schema_version", 1)
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ValidationError("Invalid schema_version")
    if schema_version == 1:
        return migrate_v2_to_v3(migrate_v1_to_v2(data))
    if schema_version == 2:
        return migrate_v2_to_v3(data)
    if schema_version > CURRENT_SCHEMA_VERSION:
        raise ValidationError(f"Unsupported future schema_version: {schema_version}")
    if schema_version == CURRENT_SCHEMA_VERSION:
        return deepcopy(data)
    raise ValidationError(f"Unsupported schema_version: {schema_version}")


MIGRATIONS = {1: migrate_v1_to_v2, 2: migrate_v2_to_v3}
