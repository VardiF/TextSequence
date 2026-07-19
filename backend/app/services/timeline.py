from app.domain.models import Project, marker_sort_key


def _gaps(clips, timeline_end):
    gaps = []
    cursor = 0
    for clip in sorted(clips, key=lambda item: (item.timeline_start_frame, item.id)):
        if clip.timeline_start_frame > cursor:
            gaps.append({"gap_ordinal": len(gaps) + 1, "start_frame": cursor, "end_frame": clip.timeline_start_frame,
                         "duration_frames": clip.timeline_start_frame - cursor})
        cursor = max(cursor, clip.timeline_start_frame + clip.duration_frames)
    if cursor < timeline_end:
        gaps.append({"gap_ordinal": len(gaps) + 1, "start_frame": cursor, "end_frame": timeline_end,
                     "duration_frames": timeline_end - cursor})
    return gaps


def timeline_projection(project: Project) -> dict:
    assets = {asset.id: asset for asset in project.assets}
    content_end_frame = max(
        [clip.timeline_start_frame + clip.duration_frames for track in project.timeline.tracks for clip in track.clips] + [0]
    )
    markers = sorted(project.timeline.markers, key=marker_sort_key)
    display_end_frame = max(
        [content_end_frame]
        + [marker.end_frame if marker.end_frame is not None else marker.start_frame + 1 for marker in markers]
    )
    tracks = []
    for track in project.timeline.tracks:
        clips = sorted(track.clips, key=lambda item: (item.timeline_start_frame, item.id))
        tracks.append({
            "id": track.id, "name": track.name, "kind": track.kind,
            "clips": [{"ordinal": index, "id": clip.id, "asset_id": clip.asset_id,
                       "asset_name": assets[clip.asset_id].name,
                       "source_in_frame": clip.source_in_frame, "source_out_frame": clip.source_out_frame,
                       "duration_frames": clip.duration_frames,
                       "timeline_start_frame": clip.timeline_start_frame,
                       "timeline_end_frame": clip.timeline_start_frame + clip.duration_frames}
                      for index, clip in enumerate(clips, 1)],
            "gaps": _gaps(clips, content_end_frame),
        })
    return {"project_id": project.id, "name": project.name, "revision": project.revision,
            "revision_id": project.revision_id, "timeline_id": project.timeline.id,
            "fps": {"numerator": project.fps.numerator, "denominator": project.fps.denominator} if project.fps else None,
            "content_end_frame": content_end_frame, "display_end_frame": display_end_frame,
            "markers": [{"id": marker.id, "start_frame": marker.start_frame, "end_frame": marker.end_frame,
                         "name": marker.name, "description": marker.description, "type": marker.type,
                         "production": {"shot_ids": list(marker.production.shot_ids),
                                        "dialogue_line_ids": list(marker.production.dialogue_line_ids),
                                        "external_refs": [{"system": ref.system, "id": ref.id, "kind": ref.kind}
                                                           for ref in marker.production.external_refs]}}
                        for marker in markers],
            "tracks": tracks}
