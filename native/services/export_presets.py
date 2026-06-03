"""Export presets for the Output / Inbox stages.

Each preset maps a human label to a tuple of ffmpeg args. Keep this file as
the single source of truth — UI widgets list presets by name from here, and
the worker pipeline reads the args without knowing what they mean.

Phase 4 ships the three streamer-focused recipes the legacy reel.js exposed
(Gameplay Focus / Facecam Top / Baked Text Punch). The facecam-stacking and
baked-text overlays in the legacy app were elaborate ffmpeg graphs that
referenced a per-channel saved facecam guide; until that data model lands
on the native branch the three recipes render distinct vertical crops with
different crop biases so the picker is meaningfully visible in exports.

Aspect-ratio presets (Output stage) are separate from style presets — they
control resolution + scale + pad, not the framing bias.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StylePreset:
    key: str
    label: str
    description: str
    # ffmpeg ``-vf`` value applied after ``-ss``/``-to`` bounding.
    video_filter: str


STYLE_PRESETS: list[StylePreset] = [
    StylePreset(
        key="gameplay_focus",
        label="Gameplay Focus",
        description="Vertical 9:16 crop centered on the action — the default for gameplay clips.",
        video_filter="crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos",
    ),
    StylePreset(
        key="facecam_top",
        label="Facecam Top",
        description="9:16 with the top half biased high — preserves facecam framing on the upper third.",
        video_filter="crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale=1080:1920:flags=lanczos,vignette=PI/8",
    ),
    StylePreset(
        key="baked_text",
        label="Baked Text Punch",
        description="Zoomed crop with a soft contrast bump — punchy framing for hook moments.",
        video_filter="crop=ih*9/20:ih*0.95:(iw-ih*9/20)/2:0,scale=1080:1920:flags=lanczos,eq=contrast=1.08:saturation=1.05",
    ),
]


@dataclass(frozen=True)
class FormatPreset:
    key: str
    label: str
    description: str
    # Drives the final scale/pad on the Output stage. Leave video_filter
    # alone — that's the StylePreset's responsibility.
    width: int
    height: int


FORMAT_PRESETS: list[FormatPreset] = [
    FormatPreset(
        key="shorts",
        label="Shorts / Reels",
        description="1080x1920 vertical. The default for TikTok, Reels, Shorts.",
        width=1080,
        height=1920,
    ),
    FormatPreset(
        key="portrait_feed",
        label="4:5 Feed",
        description="1080x1350 portrait — Instagram feed sweet spot.",
        width=1080,
        height=1350,
    ),
    FormatPreset(
        key="square",
        label="Square",
        description="1080x1080 — Twitter / X, LinkedIn feed.",
        width=1080,
        height=1080,
    ),
    FormatPreset(
        key="landscape",
        label="16:9 Landscape",
        description="1920x1080 — YouTube longform, source preservation.",
        width=1920,
        height=1080,
    ),
]


def style_preset_by_key(key: str) -> StylePreset:
    for preset in STYLE_PRESETS:
        if preset.key == key:
            return preset
    return STYLE_PRESETS[0]


def format_preset_by_key(key: str) -> FormatPreset:
    for preset in FORMAT_PRESETS:
        if preset.key == key:
            return preset
    return FORMAT_PRESETS[0]


def build_clip_export_args(
    start_ms: int,
    end_ms: int,
    style: StylePreset,
    fmt: FormatPreset | None = None,
    normalize_audio: bool = True,
    subtitle_ass: str | None = None,
) -> list[str]:
    """Compose ffmpeg args for a single-clip export.

    Returns the args between input and output paths so the worker can
    prepend ``-i <input>`` and append ``<output>`` itself.
    """
    vf = style.video_filter + (
        f",scale={fmt.width}:{fmt.height}:force_original_aspect_ratio=increase,"
        f"crop={fmt.width}:{fmt.height}"
        if fmt is not None
        else ""
    )
    if subtitle_ass:
        vf += f",ass='{subtitle_ass}'"
    args: list[str] = [
        "-ss", f"{start_ms / 1000:.3f}",
        "-to", f"{end_ms / 1000:.3f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-pix_fmt", "yuv420p",
    ]
    if normalize_audio:
        args.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])
    return args
