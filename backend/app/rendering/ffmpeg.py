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
        os.environ.get("FFMPEG_BIN"),
        str(Path(__file__).resolve().parents[3] / ".tools" / "ffmpeg" / "ffmpeg"),
        shutil.which("ffmpeg"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _has_audio(path: str) -> bool:
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise RenderError("ffprobe is required to inspect source audio; install FFmpeg or configure FFPROBE_BIN")
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "json", path], check=True, capture_output=True, text=True)
        return bool(json.loads(result.stdout).get("streams"))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        raise RenderError(f"Unable to inspect source audio: {exc}") from exc


def _build_command(plan: RenderPlan, output: Path, revision: int, render_type: str) -> list[str]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RenderError("ffmpeg is required to render a timeline; install FFmpeg or configure FFMPEG_BIN")
    if not plan.layers or plan.duration_frames <= 0:
        raise RenderError("Render plan has no segments")
    args = [ffmpeg, "-y", "-v", "error"]
    filters: list[str] = []
    fps_text = f"{plan.fps[0]}/{plan.fps[1]}"
    duration = frames_to_ffmpeg_time(plan.duration_frames, plan.fps)
    input_index = 0
    video_inputs: list[tuple[int, ClipSegment]] = []
    audio_inputs: list[tuple[int, ClipSegment]] = []
    audio_capable: set[str] = set()
    for source in sorted({segment.source_path for layer in plan.layers for segment in layer.segments}):
        if _has_audio(source):
            audio_capable.add(source)
    for layer in plan.layers:
        for segment in layer.segments:
            args += ["-i", segment.source_path]
            video_inputs.append((input_index, segment))
            if segment.source_path in audio_capable:
                audio_inputs.append((input_index, segment))
            input_index += 1

    filters.append(f"color=c=black:s={plan.width}x{plan.height}:r={fps_text}:d={duration},format=rgba[canvas0]")
    current = "canvas0"
    for index, (source_index, segment) in enumerate(video_inputs):
        offset = frames_to_ffmpeg_time(segment.timeline_start_frame, plan.fps)
        filters.append(
            f"[{source_index}:v]trim=start_frame={segment.source_in_frame}:end_frame={segment.source_out_frame},"
            f"setpts=PTS-STARTPTS+{offset}/TB,scale={plan.width}:{plan.height}:force_original_aspect_ratio=decrease,"
            f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba[vclip{index}]"
        )
        filters.append(f"[{current}][vclip{index}]overlay=eof_action=pass:format=auto[canvas{index + 1}]")
        current = f"canvas{index + 1}"
    filters.append(f"[{current}]format=yuv420p[vout]")
    has_audio = bool(audio_inputs)
    if has_audio:
        audio_labels: list[str] = []
        for index, (source_index, segment) in enumerate(audio_inputs):
            start = frames_to_ffmpeg_time(segment.source_in_frame, plan.fps)
            end = frames_to_ffmpeg_time(segment.source_out_frame, plan.fps)
            delay = frames_to_ffmpeg_time(segment.timeline_start_frame, plan.fps)
            delay_ms = round(segment.timeline_start_frame * plan.fps[1] * 1000 / plan.fps[0])
            filters.append(f"[{source_index}:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,adelay={delay_ms}:all=1,apad,atrim=duration={duration}[ain{index}]")
            audio_labels.append(f"[ain{index}]")
        filters.append("".join(audio_labels) + f"amix=inputs={len(audio_labels)}:normalize=0:duration=longest,alimiter=limit=1,aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,apad,atrim=duration={duration}[aout]")
    args += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
    if has_audio:
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
