from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Optional

from app.domain.models import ValidationError


class EditorContextError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class EditorContext:
    editor_session_id: str
    active_project_id: str
    observed_project_revision: int
    selected_clip_id: Optional[str]
    playhead_frame: int
    visible_track_id: Optional[str]
    captured_at: str


class EditorContextStore:
    def __init__(self, project_service):
        self._service = project_service
        self._contexts: dict[str, EditorContext] = {}
        self._lock = RLock()

    @staticmethod
    def _validate_id(value: str, label: str) -> None:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
            raise EditorContextError("INVALID_ARGUMENT", f"Invalid {label}")

    def capture(self, snapshot: dict) -> EditorContext:
        required = ("editor_session_id", "project_id", "observed_revision", "playhead_frame")
        if any(key not in snapshot for key in required):
            raise EditorContextError("INVALID_ARGUMENT", "Editor context is missing required fields")
        session_id = snapshot["editor_session_id"]
        self._validate_id(session_id, "editor_session_id")
        if not session_id.startswith("editor_"):
            raise EditorContextError("INVALID_ARGUMENT", "editor_session_id must use the editor_ prefix")
        project_id = snapshot["project_id"]
        self._validate_id(project_id, "project_id")
        try:
            project = self._service.get(project_id)
        except (FileNotFoundError, ValidationError) as exc:
            raise EditorContextError("PROJECT_NOT_FOUND", "Active project does not exist") from exc
        revision = snapshot["observed_revision"]
        playhead = snapshot["playhead_frame"]
        if not isinstance(revision, int) or revision < 0 or not isinstance(playhead, int) or playhead < 0:
            raise EditorContextError("INVALID_ARGUMENT", "Revision and playhead_frame must be non-negative integers")
        selected = snapshot.get("selected_clip_id")
        visible_track = snapshot.get("visible_track_id")
        if selected is not None:
            self._validate_id(selected, "selected_clip_id")
            if not any(clip.id == selected for track in project.timeline.tracks for clip in track.clips):
                raise EditorContextError("INVALID_SELECTION", "Selected clip does not exist in the current project")
        if visible_track is not None:
            self._validate_id(visible_track, "visible_track_id")
            if not any(track.id == visible_track for track in project.timeline.tracks):
                raise EditorContextError("INVALID_ARGUMENT", "Visible track does not exist in the current project")
        context = EditorContext(session_id, project_id, revision, selected, playhead, visible_track,
                                datetime.now(timezone.utc).isoformat())
        with self._lock:
            self._contexts[session_id] = context
        return context

    def get(self, editor_session_id: str) -> dict:
        self._validate_id(editor_session_id, "editor_session_id")
        with self._lock:
            context = self._contexts.get(editor_session_id)
        if context is None:
            raise EditorContextError("EDITOR_CONTEXT_MISSING", "No submitted editor context exists for this session")
        try:
            project = self._service.get(context.active_project_id)
        except (FileNotFoundError, ValidationError) as exc:
            raise EditorContextError("PROJECT_NOT_FOUND", "Active project no longer exists") from exc
        selected_exists = context.selected_clip_id is None or any(
        clip.id == context.selected_clip_id for track in project.timeline.tracks for clip in track.clips
        )
        if not selected_exists:
            raise EditorContextError("INVALID_SELECTION", "The selected clip no longer exists")
        return {
            "editor_session_id": context.editor_session_id,
            "active_project_id": context.active_project_id,
            "observed_project_revision": context.observed_project_revision,
            "current_project_revision": project.revision,
            "selected_clip_id": context.selected_clip_id,
            "selected_clip_exists": selected_exists,
            "playhead_frame": context.playhead_frame,
            "visible_track_id": context.visible_track_id,
            "captured_at": context.captured_at,
        }
