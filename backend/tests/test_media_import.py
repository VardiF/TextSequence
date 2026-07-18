from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routes import service
from app.domain.models import project_from_dict
from app.main import app
from app.persistence.project_store import ProjectStore


def test_health_reports_project_local_ffprobe():
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json()["ffprobe"]["available"] is True


def test_real_media_import_creates_asset_clip_and_streams(tmp_path: Path, media_path: Path):
    service.store = ProjectStore(tmp_path / "projects")
    client = TestClient(app)
    created = client.post("/api/projects", json={"name": "Media smoke"})
    assert created.status_code == 200
    project_id = created.json()["id"]

    imported = client.post(f"/api/projects/{project_id}/assets", json={"path": str(media_path)})
    assert imported.status_code == 200, imported.text
    project = project_from_dict(imported.json())
    assert project.fps.as_tuple() == (24, 1)
    assert project.assets[0].codec == "h264"
    assert project.assets[0].width == 320
    assert project.assets[0].height == 180
    assert project.assets[0].duration_frames == 48
    assert project.tracks[0].name == "V1"
    assert project.tracks[0].clips[0].source_in_frame == 0
    assert project.tracks[0].clips[0].source_out_frame == 48
    reopened = client.get(f"/api/projects/{project_id}")
    assert reopened.status_code == 200
    assert reopened.json()["revision"] == 1
    assert reopened.json()["assets"][0]["fps"] == {"numerator": 24, "denominator": 1}

    streamed = client.get(f"/api/projects/{project_id}/assets/{project.assets[0].id}/media", headers={"Range": "bytes=0-31"})
    assert streamed.status_code in (200, 206)
    assert streamed.headers["content-type"].startswith("video/mp4")
    assert len(streamed.content) > 0
