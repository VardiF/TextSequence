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


def _upload_test_project(tmp_path: Path):
    service.store = ProjectStore(tmp_path / "projects")
    service.media_root = tmp_path / "media"
    response = TestClient(app).post("/api/projects", json={"name": "Upload smoke"})
    assert response.status_code == 200
    return TestClient(app), response.json()["id"]


def test_multipart_upload_uses_managed_storage_and_one_revision(tmp_path: Path, media_path: Path):
    client, project_id = _upload_test_project(tmp_path)
    with media_path.open("rb") as handle:
        response = client.post(f"/api/projects/{project_id}/assets/upload", data={"expected_revision": "0"},
                               files={"file": ("../../selected video.mp4", handle, "video/mp4")})
    assert response.status_code == 200, response.text
    project = response.json()
    assert project["revision"] == 1
    asset = project["assets"][0]
    managed = Path(asset["path"])
    assert managed.is_file()
    assert managed.parent == tmp_path / "media" / project_id
    assert managed.name != "../../selected video.mp4"
    assert asset["name"] == "selected video.mp4"
    assert len(list(managed.parent.iterdir())) == 1


def test_same_upload_filename_never_overwrites_existing_media(tmp_path: Path, media_path: Path):
    client, project_id = _upload_test_project(tmp_path)
    with media_path.open("rb") as handle:
        first = client.post(f"/api/projects/{project_id}/assets/upload", data={"expected_revision": "0"},
                            files={"file": ("same.mp4", handle, "video/mp4")})
    assert first.status_code == 200, first.text
    first_project = first.json()
    first_path = first_project["assets"][0]["path"]
    clip_id = first_project["timeline"]["tracks"][0]["clips"][0]["id"]
    deleted = client.post(f"/api/projects/{project_id}/clips/delete", json={"clip_id": clip_id, "expected_revision": 1})
    assert deleted.status_code == 200
    with media_path.open("rb") as handle:
        second = client.post(f"/api/projects/{project_id}/assets/upload", data={"expected_revision": "2"},
                             files={"file": ("same.mp4", handle, "video/mp4")})
    assert second.status_code == 200, second.text
    paths = [asset["path"] for asset in second.json()["assets"]]
    assert paths[0] == first_path
    assert paths[0] != paths[1]
    assert all(Path(path).is_file() for path in paths)
    assert len(list(Path(first_path).parent.iterdir())) == 2


def test_invalid_upload_is_cleaned_up_and_does_not_mutate_project(tmp_path: Path):
    client, project_id = _upload_test_project(tmp_path)
    response = client.post(f"/api/projects/{project_id}/assets/upload", data={"expected_revision": "0"},
                           files={"file": ("not-a-video.mp4", b"not media", "video/mp4")})
    assert response.status_code == 400
    project = client.get(f"/api/projects/{project_id}").json()
    assert project["revision"] == 0
    assert project["assets"] == []
    assert not list((tmp_path / "media" / project_id).glob("*") )


def test_stale_upload_does_not_create_managed_file_or_asset(tmp_path: Path, media_path: Path):
    client, project_id = _upload_test_project(tmp_path)
    with media_path.open("rb") as handle:
        response = client.post(f"/api/projects/{project_id}/assets/upload", data={"expected_revision": "99"},
                               files={"file": ("stale.mp4", handle, "video/mp4")})
    assert response.status_code == 409
    project = client.get(f"/api/projects/{project_id}").json()
    assert project["revision"] == 0
    assert project["assets"] == []
    assert not list((tmp_path / "media" / project_id).glob("*") )
