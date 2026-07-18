from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture(scope="session")
def media_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("media") / "known-cfr-h264-aac.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for media fixtures")
    subprocess.run([
        ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
        "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart", str(output),
    ], check=True, capture_output=True)
    return output


@pytest.fixture(scope="session")
def silence_media_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("silence-media") / "tone-silence-tone.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for media fixtures")
    subprocess.run([
        ffmpeg, "-y",
        "-f", "lavfi", "-i", "testsrc=size=160x90:rate=24:duration=4.5",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=1",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=1",
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000:duration=0.5",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=1",
        "-filter_complex", "[1:a][2:a][3:a][4:a][5:a]concat=n=5:v=0:a=1[a]",
        "-map", "0:v", "-map", "[a]", "-t", "4.5", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-c:a", "aac", str(output),
    ], check=True, capture_output=True)
    return output
