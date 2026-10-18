"""Small, independent evidence helpers for the editor evaluation."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import time
from fractions import Fraction
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_command(argv: list[str], receipt: Path, *, timeout: int = 180) -> dict:
    started = time.perf_counter()
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
        value = {"argv": argv, "started": True, "exit_code": result.returncode, "stdout": result.stdout,
                 "stderr": result.stderr, "timed_out": False}
    except subprocess.TimeoutExpired as error:
        def decode(value: object) -> str:
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
        value = {"argv": argv, "started": True, "exit_code": None, "stdout": decode(error.stdout),
                 "stderr": decode(error.stderr), "timed_out": True}
    except OSError as error:
        value = {"argv": argv, "started": False, "exit_code": None, "stdout": "",
                 "stderr": str(error), "timed_out": False}
    value["elapsed_seconds"] = time.perf_counter() - started
    value["measurement"] = "automation wall time; resource contention not controlled"
    write_json(receipt, value)
    return value


def verify_export(path: Path, receipt_root: Path, *, width: int, height: int,
                  fps: str, duration: float, audio: bool) -> dict:
    """A zero editor exit alone cannot establish a valid exported file."""
    if not path.is_file():
        result = {"path": str(path), "passed": False, "errors": ["Output file missing"]}
        write_json(receipt_root / "verification.json", result)
        return result
    try:
        metadata = probe(path, count_frames=True)
        errors = validate_probe(metadata, width=width, height=height, fps=fps,
                                duration=duration, audio=audio)
    except (ValueError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        metadata = None
        errors = [str(error)]
    decode = record_command(["ffmpeg", "-hide_banner", "-v", "error", "-xerror",
                             "-threads", "2", "-i", str(path), "-f", "null", "-"],
                            receipt_root / "decode.json", timeout=180)
    if decode["exit_code"] != 0:
        errors.append("Independent full decode failed")
    result = {"path": str(path.resolve()), "sha256": fingerprint(path), "probe": metadata,
              "passed": not errors, "errors": errors,
              "scope": "stream dimensions, square pixels, average rate, duration, decoded frame count and full decode; not uniform frame cadence, visual parity or A/V sync proof"}
    write_json(receipt_root / "verification.json", result)
    return result


def probe(path: Path, *, ffprobe: str = "ffprobe", count_frames: bool = False) -> dict:
    options = ["-count_frames"] if count_frames else []
    result = subprocess.run([ffprobe, "-v", "error"] + options + ["-show_streams", "-show_format",
                             "-of", "json", str(path)], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    if result.returncode:
        raise ValueError(f"Media probe failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def validate_probe(metadata: dict, *, width: int, height: int, fps: str,
                   duration: float, audio: bool) -> list[str]:
    """Validate independent media facts, returning all discrepancies."""
    errors: list[str] = []
    videos = [item for item in metadata.get("streams", []) if item.get("codec_type") == "video"]
    if len(videos) != 1:
        errors.append(f"Expected one video stream, found {len(videos)}")
    else:
        video = videos[0]
        if (video.get("width"), video.get("height")) != (width, height):
            errors.append("Video dimensions differ")
        try:
            sample_aspect = Fraction(video["sample_aspect_ratio"].replace(":", "/"))
            if sample_aspect != 1:
                errors.append("Expected square pixels")
        except (KeyError, ValueError, ZeroDivisionError, TypeError, AttributeError):
            errors.append("Sample aspect ratio unavailable")
        actual_rate = expected_rate = Fraction(0)
        try:
            expected_rate = Fraction(fps)
            actual_rate = Fraction(video.get("avg_frame_rate", "0/1"))
            if expected_rate <= 0 or actual_rate != expected_rate:
                errors.append(f"Frame rate differs: {actual_rate} != {expected_rate}")
            tolerance = max(0.05, 2 / float(expected_rate)) if expected_rate > 0 else 0.05
        except (ValueError, ZeroDivisionError, TypeError):
            errors.append("Invalid frame rate")
            tolerance = 0.05
        try:
            if video.get("duration") is not None:
                actual_duration = float(video["duration"])
            elif video.get("duration_ts") is not None and video.get("time_base"):
                actual_duration = float(Fraction(video["duration_ts"]) * Fraction(video["time_base"]))
            elif video.get("nb_read_frames") is not None and actual_rate > 0:
                actual_duration = int(video["nb_read_frames"]) / float(actual_rate)
            else:
                raise ValueError("Video stream duration unavailable")
            if not math.isfinite(actual_duration) or actual_duration <= 0:
                errors.append("Missing or invalid duration")
            elif abs(actual_duration - duration) > tolerance:
                errors.append("Duration differs by more than two output frames / 50 ms")
        except (KeyError, ValueError, TypeError):
            errors.append("Missing or invalid duration")
        frame_count = video.get("nb_read_frames")
        if frame_count is not None and expected_rate > 0:
            try:
                expected_frames = round(duration * float(expected_rate))
                if abs(int(frame_count) - expected_frames) > 1:
                    errors.append("Decoded video frame count differs")
            except (ValueError, TypeError):
                errors.append("Invalid decoded frame count")
    has_audio = any(item.get("codec_type") == "audio" for item in metadata.get("streams", []))
    if has_audio != audio:
        errors.append("Audio presence differs")
    return errors
