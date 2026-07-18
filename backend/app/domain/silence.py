from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.domain.models import Clip, Project, Track, ValidationError


@dataclass(frozen=True)
class SourceRemovalRange:
    asset_id: str
    start_frame: int
    end_frame: int

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame


def _merge(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((start, end) for start, end in ranges if end > start)
    merged: list[list[int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]: merged[-1][1] = max(merged[-1][1], end)
        else: merged.append([start, end])
    return [(start, end) for start, end in merged]


def apply_silence_removals(project: Project, removals: Iterable[SourceRemovalRange]) -> tuple[Project, int, int, list[dict]]:
    by_asset: dict[str, list[tuple[int, int]]] = {}
    for removal in removals:
        if removal.start_frame < 0 or removal.end_frame <= removal.start_frame:
            raise ValidationError("Silence removal range must be non-empty and non-negative")
        by_asset.setdefault(removal.asset_id, []).append((removal.start_frame, removal.end_frame))
    for asset_id in by_asset:
        by_asset[asset_id] = _merge(by_asset[asset_id])

    from copy import deepcopy
    edited = deepcopy(project)
    total_removed = 0
    applied: list[dict] = []
    for track in edited.tracks:
        ordered = sorted(track.clips, key=lambda item: (item.timeline_start_frame, item.id))
        rebuilt: list[Clip] = []
        cumulative_removed = 0
        for clip in ordered:
            clip_removals = [(max(clip.source_in_frame, start), min(clip.source_out_frame, end))
                             for start, end in by_asset.get(clip.asset_id, [])
                             if start < clip.source_out_frame and end > clip.source_in_frame]
            clip_removals = _merge(clip_removals)
            retained: list[tuple[int, int]] = []
            cursor = clip.source_in_frame
            for start, end in clip_removals:
                if cursor < start: retained.append((cursor, start))
                cursor = max(cursor, end)
            if cursor < clip.source_out_frame: retained.append((cursor, clip.source_out_frame))
            removed_here = sum(end - start for start, end in clip_removals)
            total_removed += removed_here
            if removed_here:
                applied.extend({"clip_id": clip.id, "asset_id": clip.asset_id,
                                "source_in_frame": start, "source_out_frame": end,
                                "timeline_start_frame": clip.timeline_start_frame + (start - clip.source_in_frame) - cumulative_removed,
                                "duration_frames": end - start}
                               for start, end in clip_removals)
            new_start = clip.timeline_start_frame - cumulative_removed
            for index, (source_start, source_end) in enumerate(retained):
                duration = source_end - source_start
                rebuilt.append(Clip(id=clip.id if index == 0 else f"{clip.id}_silence_{index}",
                                    asset_id=clip.asset_id, source_in_frame=source_start,
                                    source_out_frame=source_end,
                                    timeline_start_frame=new_start,
                                    production=deepcopy(clip.production)))
                new_start += duration
            cumulative_removed += removed_here
        track.clips = rebuilt
    edited.validate()
    return edited, total_removed, len(applied), applied
