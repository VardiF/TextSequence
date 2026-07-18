from dataclasses import dataclass
from pathlib import Path

from app.persistence.project_store import ProjectStore
from app.services.projects import ProjectService
from app.agent.context import EditorContextStore


@dataclass
class Application:
    projects: ProjectService
    editor_contexts: EditorContextStore


def build_application(project_root: Path | None = None, runtime_root: Path | None = None) -> Application:
    projects = ProjectService(ProjectStore(project_root or Path("projects")), runtime_root or Path("runtime"))
    return Application(projects, EditorContextStore(projects))


application = build_application()
