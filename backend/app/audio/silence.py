from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from app.domain.frame_math import frames_to_seconds
from app.domain.models import Asset, FrameRate, ValidationError
from app.media.probe import find_ffprobe


class SilenceAnalysisError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SilenceInterval:
    start_frame: int
    end_frame: int

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame


@dataclass(frozen=True)
class AssetSilenceAnalysis:
    asset_id: str
    threshold_db: float
    minimum_silence_ms: int
    silences: tuple[SilenceInterval, ...]


def validate_parameters(minimum_silence_ms: int, noise_threshold_db: float, keep_padding_ms: int = 0) -> None:
    if not isinstance(minimum_silence_ms, int) or minimum_silence_ms <= 0 or minimum_silence_ms > 3_600_000:
        raise SilenceAnalysisError("INVALID_ARGUMENT", "minimum_silence_ms must be between 1 and 3600000")
    if not isinstance(noise_threshold_db, (int, float)) or noise_threshold_db < -100 or noise_threshold_db > 0:
        raise SilenceAnalysisError("INVALID_ARGUMENT", "noise_threshold_db must be between -100 and 0 dB")
    if not isinstance(keep_padding_ms, int) or keep_padding_ms < 0 or keep_padding_ms > 3_600_000:
        raise SilenceAnalysisError("INVALID_ARGUMENT", "keep_padding_ms must be between 0 and 3600000")


def _round_fraction(value: Fraction) -> int:
    quotient, remainder = divmod(value.numerator, value.denominator)
    return quotient + (1 if remainder * 2 >= value.denominator else 0)


def seconds_text_to_frame(value: str, fps: tuple[int, int]) -> int:
    try:
        seconds = Fraction(Decimal(value))
    except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
        raise SilenceAnalysisError("FFMPEG_ANALYSIS_FAILED", f"Invalid FFmpeg timestamp: {value!r}") from exc
    return max(0, _round_fraction(seconds * Fraction(fps[0], fps[1])))


def milliseconds_to_frames(milliseconds: int, fps: tuple[int, int]) -> int:
    if milliseconds < 0:
        raise SilenceAnalysisError("INVALID_ARGUMENT", "Milliseconds cannot be negative")
    return _round_fraction(Fraction(milliseconds, 1000) * Fraction(fps[0], fps[1]))


def parse_silencedetect(stderr: str, fps: tuple[int, int], duration_frames: int) -> tuple[SilenceInterval, ...]:
    starts: list[int] = []
    intervals: list[SilenceInterval] = []
    start_pattern = re.compile(r"silence_start:\s*([^\s]+)")
    end_pattern = re.compile(r"silence_end:\s*([^\s]+)")
    for line in stderr.splitlines():
        start = start_pattern.search(line)
        if start:
            starts.append(seconds_text_to_frame(start.group(1), fps))
        end = end_pattern.search(line)
        if end:
            end_frame = seconds_text_to_frame(end.group(1), fps)
            if not starts:
                raise SilenceAnalysisError("FFMPEG_ANALYSIS_FAILED", "FFmpeg returned silence_end without silence_start")
            start_frame = starts.pop(0)
            if end_frame > start_frame:
                intervals.append(SilenceInterval(start_frame, min(end_frame, duration_frames)))
    for start_frame in starts:
        if duration_frames > start_frame:
            intervals.append(SilenceInterval(start_frame, duration_frames))
    return tuple(sorted((item for item in intervals if item.end_frame > item.start_frame), key=lambda item: (item.start_frame, item.end_frame)))


def _has_audio(path: str) -> bool:
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise SilenceAnalysisError("FFPROBE_UNAVAILABLE", "ffprobe is required for silence analysis")
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "json", path], check=True, capture_output=True, text=True, timeout=30)
        return bool(json.loads(result.stdout).get("streams"))
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise SilenceAnalysisError("FFPROBE_FAILED", "Unable to inspect source audio") from exc


def analyze_asset(asset: Asset, minimum_silence_ms: int = 700, noise_threshold_db: float = -35) -> AssetSilenceAnalysis:
    validate_parameters(minimum_silence_ms, noise_threshold_db)
    path = Path(asset.path)
    if not path.is_file():
        raise SilenceAnalysisError("MEDIA_NOT_FOUND", f"Source media for asset {asset.id} is unavailable")
    if not _has_audio(str(path)):
        raise SilenceAnalysisError("NO_AUDIO_STREAM", f"Asset {asset.id} has no audio stream")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SilenceAnalysisError("FFMPEG_UNAVAILABLE", "ffmpeg is required for silence analysis")
    noise = f"{float(noise_threshold_db):g}dB"
    duration = frames_to_seconds(asset.duration_frames, asset.fps.as_tuple())
    try:
        result = subprocess.run([
            ffmpeg, "-hide_banner", "-v", "info", "-i", str(path),
            "-af", f"silencedetect=noise={noise}:d={minimum_silence_ms / 1000:g}",
            "-f", "null", "-",
        ], check=False, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SilenceAnalysisError("FFMPEG_ANALYSIS_FAILED", "FFmpeg silence analysis failed") from exc
    if result.returncode != 0:
        raise SilenceAnalysisError("FFMPEG_ANALYSIS_FAILED", "FFmpeg silence analysis failed")
    return AssetSilenceAnalysis(asset.id, float(noise_threshold_db), minimum_silence_ms,
                                parse_silencedetect(result.stderr, asset.fps.as_tuple(), asset.duration_frames))


def silence_dict(analysis: AssetSilenceAnalysis) -> dict:
    return {"asset_id": analysis.asset_id, "threshold_db": analysis.threshold_db,
            "minimum_silence_ms": analysis.minimum_silence_ms,
            "silences": [{"start_frame": item.start_frame, "end_frame": item.end_frame,
                          "duration_frames": item.duration_frames} for item in analysis.silences]}
