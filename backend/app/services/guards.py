"""Server-enforced, bearer-capability edit guards."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from app.guard_models import (
    EditGuard, GuardError, GuardScope, GuardStateError, MutationFootprint,
    MAX_ACTIVE_GUARDS, normalize_owner, normalize_scope, normalize_tokens, normalize_ttl,
    GUARD_SCHEMA_VERSION, iso_time, mutation_footprint, parse_time, utc_now,
)

if TYPE_CHECKING:
    from app.domain.models import Project
    from app.services.projects import ProjectService


_PROJECT_ID = re.compile(r"project_[A-Za-z0-9_-]{1,127}\Z")
_GUARD_ID = re.compile(r"guard_[0-9a-f]{32}\Z")


class GuardStore:
    def __init__(self, runtime_root: Path) -> None:
        self.root = runtime_root / "guards"

    def path_for(self, project_id: str) -> Path:
        if not _PROJECT_ID.fullmatch(project_id):
            raise GuardStateError()
        return self.root / f"{project_id}.json"

    def load(self, project_id: str) -> list[EditGuard]:
        path = self.path_for(project_id)
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as handle:
                document = json.load(handle)
            if not isinstance(document, dict) or set(document) != {"guard_schema_version", "project_id", "guards"}:
                raise ValueError("invalid guard document")
            if document["guard_schema_version"] != GUARD_SCHEMA_VERSION or document["project_id"] != project_id or not isinstance(document["guards"], list):
                raise ValueError("invalid guard document")
            result = [self._parse_record(project_id, item) for item in document["guards"]]
            if len({item.guard_id for item in result}) != len(result):
                raise ValueError("duplicate guard")
            return sorted(result, key=lambda item: item.guard_id)
        except GuardStateError:
            raise
        except Exception as exc:
            raise GuardStateError() from exc

    @staticmethod
    def _parse_record(project_id: str, value: Any) -> EditGuard:
        if not isinstance(value, dict) or set(value) != {"guard_id", "project_id", "owner", "scope", "purpose", "created_at", "expires_at", "capability_sha256"}:
            raise ValueError("invalid guard record")
        if value["project_id"] != project_id or not _GUARD_ID.fullmatch(value["guard_id"]):
            raise ValueError("invalid guard identity")
        owner = normalize_owner(value["owner"])
        # A persisted scope is normalized and strict, but acquisition-time ID
        # existence validation is intentionally not repeated on reload.
        scope_data = value["scope"]
        if not isinstance(scope_data, dict) or scope_data.get("kind") not in {"project", "selection"}:
            raise ValueError("invalid scope")
        if scope_data.get("kind") == "project":
            if set(scope_data) != {"kind"}:
                raise ValueError("invalid project scope")
            scope = GuardScope("project")
        else:
            if set(scope_data) != {"kind", "clip_ids", "marker_ids", "frame_ranges"}:
                raise ValueError("invalid selection scope")
            clip_ids, marker_ids, ranges = scope_data["clip_ids"], scope_data["marker_ids"], scope_data["frame_ranges"]
            if not all(isinstance(item, str) and item for item in clip_ids + marker_ids):
                raise ValueError("invalid selectors")
            from app.guard_models import FrameRange, _merge_ranges
            parsed = [FrameRange(item["start_frame"], item["end_frame"]) for item in ranges]
            if any(item.start_frame < 0 or item.end_frame <= item.start_frame for item in parsed):
                raise ValueError("invalid range")
            scope = GuardScope("selection", tuple(sorted(set(clip_ids))), tuple(sorted(set(marker_ids))), tuple(_merge_ranges(parsed)))
            if not scope.clip_ids and not scope.marker_ids and not scope.frame_ranges:
                raise ValueError("empty selection")
        if value["purpose"] is not None and (not isinstance(value["purpose"], str) or len(value["purpose"]) > 500):
            raise ValueError("invalid purpose")
        created, expires = parse_time(value["created_at"]), parse_time(value["expires_at"])
        if expires <= created or not re.fullmatch(r"[0-9a-f]{64}", value["capability_sha256"]):
            raise ValueError("invalid guard lease")
        return EditGuard(value["guard_id"], project_id, owner, scope, value["purpose"], iso_time(created), iso_time(expires), value["capability_sha256"])

    def save(self, project_id: str, guards: list[EditGuard]) -> None:
        path = self.path_for(project_id)
        temporary: Path | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
            payload = {"guard_schema_version": GUARD_SCHEMA_VERSION, "project_id": project_id,
                       "guards": [item.persisted() for item in sorted(guards, key=lambda item: item.guard_id)]}
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = None
        except GuardStateError:
            raise
        except Exception as exc:
            raise GuardStateError() from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


def _active(guards: list[EditGuard], now) -> list[EditGuard]:
    return sorted([guard for guard in guards if parse_time(guard.expires_at) > now], key=lambda item: item.guard_id)


def _overlap(left, right) -> bool:
    return left.start_frame < right.end_frame and right.start_frame < left.end_frame


def _clip_spans(project: "Project") -> dict[str, tuple[int, int]]:
    return {clip.id: (clip.timeline_start_frame, clip.timeline_start_frame + clip.duration_frames)
            for track in project.timeline.tracks for clip in track.clips}


def _marker_spans(project: "Project") -> dict[str, tuple[int, int]]:
    return {marker.id: (marker.start_frame, marker.end_frame if marker.end_frame is not None else marker.start_frame + 1)
            for marker in project.timeline.markers}


def scopes_conflict(left: GuardScope, right: GuardScope, project: "Project") -> bool:
    if left.kind == "project" or right.kind == "project":
        return True
    if set(left.clip_ids) & set(right.clip_ids) or set(left.marker_ids) & set(right.marker_ids):
        return True
    if any(_overlap(a, b) for a in left.frame_ranges for b in right.frame_ranges):
        return True
    clips, markers = _clip_spans(project), _marker_spans(project)
    for selected_ids, spans in ((left.clip_ids, clips), (left.marker_ids, markers)):
        for entity_id in selected_ids:
            if entity_id in spans and any(_overlap_range(spans[entity_id], frame_range) for frame_range in right.frame_ranges):
                return True
    for selected_ids, spans in ((right.clip_ids, clips), (right.marker_ids, markers)):
        for entity_id in selected_ids:
            if entity_id in spans and any(_overlap_range(spans[entity_id], frame_range) for frame_range in left.frame_ranges):
                return True
    return False


def _overlap_range(span: tuple[int, int], frame_range) -> bool:
    return span[0] < frame_range.end_frame and frame_range.start_frame < span[1]


def footprint_conflicts(scope: GuardScope, footprint: MutationFootprint) -> bool:
    if scope.kind == "project" or footprint.project_wide:
        return True
    if set(scope.clip_ids) & set(footprint.clip_ids) or set(scope.marker_ids) & set(footprint.marker_ids):
        return True
    return any(_overlap(a, b) for a in scope.frame_ranges for b in footprint.frame_ranges)


class GuardService:
    def __init__(self, projects: "ProjectService") -> None:
        self.projects = projects
        self.store = GuardStore(projects.runtime_root)

    def acquire(self, project_id: str, owner: Any, scope: Any, ttl_seconds: Any = None, purpose: Any = None) -> dict[str, Any]:
        owner_value = normalize_owner(owner)
        ttl = normalize_ttl(ttl_seconds)
        if purpose is not None and (not isinstance(purpose, str) or len(purpose) > 500):
            raise GuardError("INVALID_GUARD_SCOPE", "Guard purpose is invalid")
        with self.projects._project_lock(project_id):
            project = self.projects.store.load(project_id)
            guards = self.store.load(project_id)
            now = utc_now()
            active = _active(guards, now)
            scope_value = normalize_scope(scope, project)
            if any(scopes_conflict(existing.scope, scope_value, project) for existing in active):
                raise GuardError("GUARD_CONFLICT", "The requested edit guard conflicts with an active edit guard")
            if len(active) >= MAX_ACTIVE_GUARDS:
                raise GuardError("GUARD_LIMIT_EXCEEDED", "The active edit guard limit has been reached")
            token = f"guard_token_{secrets.token_urlsafe(32)}"
            guard = EditGuard(
                guard_id=f"guard_{uuid4().hex}", project_id=project_id, owner=owner_value, scope=scope_value,
                purpose=purpose, created_at=iso_time(now), expires_at=iso_time(now + timedelta(seconds=ttl)),
                capability_sha256=hashlib.sha256(token.encode()).hexdigest(),
            )
            self.store.save(project_id, active + [guard])
            return {"ok": True, **guard.metadata(), "guard_token": token}

    def renew(self, project_id: str, guard_id: str, guard_token: str, ttl_seconds: Any = None) -> dict[str, Any]:
        ttl = normalize_ttl(ttl_seconds)
        with self.projects._project_lock(project_id):
            self.projects.store.load(project_id)
            guards = self.store.load(project_id)
            now = utc_now()
            active = _active(guards, now)
            guard = next((item for item in active if item.guard_id == guard_id), None)
            if guard is None:
                raise GuardError("GUARD_NOT_FOUND", "Edit guard is not active")
            self._check_capability(guard, guard_token)
            renewed = EditGuard(guard.guard_id, guard.project_id, guard.owner, guard.scope, guard.purpose,
                                guard.created_at, iso_time(now + timedelta(seconds=ttl)), guard.capability_sha256)
            self.store.save(project_id, [item for item in active if item.guard_id != guard_id] + [renewed])
            return {"ok": True, **renewed.metadata()}

    def release(self, project_id: str, guard_id: str, guard_token: str) -> dict[str, Any]:
        with self.projects._project_lock(project_id):
            self.projects.store.load(project_id)
            guards = self.store.load(project_id)
            active = _active(guards, utc_now())
            guard = next((item for item in active if item.guard_id == guard_id), None)
            if guard is None:
                return {"ok": True, "status": "not_active", "guard_id": guard_id}
            self._check_capability(guard, guard_token)
            self.store.save(project_id, [item for item in active if item.guard_id != guard_id])
            return {"ok": True, "status": "released", "guard_id": guard_id}

    def list(self, project_id: str) -> dict[str, Any]:
        with self.projects._project_lock(project_id):
            self.projects.store.load(project_id)
            observed_at = utc_now()
            guards = _active(self.store.load(project_id), observed_at)
            return {"ok": True, "project_id": project_id, "observed_at": iso_time(observed_at),
                    "guards": [guard.metadata() for guard in guards]}

    @staticmethod
    def _check_capability(guard: EditGuard, token: Any) -> None:
        if not isinstance(token, str) or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), guard.capability_sha256):
            raise GuardError("GUARD_CAPABILITY_INVALID", "The edit guard capability is invalid")

    def authorize(self, project_id: str, before: "Project", final: "Project", supplied_guard_tokens: Any = None,
                  *, footprint: MutationFootprint | None = None) -> None:
        tokens = normalize_tokens(supplied_guard_tokens)
        guards = _active(self.store.load(project_id), utc_now())
        if not guards:
            return
        footprint = (footprint or mutation_footprint(before, final)).normalized()
        conflicts = [guard for guard in guards if footprint_conflicts(guard.scope, footprint)]
        if not conflicts:
            return
        matched = set()
        for guard in conflicts:
            if any(hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), guard.capability_sha256) for token in tokens):
                matched.add(guard.guard_id)
        if len(matched) != len(conflicts):
            if tokens and not matched:
                raise GuardError("GUARD_CAPABILITY_INVALID", "The edit guard capability is invalid")
            raise GuardError("GUARD_CONFLICT", "This edit is protected by an active edit guard",
                             conflicts=[{"guard_id": guard.guard_id, "expires_at": guard.expires_at} for guard in conflicts if guard.guard_id not in matched])
