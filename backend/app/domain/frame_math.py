from __future__ import annotations

from fractions import Fraction
from decimal import Decimal, getcontext


def parse_rational(value: str) -> tuple[int, int]:
    if value in ("", "N/A", "0/0"):
        raise ValueError(f"Invalid rational FPS: {value!r}")
    if "/" in value:
        numerator, denominator = value.split("/", 1)
    else:
        numerator, denominator = value, "1"
    n, d = int(numerator), int(denominator)
    if n <= 0 or d <= 0:
        raise ValueError(f"Invalid rational FPS: {value!r}")
    fraction = Fraction(n, d)
    return fraction.numerator, fraction.denominator


def frames_to_seconds(frames: int, fps: tuple[int, int]) -> float:
    return frames * fps[1] / fps[0]


def seconds_to_frame(seconds: float, fps: tuple[int, int]) -> int:
    return max(0, int(round(seconds * fps[0] / fps[1])))


def frames_to_ffmpeg_time(frames: int, fps: tuple[int, int]) -> str:
    """Return a deterministic decimal seconds value for FFmpeg boundaries."""
    if frames < 0:
        raise ValueError("Frame count cannot be negative")
    getcontext().prec = 28
    seconds = (Decimal(frames) * Decimal(fps[1])) / Decimal(fps[0])
    text = format(seconds, ".12f").rstrip("0").rstrip(".")
    return text or "0"
