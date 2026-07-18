from fastapi.testclient import TestClient

from app.api.routes import service
from app.domain.models import Asset, FrameRate
from app.domain.operations import new_project, register_asset
from app.main import app
from app.persistence.project_store import ProjectStore


def test_clip_mutation_endpoints_return_authoritative_state(tmp_path):
    service.store = ProjectStore(tmp_path)
    project = register_asset(new_project("API editing"), Asset("asset", "/tmp/edit.mp4", "edit.mp4", "h264", 1, 1, FrameRate(24, 1), 100))
    service.store.save(project)
    client = TestClient(app)

    split = client.post(f"/api/projects/{project.id}/clips/split", json={"clip_id": project.tracks[0].clips[0].id, "timeline_frame": 40, "expected_revision": 1})
    assert split.status_code == 200
    split_project = split.json()
    assert split_project["revision"] == 2
    assert len(split_project["tracks"][0]["clips"]) == 2
    second_id = split_project["tracks"][0]["clips"][1]["id"]

    conflict = client.post(f"/api/projects/{project.id}/clips/move", json={"clip_id": second_id, "timeline_start_frame": 0, "expected_revision": 2})
    assert conflict.status_code == 400
    stale = client.post(f"/api/projects/{project.id}/clips/delete", json={"clip_id": second_id, "expected_revision": 1})
    assert stale.status_code == 409

    moved = client.post(f"/api/projects/{project.id}/clips/move", json={"clip_id": second_id, "timeline_start_frame": 120, "expected_revision": 2})
    assert moved.status_code == 200
    assert moved.json()["revision"] == 3

    trimmed = client.post(f"/api/projects/{project.id}/clips/trim", json={"clip_id": second_id, "source_out_frame": 80, "expected_revision": 3})
    assert trimmed.status_code == 200
    assert trimmed.json()["revision"] == 4
    clip = next(item for item in trimmed.json()["tracks"][0]["clips"] if item["id"] == second_id)
    assert clip["timeline_start_frame"] == 120
    assert clip["source_out_frame"] == 80


def test_agent_chat_without_key_keeps_nle_available(tmp_path, monkeypatch):
    service.store = ProjectStore(tmp_path)
    project = register_asset(new_project("Chat config"), Asset("asset", "/tmp/edit.mp4", "edit.mp4", "h264", 1, 1, FrameRate(24, 1), 100))
    service.store.save(project)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    clip_id = project.tracks[0].clips[0].id
    response = TestClient(app).post("/api/agent/chat", json={
        "editor_session_id": "editor_chat_test", "message": "Show me the timeline",
        "editor_context": {"editor_session_id": "editor_chat_test", "project_id": project.id,
                            "observed_revision": 1, "selected_clip_id": clip_id,
                            "playhead_frame": 12, "visible_track_id": project.tracks[0].id},
    })
    assert response.status_code == 200
    assert response.json()["error"]["code"] == "OPENAI_API_KEY_MISSING"
