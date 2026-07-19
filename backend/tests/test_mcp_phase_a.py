import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from app.domain.models import Asset, FrameRate, ValidationError
from app.domain.operations import new_project, register_asset
from app.persistence.project_store import ProjectStore, StaleRevisionError
from app.services.projects import ProjectService
from app.services.timeline import timeline_projection
from app.mcp_server import mcp
from app.agent.context import EditorContextError, EditorContextStore
from app.agent.runtime import AGENT_INSTRUCTIONS, AgentRuntime
from agents.items import ToolCallItem, ToolCallOutputItem


def seeded_service(tmp_path):
    service = ProjectService(ProjectStore(tmp_path), tmp_path / "runtime")
    project = register_asset(new_project("Projection"), Asset("asset", "/safe/media.mp4", "media.mp4", "h264", 1, 1, FrameRate(24, 1), 100))
    service.store.save(project)
    return service, project


def test_mcp_tool_discovery_contains_phase_a_surface():
    mcp.streamable_http_app()
    assert {tool.name for tool in mcp._tool_manager.list_tools()} == {
        "list_projects", "get_timeline", "get_editor_context", "split_clip", "delete_clip", "move_clip",
        "trim_clip", "render_preview", "export_project", "analyze_silence", "remove_silence",
    }


def test_editor_context_is_validated_against_authoritative_project(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.tracks[0].clips[0].id
    contexts = EditorContextStore(service)
    context = contexts.capture({"editor_session_id": "editor_test", "project_id": project.id,
                                "observed_revision": 0, "selected_clip_id": clip_id,
                                "playhead_frame": 12, "visible_track_id": project.tracks[0].id})
    result = contexts.get(context.editor_session_id)
    assert result["selected_clip_exists"] is True
    assert result["current_project_revision"] == 0
    with pytest.raises(EditorContextError) as missing:
        contexts.get("editor_unknown")
    assert missing.value.code == "EDITOR_CONTEXT_MISSING"
    with pytest.raises(EditorContextError) as invalid:
        contexts.capture({"editor_session_id": "editor_bad", "project_id": project.id,
                          "observed_revision": 0, "selected_clip_id": "clip_missing", "playhead_frame": 1})
    assert invalid.value.code == "INVALID_SELECTION"


def test_agent_runtime_uses_mcp_server_adapter_and_sanitizes_actions(monkeypatch):
    class FakeMCP:
        def __init__(self, params, **kwargs):
            self.params = params
            self.kwargs = kwargs
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None

    class FakeRunner:
        async def run(self, agent, prompt, **_kwargs):
            assert agent.mcp_servers[0].params["url"] == "http://127.0.0.1:8000/mcp"
            assert "get_editor_context" in AGENT_INSTRUCTIONS
            assert "editor_test" in prompt
            dummy_agent = type("DummyAgent", (), {})()
            call = ToolCallItem(dummy_agent, {"name": "split_clip", "arguments": '{"clip_id":"clip_1","path":"/private/file.mp4"}'})
            output = ToolCallOutputItem(dummy_agent, {}, '{"ok":true,"revision":4}')
            return SimpleNamespace(final_output="Done.", new_items=[call, output])

    monkeypatch.setenv("OPENAI_API_KEY", "test-only-not-sent")
    import app.agent.runtime as runtime_module
    monkeypatch.setattr(runtime_module, "MCPServerStreamableHttp", FakeMCP)
    runtime = AgentRuntime(runner=FakeRunner())
    result = asyncio.run(runtime.run("editor_test", "Split this here"))
    assert result.message == "Done."
    assert result.actions[0]["tool"] == "split_clip"
    assert "path" not in result.actions[0]["arguments"]
    assert "revision 4" in result.actions[0]["summary"]


def test_project_ids_are_path_safe(tmp_path):
    store = ProjectStore(tmp_path)
    with pytest.raises(ValidationError): store.path_for("../escape")
    with pytest.raises(ValidationError): store.path_for("/absolute")
    with pytest.raises(ValidationError): store.path_for("nested/name")


def test_timeline_projection_has_stable_order_and_gaps(tmp_path):
    service, project = seeded_service(tmp_path)
    project = service.move(project.id, project.tracks[0].clips[0].id, 10, 0)
    first = project.tracks[0].clips[0]
    project = service.split(project.id, first.id, 40, 1)
    projection = service.timeline(project.id)
    track = projection["tracks"][0]
    assert [clip["ordinal"] for clip in track["clips"]] == [1, 2]
    assert track["clips"][0]["timeline_start_frame"] == 10
    assert track["gaps"][0] == {"gap_ordinal": 1, "start_frame": 0, "end_frame": 10, "duration_frames": 10}
    assert "path" not in str(projection)


def test_same_expected_revision_concurrent_mutations_have_one_winner(tmp_path):
    service, project = seeded_service(tmp_path)
    clip_id = project.tracks[0].clips[0].id
    def mutate(frame):
        try:
            return service.split(project.id, clip_id, frame, 0)
        except StaleRevisionError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(mutate, (30, 60)))
    assert sum(isinstance(result, StaleRevisionError) for result in results) == 1
    assert service.get(project.id).revision == 1
