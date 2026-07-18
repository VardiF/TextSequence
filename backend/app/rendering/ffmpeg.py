from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.domain.frame_math import frames_to_ffmpeg_time
from app.media.probe import find_ffprobe
from app.rendering.plan import ClipSegment, GapSegment, RenderPlan


class RenderError(ValueError):
    pass


@dataclass(frozen=True)
class RenderResult:
    path: str
    render_type: str
    revision: int
    duration_frames: int


def find_ffmpeg() -> Optional[str]:
    candidates = [
        shutil.which("ffmpeg"),
        os.environ.get("FFMPEG_BIN"),
        str(Path(__file__).resolve().parents[3] / ".tools" / "ffmpeg" / "ffmpeg"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _has_audio(path: str) -> bool:
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise RenderError("ffprobe is required to inspect source audio")
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "json", path], check=True, capture_output=True, text=True)
        return bool(json.loads(result.stdout).get("streams"))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RenderError(f"Unable to inspect source audio: {exc}") from exc


def _build_command(plan: RenderPlan, output: Path, revision: int, render_type: str) -> list[str]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RenderError("ffmpeg is required to render a timeline")
    if not plan.segments:
        raise RenderError("Render plan has no segments")
    audio = _has_audio(next(segment.source_path for segment in plan.segments if isinstance(segment, ClipSegment)))
    args = [ffmpeg, "-y", "-v", "error"]
    filters: list[str] = []
    concat_inputs: list[str] = []
    input_index = 0
    fps_text = f"{plan.fps[0]}/{plan.fps[1]}"
    for segment_index, segment in enumerate(plan.segments):
        if isinstance(segment, ClipSegment):
            args += ["-i", segment.source_path]
            video_index = input_index
            input_index += 1
            filters.append(f"[{video_index}:v]trim=start_frame={segment.source_in_frame}:end_frame={segment.source_out_frame},setpts=PTS-STARTPTS,format=yuv420p[v{segment_index}]")
            if audio:
                start = frames_to_ffmpeg_time(segment.source_in_frame, plan.fps)
                end = frames_to_ffmpeg_time(segment.source_out_frame, plan.fps)
                filters.append(f"[{video_index}:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[a{segment_index}]")
        else:
            duration = frames_to_ffmpeg_time(segment.duration_frames, plan.fps)
            args += ["-f", "lavfi", "-i", f"color=c=black:s={plan.width}x{plan.height}:r={fps_text}:d={duration}"]
            filters.append(f"[{input_index}:v]setpts=PTS-STARTPTS,format=yuv420p[v{segment_index}]")
            input_index += 1
            if audio:
                args += ["-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=48000:d={duration}"]
                filters.append(f"[{input_index}:a]asetpts=PTS-STARTPTS[a{segment_index}]")
                input_index += 1
        concat_inputs.append(f"[v{segment_index}]" + (f"[a{segment_index}]" if audio else ""))
    concat = "".join(concat_inputs)
    if audio:
        filters.append(f"{concat}concat=n={len(plan.segments)}:v=1:a=1[vout][aout]")
    else:
        filters.append(f"{concat}concat=n={len(plan.segments)}:v=1:a=0[vout]")
    args += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if audio:
        args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
    else:
        args += ["-an"]
    args += ["-c:v", "libx264", "-preset", "ultrafast" if render_type == "preview" else "medium", "-pix_fmt", "yuv420p", "-r", fps_text, "-movflags", "+faststart", str(output)]
    return args


def render_plan(plan: RenderPlan, output: Path, revision: int, render_type: str) -> RenderResult:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = _build_command(plan, output, revision, render_type)
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired as exc:
        raise RenderError("FFmpeg rendering timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "FFmpeg returned an error").strip()
        raise RenderError(f"FFmpeg rendering failed: {detail[-2000:]}") from exc
    if not output.is_file() or output.stat().st_size == 0:
        raise RenderError("FFmpeg completed without producing an output file")
    return RenderResult(str(output), render_type, revision, plan.duration_frames)
