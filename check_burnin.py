"""Manual render checkpoint for caption burn-in (dev helper, not shipped).

Usage:
    python check_burnin.py <input.mp4> <output.mp4>

Renders a 0–4s clip with two fake speakers at different colours/positions and
burns the captions in via the real build_clip_ass + escape_ass_path + ffmpeg
ass= filter path. Open the output and confirm: captions appear, timing matches,
SPEAKER_0 = yellow upper-left, SPEAKER_1 = blue lower-right, font legible.

This validates the parts that can't be checked headless (ffmpeg filtergraph
escaping, subtitle timing, font scale, per-speaker colour/position).
"""
import subprocess
import sys

from native.services.captions import build_clip_ass, escape_ass_path
from native.services.export_presets import (
    build_clip_export_args,
    format_preset_by_key,
    style_preset_by_key,
)
from native.services.tools import TOOLS


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    inp, out = sys.argv[1], sys.argv[2]
    words = [  # source-timeline seconds; two speakers
        {"text": "hello", "start": 0.2, "end": 0.7, "speaker": "SPEAKER_0", "enabled": True},
        {"text": "there", "start": 0.7, "end": 1.2, "speaker": "SPEAKER_0", "enabled": True},
        {"text": "reply", "start": 1.6, "end": 2.2, "speaker": "SPEAKER_1", "enabled": True},
    ]
    speakers = {
        "SPEAKER_0": {"color": "#FFD700", "pos": (0.30, 0.20)},  # yellow, upper-left
        "SPEAKER_1": {"color": "#00BFFF", "pos": (0.70, 0.80)},  # blue, lower-right
    }
    fmt = format_preset_by_key("shorts")  # 1080x1920
    ass = build_clip_ass(words, speakers, {}, 0, 4000, fmt.width, fmt.height)
    with open("check.ass", "w", encoding="utf-8") as fh:
        fh.write(ass)
    args = [
        TOOLS.ffmpeg, "-y", "-i", inp,
        *build_clip_export_args(
            0, 4000, style_preset_by_key("gameplay_focus"), fmt,
            subtitle_ass=escape_ass_path("check.ass"),
        ),
        out,
    ]
    print(" ".join(args))
    subprocess.run(args, check=True)
    print("Done ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
