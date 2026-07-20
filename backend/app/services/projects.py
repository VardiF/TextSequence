from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path
import re
from threading import Lock, RLock
from typing import Optional
from uuid import uuid4

from app.domain.models import ExternalReference, Marker, ValidationError, marker_production_from_dict, project_from_dict, validate_revision_id
from app.domain.silence import SourceRemovalRange, apply_silence_removals
from app.audio.silence import AssetSilenceAnalysis, SilenceAnalysisError, analyze_asset, milliseconds_to_frames, silence_dict, validate_parameters
from app.domain.operations import (add_marker, add_track, delete_clip, delete_marker, delete_track, move_clip,
                                   new_marker_id, new_project, register_asset, reorder_track, split_clip, trim_clip,
                                   update_marker, update_track)
from app.domain.models import project_to_dict
from app.media.probe import probe_media
from app.persistence.project_store import ProjectStore, RevisionNotFoundError, StaleRevisionError
from app.rendering.ffmpeg import RenderResult, render_plan
from app.rendering.plan import compile_render_plan
from app.services.timeline import timeline_projection
from app.services.query import query_timeline
from app.services.projections import project_summary_projection
from app.services.revision_diff import (RevisionDiffError, RevisionDiffIntegrityError,
                                        RevisionHistoryUnavailableError, diff_projects, summarize_changes)
from app.revision_diff_models import RevisionDiffMetadata, RevisionDiffResult
from app.services.projections import revision_metadata_projection
from app.services.transactions import TransactionService
from app.services.restore import RestoreService
from app.services.guards import GuardService
from app.guard_models import MutationFootprint


