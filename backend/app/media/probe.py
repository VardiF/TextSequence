from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from app.domain.frame_math import parse_rational
from app.domain.models import Asset, FrameRate


class ProbeError(ValueError):
    pass


def find_ffprobe() -> str | None:
    candidates = [
        os.environ.get("FFPROBE_BIN"),
        str(Path(__file__).resolve().parents[3] / ".tools" / "ffprobe" / "ffprobe"),
        shutil.which("ffprobe"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def probe_media(path: str) -> Asset:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ProbeError(f"Media file does not exist: {source}")
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise ProbeError("ffprobe is required; install FFmpeg, configure FFPROBE_BIN, or provide .tools/ffprobe/ffprobe")
    command = [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries", "stream=codec_name,width,height,r_frame_rate,nb_read_frames", "-of", "json", str(source)]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        payload = json.loads(result.stdout)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise ProbeError(f"Unable to inspect media: {exc}") from exc
    streams = payload.get("streams", [])
    if not streams:
        raise ProbeError("Media has no video stream")
    stream = streams[0]
    try:
        n, d = parse_rational(stream["r_frame_rate"])
        frames = int(stream["nb_read_frames"])
        codec, width, height = stream["codec_name"], int(stream["width"]), int(stream["height"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbeError("Media has unsupported or incomplete video metadata") from exc
    if frames <= 0 or width <= 0 or height <= 0:
        raise ProbeError("Media has unsupported video dimensions or duration")
    return Asset(id=f"asset_{__import__('uuid').uuid4().hex}", path=str(source), name=source.name, codec=codec, width=width, height=height, fps=FrameRate(n, d), duration_frames=frames)
