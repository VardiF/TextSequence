import json
import subprocess
from copy import deepcopy

import pytest

from app.audio.silence import SilenceAnalysisError, SilenceInterval, milliseconds_to_frames, parse_silencedetect, seconds_text_to_frame
from app.domain.models import Asset, FrameRate
from app.domain.operations import new_project, register_asset
from app.domain.silence import SourceRemovalRange, apply_silence_removals
from app.persistence.project_store import ProjectStore, StaleRevisionError
from app.services.projects import ProjectService
from app.media.probe import find_ffprobe, probe_media


def make_service(tmp_path, media_path):
    service = ProjectService(ProjectStore(tmp_path / "projects"), tmp_path / "runtime")
    asset = probe_media(str(media_path))
    project = register_asset(new_project("Silence test"), asset)
    service.store.save(project)
    return service, project


def test_silence_parser_and_frame_conversion():
    output = """[silencedetect] silence_start: 0.5
[silencedetect] silence_end: 1.5 | silence_duration: 1
[silencedetect] silence_start: 3.5
"""
    assert parse_silencedetect(output, (24, 1), 120) == (
        # The open-ended range is closed at the known asset duration.
        SilenceInterval(12, 36), SilenceInterval(84, 120),
    )
    assert seconds_text_to_frame("1.500", (24, 1)) == 36
    assert milliseconds_to_frames(700, (24, 1)) == 17
    with pytest.raises(SilenceAnalysisError): parse_silencedetect("silence_end: 1", (24, 1), 24)


def test_analysis_thresholds_and_padding(silence_media_path, tmp_path):
    service, project = make_service(tmp_path, silence_media_path)
    analysis = service.analyze_silence(project.id)
    assert analysis["summary"]["detected_silences"] == 1
    assert analysis["silences"][0]["start_frame"] == 24
    assert analysis["silences"][0]["end_frame"] == 48
    shorter_threshold = service.analyze_silence(project.id, 400)
    assert shorter_threshold["summary"]["detected_silences"] == 2
    result = service.remove_silence(project.id, 1, keep_padding_ms=100)
    assert result["revision"] == 2
    assert result["previous_revision"] == 1
    assert result["removed_frames"] == 20
    assert result["removed_duration_ms"] == 833
    assert service.get(project.id).revision == 2


def test_timeline_mapping_intersects_trimmed_and_moved_clips(tmp_path):
    service = ProjectService(ProjectStore(tmp_path / "projects"))
    project = new_project("Mapping")
    asset = Asset("asset", "/tmp/media.mp4", "media.mp4", "h264", 1, 1, FrameRate(24, 1), 200)
    project = register_asset(project, asset)
    track = project.tracks[0]
    first = track.clips[0]
    first.source_in_frame, first.source_out_frame, first.timeline_start_frame = 20, 60, 100
    track.clips.append(type(first)("clip_second", asset.id, 60, 100, 200))
    project.validate()
    edited, removed_frames, count, ranges = apply_silence_removals(project, [SourceRemovalRange(asset.id, 24, 48)])
    assert removed_frames == 24
    assert count == 1
    assert [(clip.source_in_frame, clip.source_out_frame, clip.timeline_start_frame) for clip in edited.tracks[0].clips] == [
        (20, 24, 100), (48, 60, 104), (60, 100, 176)
    ]
    assert ranges[0]["timeline_start_frame"] == 104


def test_remove_silence_renders_shorter_h264_aac_media(silence_media_path, tmp_path):
    service, project = make_service(tmp_path, silence_media_path)
    result = service.remove_silence(project.id, 1)
    rendered = service.render_preview(project.id, result["revision"])
    ffprobe = find_ffprobe()
    assert ffprobe
    result_probe = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-of", "json", rendered.path], check=True, capture_output=True, text=True)
    streams = json.loads(result_probe.stdout)["streams"]
    assert {stream["codec_name"] for stream in streams} >= {"h264", "aac"}
    assert rendered.duration_frames == 84


def test_remove_silence_rejects_revision_changed_during_analysis(silence_media_path, tmp_path, monkeypatch):
    service, project = make_service(tmp_path, silence_media_path)
    original = service._analyze_project_silence

    def analyze_then_external_edit(current, minimum_ms, threshold_db):
        analyses = original(current, minimum_ms, threshold_db)
        changed = deepcopy(service.get(project.id))
        changed.revision = 2
        service.store.save(changed)
        return analyses

    monkeypatch.setattr(service, "_analyze_project_silence", analyze_then_external_edit)
    with pytest.raises(StaleRevisionError) as error:
        service.remove_silence(project.id, 1)
    assert error.value.current_revision == 2
    assert service.get(project.id).revision == 2
