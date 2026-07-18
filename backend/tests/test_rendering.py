import json
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.routes import service
from app.domain.models import Asset, Clip, FrameRate
from app.domain.operations import new_project
from app.media.probe import find_ffprobe
from app.main import app
from app.persistence.project_store import ProjectStore
from app.rendering.ffmpeg import render_plan
from app.rendering.plan import compile_render_plan


def test_ffmpeg_render_includes_gap_and_valid_media(tmp_path: Path, media_path: Path):
    project = new_project("Render integration")
    project.fps = FrameRate(24, 1)
    project.assets.append(Asset("asset", str(media_path), media_path.name, "h264", 320, 180, FrameRate(24, 1), 48))
    project.tracks[0].clips = [
        Clip("a", "asset", 0, 24, 0),
        Clip("b", "asset", 24, 36, 36),
    ]
    project.validate()
    output = tmp_path / "preview.mp4"
    result = render_plan(compile_render_plan(project), output, project.revision, "preview")
    assert result.duration_frames == 48
    ffprobe = find_ffprobe()
    assert ffprobe
    inspected = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height,r_frame_rate", "-of", "json", str(output)], check=True, capture_output=True, text=True)
    payload = json.loads(inspected.stdout)
    assert payload["streams"][0]["codec_name"] == "h264"
    assert payload["streams"][0]["width"] == 320
    assert payload["streams"][0]["height"] == 180
    assert payload["streams"][0]["r_frame_rate"] == "24/1"
    assert 1.9 < float(payload["format"]["duration"]) < 2.1


def test_render_api_loads_canonical_revision_and_serves_output(tmp_path: Path, media_path: Path):
    service.store = ProjectStore(tmp_path / "projects")
    service.runtime_root = tmp_path / "runtime"
    project = new_project("Render API")
    project.fps = FrameRate(24, 1)
    project.assets.append(Asset("asset", str(media_path), media_path.name, "h264", 320, 180, FrameRate(24, 1), 48))
    project.tracks[0].clips = [Clip("a", "asset", 0, 48, 0)]
    service.store.save(project)
    client = TestClient(app)
    response = client.post(f"/api/projects/{project.id}/render-preview", json={"expected_revision": 0})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["revision"] == 0
    served = client.get(result["url"])
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("video/mp4")
