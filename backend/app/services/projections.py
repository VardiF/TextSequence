"""Safe, transport-neutral projections for read APIs and MCP resources."""
from __future__ import annotations

from app.domain.models import Asset, Clip, Marker, Project
from app.persistence.revisions import RevisionMetadata, RevisionRecord


def _reference(ref) -> dict:
    return {"system": ref.system, "id": ref.id, "kind": ref.kind}


def _production(value) -> dict:
    return {
        "shot_ids": list(value.shot_ids),
        "dialogue_line_ids": list(value.dialogue_line_ids),
        "external_refs": [_reference(ref) for ref in value.external_refs],
    }


def asset_projection(asset: Asset) -> dict:
    return {
        "id": asset.id, "name": asset.name,
        "duration_frames": asset.duration_frames,
        "fps": {"numerator": asset.fps.numerator, "denominator": asset.fps.denominator} if asset.fps else None,
        "width": asset.width, "height": asset.height, "codec": asset.codec,
        "production": _production(asset.production),
    }


def clip_projection(clip: Clip, track, assets: dict[str, Asset]) -> dict:
    asset = assets[clip.asset_id]
    return {
        "id": clip.id, "track_id": track.id, "track_name": track.name, "track_kind": track.kind,
        "asset_id": clip.asset_id, "asset_name": asset.name,
        "source_in_frame": clip.source_in_frame, "source_out_frame": clip.source_out_frame,
        "duration_frames": clip.duration_frames, "timeline_start_frame": clip.timeline_start_frame,
        "timeline_end_frame": clip.timeline_start_frame + clip.duration_frames,
        "production": _production(clip.production),
    }


def marker_projection(marker: Marker) -> dict:
    return {
        "id": marker.id, "start_frame": marker.start_frame, "end_frame": marker.end_frame,
        "name": marker.name, "description": marker.description, "type": marker.type,
        "production": _production(marker.production),
    }


def project_summary_projection(project: Project) -> dict:
    return {
        "project_id": project.id, "name": project.name, "revision": project.revision,
        "revision_id": project.revision_id, "timeline_id": project.timeline.id,
        "fps": {"numerator": project.fps.numerator, "denominator": project.fps.denominator} if project.fps else None,
        "clip_count": sum(len(track.clips) for track in project.timeline.tracks),
        "asset_count": len(project.assets), "marker_count": len(project.timeline.markers),
    }


def project_projection(project: Project) -> dict:
    return {
        **project_summary_projection(project),
        "schema_version": project.schema_version,
        "assets": [asset_projection(asset) for asset in sorted(project.assets, key=lambda item: item.id)],
    }


def revision_metadata_projection(metadata: RevisionMetadata, *, is_head: bool = False) -> dict:
    return {
        "project_id": metadata.project_id, "revision_id": metadata.revision_id,
        "revision": metadata.revision_number, "parent_revision_id": metadata.parent_revision_id,
        "created_at": metadata.created_at, "origin": metadata.origin, "actor": dict(metadata.actor),
        "operation": metadata.operation, "summary": metadata.summary,
        "restored_from_revision_id": metadata.restored_from_revision_id, "is_head": is_head,
    }


def revision_projection(record: RevisionRecord, *, is_head: bool = False) -> dict:
    from app.domain.models import project_from_dict
    project = project_from_dict(record.snapshot)
    from app.services.timeline import timeline_projection
    return {
        "metadata": revision_metadata_projection(record.metadata, is_head=is_head),
        "project": project_projection(project),
        "timeline": timeline_projection(project),
    }
