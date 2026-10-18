"""Record existing Clipline's export behavior without modifying the UI/state."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from native.services.export_presets import build_clip_export_args, format_preset_by_key, style_preset_by_key
from common import fingerprint, record_command, verify_export, write_json


def edge_markers(path: Path, width: int, height: int) -> dict:
    """Check known generated edge markers; meaningful only on our fixture."""
    result = subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-ss", "1", "-i", str(path),
                             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                            capture_output=True, timeout=60)
    if result.returncode or len(result.stdout) != width * height * 3:
        return {"verified": False, "error": "Could not extract frame"}
    def pixel(x, y):
        offset = (y * width + x) * 3
        return list(result.stdout[offset:offset + 3])
    left = [pixel(5, int(height * (index + 0.5) / 5)) for index in range(5)]
    right = [pixel(width - 6, int(height * (index + 0.5) / 5)) for index in range(5)]
    expected_left = [(255, 0, 0), (0, 255, 0), (0, 255, 255), (255, 0, 255), (255, 165, 0)]
    expected_right = [(0, 0, 255), (255, 165, 0), (0, 255, 0), (255, 0, 0), (0, 255, 255)]
    def matches(actual, expected):
        return all(abs(channel - target) <= 30 for rgb, wanted in zip(actual, expected)
                   for channel, target in zip(rgb, wanted))
    return {"verified": True, "left_rgb_samples": left, "right_rgb_samples": right,
            "left_marker_pattern_preserved": matches(left, expected_left),
            "right_marker_pattern_preserved": matches(right, expected_right),
            "scope": "known five-color fixture patterns only"}


def run(manifest_path: Path, output: Path) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("fixture_version") != 2:
        raise ValueError("Edge assessment requires fixture version 2; version 1 single-color markers were ambiguous")
    fixture = next(item for item in manifest["fixtures"] if item["id"] == "landscape_60")
    source = Path(fixture["path"])
    if fingerprint(source) != fixture["sha256"]:
        raise ValueError("Fixture fingerprint mismatch")
    export_owner = Path(__file__).resolve().parents[2] / "native/services/export_presets.py"
    result = {"application": "Clipline checkout under evaluation",
              "code": {"export_owner": str(export_owner), "sha256": fingerprint(export_owner)},
              "input": fixture, "measurement_class": "functional automation, uncontrolled contention",
              "cases": [], "human_editing_time": None, "caption_accuracy": None}
    result["original_edges"] = edge_markers(source, fixture["width"], fixture["height"])
    for key in ("landscape", "shorts"):
        case_root = output / key
        case_root.mkdir(parents=True, exist_ok=True)
        destination = case_root / "export.mp4"
        if destination.exists():
            raise FileExistsError(f"Use a fresh output directory: {destination}")
        preset = format_preset_by_key(key)
        args = build_clip_export_args(0, 6000, style_preset_by_key("gameplay_focus"), preset)
        command = ["ffmpeg", "-hide_banner", "-v", "error", "-n", "-i", str(source)] + args
        command += ["-threads", "2", str(destination)]
        export = record_command(command, case_root / "export-command.json", timeout=180)
        verification = verify_export(destination, case_root, width=preset.width, height=preset.height,
                                     fps="60", duration=6.0, audio=True)
        case = {"format": key, "editor_exit_code": export["exit_code"],
                "export_seconds": export["elapsed_seconds"], "verification": verification}
        if destination.is_file() and export["exit_code"] == 0:
            case["edge_markers"] = edge_markers(destination, preset.width, preset.height)
        result["cases"].append(case)
        write_json(output / "result.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.manifest, args.output)
    print(json.dumps({"cases": [{"format": case["format"],
          "media_passed": case["verification"]["passed"], "edges": case.get("edge_markers")}
          for case in report["cases"]]}, indent=2))
