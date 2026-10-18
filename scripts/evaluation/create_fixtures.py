"""Create reproducible synthetic fixtures; these are not gaming/ASR evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

if __package__:
    from .common import fingerprint, probe, record_command, validate_probe, write_json
else:
    from common import fingerprint, probe, record_command, validate_probe, write_json


FIXTURES = [
    ("landscape_60", 1920, 1080, "60", 6.0, True),
    ("landscape_ntsc", 1920, 1080, "60000/1001", 6.0, True),
    ("portrait_no_audio", 1080, 1920, "30", 4.0, False),
    ("uhd_60", 3840, 2160, "60", 3.0, True),
]


def create(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    subtitles = root / "captions.srt"
    subtitle_text = ("1\n00:00:00,500 --> 00:00:01,500\nSynthetic fixture caption\n\n"
                     "2\n00:00:02,000 --> 00:00:03,000\nPause before this caption\n")
    if subtitles.exists() and subtitles.read_text(encoding="utf-8") != subtitle_text:
        raise ValueError("Existing subtitles differ; use a fresh fixture directory to preserve evidence")
    prior = {}
    if (root / "manifest.json").exists():
        prior_manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if prior_manifest.get("fixture_version") != 2:
            raise ValueError("Use a fresh directory for fixture version 2; previous evidence is preserved")
        prior = {item["id"]: item for item in prior_manifest["fixtures"]}
    manifest = {"schema_version": 1, "fixture_version": 2, "created_at": datetime.now(timezone.utc).isoformat(),
                "evidence_class": "synthetic fixture", "asr_evidence": False, "fixtures": []}
    for identity, width, height, fps, duration, audio in FIXTURES:
        output = root / f"{identity}.mp4"
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-f", "lavfi", "-i",
                   f"testsrc2=size={width}x{height}:rate={fps}"]
        if audio:
            command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        edge = max(12, width // 30)
        left_colors = ("red", "lime", "cyan", "magenta", "orange")
        right_colors = ("blue", "orange", "lime", "red", "cyan")
        filters = ",".join(
            f"drawbox=x={x}:y=ih*{index}/5:w={edge}:h=ih/5:color={color}:t=fill"
            for x, colors in (("0", left_colors), (f"iw-{edge}", right_colors))
            for index, color in enumerate(colors))
        filters += ",drawbox=x=iw/12:y=ih/12:w=iw/5:h=ih/4:color=orange:t=fill"
        command += ["-vf", filters, "-t", str(duration), "-c:v", "libx264", "-threads", "4",
                    "-preset", "ultrafast", "-crf", "23", "-pix_fmt", "yuv420p"]
        if audio:
            command += ["-c:a", "aac", "-b:a", "128k"]
        command += [str(output)]
        if output.exists():
            expected = prior.get(identity)
            if expected is None or fingerprint(output) != expected["sha256"]:
                raise ValueError(f"Existing fixture lacks matching provenance: {identity}")
        else:
            receipt = record_command(command, root / f"{identity}.generation.json")
            if receipt["exit_code"] != 0:
                raise RuntimeError(f"Fixture generation failed: {identity}; see receipt")
        metadata = probe(output)
        errors = validate_probe(metadata, width=width, height=height, fps=fps,
                                duration=duration, audio=audio)
        if errors:
            raise RuntimeError(f"Fixture invalid: {identity}: {errors}")
        write_json(root / f"{identity}.probe.json", metadata)
        manifest["fixtures"].append({"id": identity, "path": str(output.resolve()),
            "sha256": fingerprint(output), "width": width, "height": height, "fps": fps,
            "duration_seconds": duration, "audio": audio,
            "features": ["five-color left fiducial", "five-color right fiducial", "orange upper-left facecam region"],
            "speech": False})
        # Publish incremental availability for parallel functional trials.
        write_json(root / "manifest.json", manifest)
        print(f"Ready: {identity}", flush=True)
    if not subtitles.exists():
        subtitles.write_text(subtitle_text, encoding="utf-8")
    manifest["subtitles"] = {"path": str(subtitles.resolve()), "sha256": fingerprint(subtitles),
                             "evidence_class": "authored fixture; not a speech transcript"}
    write_json(root / "manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    create(args.output)