class ProjectService:
    def __init__(self, store: Optional[ProjectStore] = None, runtime_root: Optional[Path] = None,
                 media_root: Optional[Path] = None) -> None:
        self.store = store or ProjectStore()
        self.runtime_root = runtime_root or Path("runtime")
        self.media_root = media_root or Path("media")
        self._locks: dict[str, RLock] = {}
        self._locks_guard = Lock()
        self.guards = GuardService(self)
        self.transactions = TransactionService(self)
        self.restore = RestoreService(self)

    def _project_lock(self, project_id: str) -> RLock:
        self.store.path_for(project_id)
        with self._locks_guard:
            return self._locks.setdefault(project_id, RLock())

    def create(self, name: str):
        project = new_project(name)
        return self.store.create_initial(project, operation="create_project", origin="system", actor={"type": "system"})

    def get(self, project_id: str): return self.store.load(project_id)
    def list(self): return self.store.list()
    def list_summaries(self):
        return [project_summary_projection(p)
                for p in sorted(self.store.list(), key=lambda item: (item.name, item.id))]
    def timeline(self, project_id: str): return timeline_projection(self.store.load(project_id))
    def query_timeline(self, project_id: str, query: dict): return query_timeline(self.store.load(project_id), query)

    def prepare_transaction(self, project_id: str, request):
        return self.transactions.prepare(project_id, request)

    def commit_transaction(self, project_id: str, request, *, origin="rest", actor=None):
        return self.transactions.commit(project_id, request, origin=origin, actor=actor or {"type": "human"})

    def restore_revision(self, project_id: str, target_revision_id: str, request, *, origin="rest", actor=None):
        return self.restore.restore(project_id, target_revision_id, request, origin=origin,
                                    actor=actor or {"type": "human"})

    def revision_records(self, project_id: str):
        return self.store.reachable_revisions(project_id)

    def revision_record(self, project_id: str, revision_id: str):
        return self.store.reachable_revision(project_id, revision_id)

    def diff_revisions(self, project_id: str, from_revision_id: str, to_revision_id: str) -> RevisionDiffResult:
        try:
            self.store.path_for(project_id)
            validate_revision_id(from_revision_id)
            validate_revision_id(to_revision_id)
        except ValidationError as exc:
            raise RevisionDiffError("INVALID_ARGUMENT", "Invalid project or revision identifier") from exc

        try:
            history_available, records = self.store.reachable_revisions(project_id)
        except FileNotFoundError:
            raise
        except ValidationError as exc:
            raise RevisionDiffError("INTEGRITY_ERROR", "Revision history integrity validation failed") from exc
        if not history_available:
            raise RevisionHistoryUnavailableError()

        by_id = {record.metadata.revision_id: (index, record) for index, record in enumerate(records)}
        if from_revision_id not in by_id or to_revision_id not in by_id:
            raise RevisionNotFoundError("Revision does not exist")
        from_index, from_record = by_id[from_revision_id]
        to_index, to_record = by_id[to_revision_id]
        try:
            if from_record.metadata.project_id != project_id or to_record.metadata.project_id != project_id:
                raise RevisionDiffIntegrityError("Revision belongs to another project")
            before = project_from_dict(from_record.snapshot)
            after = project_from_dict(to_record.snapshot)
            if before.id != project_id or after.id != project_id:
                raise RevisionDiffIntegrityError("Revision snapshot belongs to another project")
            if before.timeline.id != after.timeline.id:
                raise RevisionDiffIntegrityError("Compared timelines have different identities")
            changes = diff_projects(before, after)
            summary = summarize_changes(changes)
        except (ValidationError, RevisionDiffIntegrityError) as exc:
            raise RevisionDiffError("INTEGRITY_ERROR", "Revision history integrity validation failed") from exc

        if from_index == to_index:
            direction = "same"
        elif from_index > to_index:
            direction = "forward"
        else:
            direction = "reverse"
        return RevisionDiffResult(
            project_id=project_id,
            timeline_id=before.timeline.id,
            direction=direction,
            from_revision=RevisionDiffMetadata.model_validate(
                revision_metadata_projection(from_record.metadata, is_head=from_index == 0)
            ),
            to_revision=RevisionDiffMetadata.model_validate(
                revision_metadata_projection(to_record.metadata, is_head=to_index == 0)
            ),
            summary=summary,
            changes=changes,
        )

    @staticmethod
    def _analysis_output(project_id: str, revision: int, analyses: list[AssetSilenceAnalysis], minimum_silence_ms: int, noise_threshold_db: float) -> dict:
        silences = []
        for analysis in analyses:
            for silence in analysis.silences:
                silences.append({"asset_id": analysis.asset_id, "start_frame": silence.start_frame,
                                 "end_frame": silence.end_frame, "duration_frames": silence.duration_frames})
        return {"project_id": project_id, "revision": revision,
                "minimum_silence_ms": minimum_silence_ms, "noise_threshold_db": noise_threshold_db,
                "silences": silences, "summary": {"detected_silences": len(silences),
                "total_silence_frames": sum(item["duration_frames"] for item in silences)}}

    def _analyze_project_silence(self, project, minimum_silence_ms: int, noise_threshold_db: float) -> list[AssetSilenceAnalysis]:
        validate_parameters(minimum_silence_ms, noise_threshold_db)
        if not project.assets:
            raise SilenceAnalysisError("NO_MEDIA", "Project has no imported media")
        return [analyze_asset(asset, minimum_silence_ms, noise_threshold_db) for asset in project.assets]

    def analyze_silence(self, project_id: str, minimum_silence_ms: int = 700, noise_threshold_db: float = -35) -> dict:
        project = self.store.load(project_id)
        analyses = self._analyze_project_silence(project, minimum_silence_ms, noise_threshold_db)
        return self._analysis_output(project.id, project.revision, analyses, minimum_silence_ms, noise_threshold_db)

    def remove_silence(self, project_id: str, expected_revision: int, minimum_silence_ms: int = 700,
                       noise_threshold_db: float = -35, keep_padding_ms: int = 0,
                       origin: str = "rest", actor: Optional[dict[str, str]] = None,
                       guard_tokens: Optional[list[str]] = None) -> dict:
        validate_parameters(minimum_silence_ms, noise_threshold_db, keep_padding_ms)
        initial = self.store.load(project_id)
        if sum(bool(track.clips) for track in initial.timeline.tracks) > 1:
            raise SilenceAnalysisError("MULTI_TRACK_SILENCE_UNSUPPORTED", "Silence removal is unavailable when clips occupy multiple video tracks")
        analyses = self._analyze_project_silence(initial, minimum_silence_ms, noise_threshold_db)
        padding_frames = milliseconds_to_frames(keep_padding_ms, initial.fps.as_tuple()) if initial.fps else 0
        removals: list[SourceRemovalRange] = []
        detected = []
        for analysis in analyses:
            for silence in analysis.silences:
                detected.append({"asset_id": analysis.asset_id, "start_frame": silence.start_frame,
                                 "end_frame": silence.end_frame, "duration_frames": silence.duration_frames})
                start, end = silence.start_frame + padding_frames, silence.end_frame - padding_frames
                if end > start:
                    removals.append(SourceRemovalRange(analysis.asset_id, start, end))
        previous_revision = initial.revision
        _, removed_frames, removed_count, applied = apply_silence_removals(initial, removals)
        project_result = self._commit_operation(project_id, expected_revision,
            lambda current: apply_silence_removals(current, removals)[0], origin, actor,
            "remove_silence", "Remove detected silence", guard_tokens=guard_tokens)
        new_revision = project_result.revision
        resulting_clip_count = sum(len(track.clips) for track in project_result.timeline.tracks)
        fps = initial.fps.as_tuple() if initial.fps else (1, 1)
        removed_duration_ms = round(removed_frames * 1000 * fps[1] / fps[0])
        return {"ok": True, "project_id": project_id, "previous_revision": previous_revision,
                "revision": new_revision, "minimum_silence_ms": minimum_silence_ms,
                "noise_threshold_db": noise_threshold_db, "keep_padding_ms": keep_padding_ms,
                "detected_silences": len(detected), "removed_silences": removed_count,
                "removed_frames": removed_frames, "removed_duration_ms": removed_duration_ms,
                "resulting_clip_count": resulting_clip_count, "detected_ranges": detected,
                "removed_ranges": applied, "project": project_result}

    def media_path(self, project_id: str, asset_id: str) -> Path:
        project = self.store.load(project_id)
        for asset in project.assets:
            if asset.id == asset_id: return Path(asset.path)
        raise FileNotFoundError(f"Asset does not exist: {asset_id}")

    def import_media(self, project_id: str, path: str, target_track_id: str | None = None,
                     timeline_start_frame: int | None = None, origin: str = "rest", actor: Optional[dict[str, str]] = None,
                     guard_tokens: Optional[list[str]] = None):
        asset = probe_media(path)
        return self._commit_operation(project_id, None, lambda project: register_asset(project, asset, target_track_id, timeline_start_frame), origin, actor,
                                      "import_media", f"Import media {asset.name}", guard_tokens=guard_tokens)

    @staticmethod
    def _safe_upload_name(filename: Optional[str]) -> str:
        raw = (filename or "video").replace("\\", "/")
        basename = raw.rsplit("/", 1)[-1]
        basename = re.sub(r"[^A-Za-z0-9._ -]+", "_", basename).strip(" .")
        return basename[:180] or "video"

    async def import_uploaded_media(self, project_id: str, upload, expected_revision: int,
                                    target_track_id: str | None = None, timeline_start_frame: int | None = None,
                                    origin: str = "rest", actor: Optional[dict[str, str]] = None,
                                    guard_tokens: Optional[list[str]] = None):
        # Validate the project before creating any managed media directory.
        self.store.load(project_id)
        safe_name = self._safe_upload_name(getattr(upload, "filename", None))
        project_root = self.media_root / project_id
        self.store.path_for(project_id)
        project_root.mkdir(parents=True, exist_ok=True)
        token = uuid4().hex
        temporary = project_root / f".upload-{token}.part"
        destination = project_root / f"{token}_{safe_name}"
        try:
            with temporary.open("wb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            asset = probe_media(str(destination), display_name=safe_name)
            return self._commit_operation(project_id, expected_revision,
                lambda project: register_asset(project, asset, target_track_id, timeline_start_frame), origin, actor,
                "import_media", f"Import media {asset.name}", guard_tokens=guard_tokens)
        except Exception:
            for path in (temporary, destination):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            close = getattr(upload, "close", None)
            if close is not None:
                await close()

    def _commit_operation(self, project_id: str, expected_revision: Optional[int], operation, origin: str,
                          actor: Optional[dict[str, str]], operation_name: str, summary: str,
                          *, guard_tokens: Optional[list[str]] = None):
        with self._project_lock(project_id):
            loaded = self.store.load_with_source(project_id)
            project = loaded.project
            if expected_revision is not None and project.revision != expected_revision:
                raise StaleRevisionError("Project revision is stale", project.revision)
            # Operations receive an isolated candidate. This protects the loaded
            # authoritative object even when an intake operation mutates in place.
            edited = operation(deepcopy(project))
            if project_to_dict(edited) == project_to_dict(project):
                return project
            footprint = MutationFootprint(project_wide=operation_name in {"add_track", "update_track", "delete_track", "reorder_track"})
            self.guards.authorize(project_id, project, edited, guard_tokens, footprint=footprint if footprint.project_wide else None)
            edited.revision = project.revision + 1
            edited.revision_id = self.store.revision_id_factory()
            return self.store.commit(loaded, edited, project.revision_id, origin,
                                     actor or {"type": "human"}, operation_name, summary, project.revision)

    def _mutate(self, project_id: str, expected_revision: int, operation, *args, origin: str = "rest",
                actor: Optional[dict[str, str]] = None, operation_name: str = "edit", summary: str = "Edit timeline",
                guard_tokens: Optional[list[str]] = None):
        return self._commit_operation(project_id, expected_revision,
                                      lambda project: operation(project, *args), origin, actor, operation_name, summary,
                                      guard_tokens=guard_tokens)

    def split(self, project_id, clip_id, timeline_frame, expected_revision, origin="rest", actor=None, guard_tokens=None): return self._mutate(project_id, expected_revision, split_clip, clip_id, timeline_frame, origin=origin, actor=actor, operation_name="split_clip", summary="Split clip", guard_tokens=guard_tokens)
    def delete(self, project_id, clip_id, expected_revision, origin="rest", actor=None, guard_tokens=None): return self._mutate(project_id, expected_revision, delete_clip, clip_id, origin=origin, actor=actor, operation_name="delete_clip", summary="Delete clip", guard_tokens=guard_tokens)
    def move(self, project_id, clip_id, timeline_start_frame, expected_revision, target_track_id=None, origin="rest", actor=None, guard_tokens=None): return self._mutate(project_id, expected_revision, move_clip, clip_id, timeline_start_frame, target_track_id, origin=origin, actor=actor, operation_name="move_clip", summary="Move clip", guard_tokens=guard_tokens)
    def trim(self, project_id, clip_id, expected_revision, source_in_frame=None, source_out_frame=None, origin="rest", actor=None, guard_tokens=None): return self._mutate(project_id, expected_revision, trim_clip, clip_id, source_in_frame, source_out_frame, origin=origin, actor=actor, operation_name="trim_clip", summary="Trim clip", guard_tokens=guard_tokens)

    def add_track(self, project_id, expected_revision, name, position=None, external_refs=None, origin="rest", actor=None, guard_tokens=None):
        refs = [item if isinstance(item, ExternalReference) else ExternalReference(item["system"], item["id"], item.get("kind", "")) for item in (external_refs or [])]
        return self._mutate(project_id, expected_revision, add_track, name, position, refs, origin=origin, actor=actor, operation_name="add_track", summary="Add video track", guard_tokens=guard_tokens)

    def update_track(self, project_id, track_id, expected_revision, name=None, external_refs=None, origin="rest", actor=None, guard_tokens=None):
        refs = None if external_refs is None else [item if isinstance(item, ExternalReference) else ExternalReference(item["system"], item["id"], item.get("kind", "")) for item in external_refs]
        return self._mutate(project_id, expected_revision, update_track, track_id, name, refs, origin=origin, actor=actor, operation_name="update_track", summary="Update video track", guard_tokens=guard_tokens)

    def delete_track(self, project_id, track_id, expected_revision, origin="rest", actor=None, guard_tokens=None):
        return self._mutate(project_id, expected_revision, delete_track, track_id, origin=origin, actor=actor, operation_name="delete_track", summary="Delete video track", guard_tokens=guard_tokens)

    def reorder_track(self, project_id, track_id, expected_revision, position, origin="rest", actor=None, guard_tokens=None):
        return self._mutate(project_id, expected_revision, reorder_track, track_id, position, origin=origin, actor=actor, operation_name="reorder_track", summary="Reorder video track", guard_tokens=guard_tokens)

    def add_marker(self, project_id: str, expected_revision: int, start_frame: int, end_frame=None,
                   name: str = "", description: str = "", marker_type: str = "generic", production=None,
                   origin="rest", actor=None, guard_tokens=None):
        marker = Marker(new_marker_id(), start_frame, end_frame, name, description, marker_type,
                        marker_production_from_dict(production))
        return self._mutate(project_id, expected_revision, add_marker, marker, origin=origin, actor=actor,
                            operation_name="add_marker", summary="Add timeline marker", guard_tokens=guard_tokens)

    def update_marker(self, project_id: str, expected_revision: int, marker_id: str, changes: dict,
                      origin="rest", actor=None, guard_tokens=None):
        normalized = dict(changes)
        if "production" in normalized:
            normalized["production"] = marker_production_from_dict(normalized["production"])
        return self._mutate(project_id, expected_revision, update_marker, marker_id, normalized,
                            origin=origin, actor=actor, operation_name="update_marker", summary="Update timeline marker",
                            guard_tokens=guard_tokens)

    def delete_marker(self, project_id: str, expected_revision: int, marker_id: str, origin="rest", actor=None, guard_tokens=None):
        return self._mutate(project_id, expected_revision, delete_marker, marker_id,
                            origin=origin, actor=actor, operation_name="delete_marker", summary="Delete timeline marker",
                            guard_tokens=guard_tokens)

    def trim_relative(self, project_id, clip_id, expected_revision, edge, frames_to_remove, origin="rest", actor=None, guard_tokens=None):
        if edge not in ("start", "end") or not isinstance(frames_to_remove, int) or frames_to_remove <= 0:
            raise ValidationError("edge must be start or end and frames_to_remove must be positive")
        def operation(project):
            clip = next((c for t in project.timeline.tracks for c in t.clips if c.id == clip_id), None)
            if clip is None: raise ValidationError(f"Clip does not exist: {clip_id}")
            return trim_clip(project, clip_id, clip.source_in_frame + frames_to_remove, None) if edge == "start" else trim_clip(project, clip_id, None, clip.source_out_frame - frames_to_remove)
        return self._commit_operation(project_id, expected_revision, operation, origin, actor, "trim_clip", "Trim clip", guard_tokens=guard_tokens)

    def move_to_gap(self, project_id, clip_id, gap_ordinal, expected_revision, target_track_id=None, origin="rest", actor=None, guard_tokens=None):
        if not isinstance(gap_ordinal, int) or gap_ordinal < 1: raise ValidationError("gap_ordinal must be positive")
        def operation(project):
            source_track = next((track for track in project.timeline.tracks if any(c.id == clip_id for c in track.clips)), None)
            if source_track is None:
                raise ValidationError(f"Clip does not exist: {clip_id}")
            target_id = target_track_id or source_track.id
            if not any(track.id == target_id for track in project.timeline.tracks):
                raise ValidationError("Target track does not exist")
            for track in project.timeline.tracks:
                clip = next((c for c in track.clips if c.id == clip_id), None)
                if clip:
                    without = deepcopy(project)
                    target = next(t for t in without.timeline.tracks if t.id == target_id)
                    for candidate in without.timeline.tracks:
                        candidate.clips = [c for c in candidate.clips if c.id != clip_id]
                    gaps = next(t["gaps"] for t in timeline_projection(without)["tracks"] if t["id"] == target_id)
                    gap = next((g for g in gaps if g["gap_ordinal"] == gap_ordinal), None)
                    if gap is None: raise ValidationError("Gap does not exist")
                    result = move_clip(project, clip_id, gap["start_frame"], target_id)
                    return result
            raise ValidationError(f"Clip does not exist: {clip_id}")
        return self._commit_operation(project_id, expected_revision, operation, origin, actor, "move_clip", "Move clip to gap", guard_tokens=guard_tokens)

    def render(self, project_id: str, expected_revision: int, render_type: str) -> RenderResult:
        if render_type not in ("preview", "export"): raise ValidationError("Unknown render type")
        with self._project_lock(project_id):
            project = self.store.load(project_id)
            if project.revision != expected_revision: raise StaleRevisionError("Project revision is stale", project.revision)
            plan = compile_render_plan(project)
            revision = project.revision
            folder = self.runtime_root / project.id / ("previews" if render_type == "preview" else "exports")
        return render_plan(plan, folder / f"revision-{revision}.mp4", revision, render_type)
    def render_preview(self, project_id, expected_revision): return self.render(project_id, expected_revision, "preview")
    def export_project(self, project_id, expected_revision): return self.render(project_id, expected_revision, "export")
    def render_path(self, project_id, render_type, revision):
        if render_type not in ("preview", "export"): raise ValidationError("Unknown render type")
        return self.runtime_root / project_id / ("previews" if render_type == "preview" else "exports") / f"revision-{revision}.mp4"

    def current_render(self, project_id: str, render_type: str):
        project = self.store.load(project_id)
        path = self.render_path(project_id, render_type, project.revision)
        if not path.is_file(): raise FileNotFoundError("Current rendered media does not exist")
        return {"revision": project.revision, "render_type": render_type,
                "url": f"/api/projects/{project_id}/renders/{render_type}/{project.revision}"}
