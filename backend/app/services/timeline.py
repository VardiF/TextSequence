from app.domain.models import Project, marker_sort_key
from app.services.projections import clip_projection, marker_projection


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
            "external_refs": [{"system": ref.system, "id": ref.id, "kind": ref.kind} for ref in track.external_refs],
            "clips": [{**clip_projection(clip, track, assets), "ordinal": index}
                      for index, clip in enumerate(clips, 1)],
            "gaps": _gaps(clips, content_end_frame),
        })
    return {"project_id": project.id, "name": project.name, "revision": project.revision,
            "revision_id": project.revision_id, "timeline_id": project.timeline.id,
            "fps": {"numerator": project.fps.numerator, "denominator": project.fps.denominator} if project.fps else None,
            "schema_version": project.schema_version,
            "video_canvas": {"width": project.timeline.video_canvas.width, "height": project.timeline.video_canvas.height} if project.timeline.video_canvas else None,
            "content_end_frame": content_end_frame, "display_end_frame": display_end_frame,
            "markers": [marker_projection(marker) for marker in markers],
            "tracks": tracks}
