from app.domain.models import Project


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
            "gaps": _gaps(clips, max([assets[a.id].duration_frames for a in project.assets] + [c.timeline_start_frame + c.duration_frames for c in clips] + [0])),
        })
    return {"project_id": project.id, "name": project.name, "revision": project.revision,
            "revision_id": project.revision_id, "timeline_id": project.timeline.id,
            "fps": {"numerator": project.fps.numerator, "denominator": project.fps.denominator} if project.fps else None,
            "tracks": tracks}
