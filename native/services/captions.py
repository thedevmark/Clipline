"""Caption pipeline for the native shell — transcription + ASS/SRT generation.

The style presets, word→line grouping, and ASS builder are carried over verbatim
from the legacy Flask ``captions.py`` (same subtitle output the web build
shipped); the Flask routes, ``import app`` shim, and pyannote diarization are
dropped (MVP is single-speaker).

The transcription backend is **whisper.cpp**, not faster-whisper: the audience
can't ``pip install``, so the engine is a downloaded native binary + model
(see ``whisper_cpp.py``). This module just delegates and keeps the subtitle
formatting. ``available()`` reflects whether that engine is provisioned.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from native.services import whisper_cpp

DEFAULT_SPEAKER_COLORS = ["#FFD700", "#00BFFF", "#FF6B6B", "#7CFC00", "#FF69B4"]
DEFAULT_CAPTION_STYLE = {
    "preset": "pathos_clean",
    "font_family": "Arial",
    "font_scale": 1.0,
    "max_words": 6,
    "margin_v": 120,
    "outline": 4,
    "shadow": 2,
    "background_opacity": 50,
    "all_caps": False,
    "karaoke": True,
    "bold": True,
}
CAPTION_STYLE_PRESETS = {
    "pathos_clean": dict(DEFAULT_CAPTION_STYLE),
    "broadcast_bold": {
        "font_family": "Impact", "font_scale": 1.16, "max_words": 5, "margin_v": 136,
        "outline": 5, "shadow": 1, "background_opacity": 36, "all_caps": True,
        "karaoke": True, "bold": True,
    },
    "minimal_clean": {
        "font_family": "Tahoma", "font_scale": 0.9, "max_words": 7, "margin_v": 108,
        "outline": 2, "shadow": 1, "background_opacity": 18, "all_caps": False,
        "karaoke": False, "bold": False,
    },
}


def available() -> bool:
    """True if the whisper.cpp engine (binary + model) is provisioned."""
    return whisper_cpp.is_ready()


def normalize_caption_style(style: Optional[dict] = None) -> dict:
    resolved = dict(DEFAULT_CAPTION_STYLE)
    if isinstance(style, dict):
        preset_name = str(style.get("preset", resolved["preset"])).strip() or resolved["preset"]
        if preset_name in CAPTION_STYLE_PRESETS:
            resolved.update(CAPTION_STYLE_PRESETS[preset_name])
        resolved.update({k: v for k, v in style.items() if v is not None})
    resolved["preset"] = str(resolved.get("preset", "pathos_clean")).strip() or "pathos_clean"
    resolved["font_family"] = str(resolved.get("font_family", "Arial")).strip() or "Arial"
    resolved["font_scale"] = max(0.65, min(1.8, float(resolved.get("font_scale", 1.0) or 1.0)))
    resolved["max_words"] = max(2, min(12, int(resolved.get("max_words", 6) or 6)))
    resolved["margin_v"] = max(40, min(260, int(resolved.get("margin_v", 120) or 120)))
    resolved["outline"] = max(0, min(8, float(resolved.get("outline", 4) or 0)))
    resolved["shadow"] = max(0, min(8, float(resolved.get("shadow", 2) or 0)))
    resolved["background_opacity"] = max(0, min(100, int(resolved.get("background_opacity", 50) or 0)))
    resolved["all_caps"] = bool(resolved.get("all_caps", False))
    resolved["karaoke"] = bool(resolved.get("karaoke", True))
    resolved["bold"] = bool(resolved.get("bold", True))
    return resolved


def transcribe_words(
    media_path: Path,
    ffmpeg: str,
    on_progress: Optional[Callable[[str], None]] = None,
) -> list[dict]:
    """Transcribe to word-level dicts via whisper.cpp.

    Returns ``[{text, start, end, speaker, enabled}, ...]``.
    """
    return whisper_cpp.transcribe(Path(media_path), ffmpeg, on_progress=on_progress)


def group_words_into_lines(words: list[dict], max_words: int = 6) -> list[dict]:
    lines: list[dict] = []
    current_line: list[dict] = []

    def flush() -> None:
        if not current_line:
            return
        lines.append({
            "words": list(current_line),
            "speaker": current_line[0].get("speaker", "SPEAKER_0"),
            "start": current_line[0]["start"],
            "end": current_line[-1]["end"],
            "enabled": bool(current_line[0].get("enabled", True)),
        })
        current_line.clear()

    for word in words:
        if current_line:
            if (bool(word.get("enabled", True)) != bool(current_line[0].get("enabled", True))
                    or word.get("speaker", "SPEAKER_0") != current_line[0].get("speaker", "SPEAKER_0")):
                flush()
        current_line.append(word)
        if len(current_line) >= max_words or word["text"].rstrip().endswith((".", "!", "?", ",")):
            flush()
    flush()
    return lines


def format_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int((seconds % 1) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def generate_ass_subtitles(words, speakers, play_res_x=1080, play_res_y=1920, style=None) -> str:
    cfg = normalize_caption_style(style)
    ass = (
        "[Script Info]\nTitle: Clipline Captions\nScriptType: v4.00+\n"
        f"PlayResX: {play_res_x}\nPlayResY: {play_res_y}\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    )
    for speaker_id, data in speakers.items():
        hex_color = data.get("color", "#FFFFFF").lstrip("#")
        if len(hex_color) == 6:
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        else:
            r, g, b = 255, 255, 255
        ass_color = f"&H00{b:02X}{g:02X}{r:02X}&"
        alpha = int(round((100 - cfg["background_opacity"]) * 255 / 100))
        back_color = f"&H{alpha:02X}000000&"
        style_name = speaker_id.replace(" ", "_")
        font_size = int(72 * cfg["font_scale"] * play_res_x / 1080)
        bold_flag = -1 if cfg["bold"] else 0
        ass += (
            f"Style: {style_name},{cfg['font_family']},{font_size},{ass_color},&H000000FF,"
            f"&H00000000&,{back_color},{bold_flag},0,0,0,100,100,0,0,1,"
            f"{cfg['outline']},{cfg['shadow']},2,30,30,{cfg['margin_v']},1\n"
        )

    ass += "\n[Events]\n"
    ass += "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    for line in group_words_into_lines(words, max_words=cfg["max_words"]):
        if not line.get("enabled", True):
            continue
        style_name = line["speaker"].replace(" ", "_")
        start, end = format_ass_time(line["start"]), format_ass_time(line["end"])
        if cfg["karaoke"]:
            parts = []
            for w in line["words"]:
                dur_cs = max(1, int((w["end"] - w["start"]) * 100))
                txt = w["text"].upper() if cfg["all_caps"] else w["text"]
                parts.append(f"{{\\kf{dur_cs}}}{txt}")
            text = " ".join(parts)
        else:
            text = " ".join(
                (w["text"].upper() if cfg["all_caps"] else w["text"]) for w in line["words"]
            )
        ass += f"Dialogue: 0,{start},{end},{style_name},,0,0,0,,{text}\n"
    return ass


def _srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def generate_srt(words: list[dict], max_words: int = 6) -> str:
    out = []
    for i, line in enumerate(group_words_into_lines(words, max_words=max_words), start=1):
        if not line.get("enabled", True):
            continue
        text = " ".join(w["text"] for w in line["words"])
        out.append(f"{i}\n{_srt_time(line['start'])} --> {_srt_time(line['end'])}\n{text}\n")
    return "\n".join(out)
