"""Render docs/hero.png — the README banner.

Heavily inspired by sibling repo Alert! Alert!'s docs/hero.png (same 2560x1200
canvas, same center-stacked layout). Brand palette here comes from
``static/img/app-icon.svg`` so the banner stays visually consistent with the
app icon when both ship.

Run: ``python scripts/generate_hero.py``  — writes ``docs/hero.png``.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from PySide6.QtCore import QByteArray
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
SVG_PATH = ROOT / "static" / "img" / "app-icon.svg"
OUTPUT_PATH = ROOT / "docs" / "hero.png"

CANVAS = (2560, 1200)
CENTER_X = CANVAS[0] // 2

# Palette pulled from app-icon.svg gradient stops.
BG_INNER = (22, 35, 52)   # #162334
BG_OUTER = (13, 21, 33)   # #0D1521
ACCENT = (123, 213, 229)  # #7BD5E5  — primary teal
ACCENT_DEEP = (63, 169, 189)  # #3FA9BD
INK_BRIGHT = (245, 250, 255)  # #F5FAFF
INK_MUTED = (175, 192, 211)   # tuned for body text on dark navy
INK_DIM = (130, 148, 168)
GLASS_PURPLE = (176, 123, 255)  # #B07BFF — Twitch-purple pop

# Highlight glow positioned upper-right, mirroring alert-alert's hero.
GLOW_CENTER = (int(CANVAS[0] * 0.78), int(CANVAS[1] * 0.18))
GLOW_RADIUS = 1100


def render_icon_png(size: int) -> Image.Image:
    """Rasterize the Clipline SVG at the given size via QSvgRenderer.

    Matches the technique in ``scripts/generate_app_icon.py``; reused here
    so the hero icon and the app icon stay identical.
    """
    _ = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(QByteArray(SVG_PATH.read_bytes()))
    if not renderer.isValid():
        raise SystemExit(f"Failed to parse SVG: {SVG_PATH}")

    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()

    rgba = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            pixel = image.pixelColor(x, y)
            offset = (y * size + x) * 4
            rgba[offset] = pixel.red()
            rgba[offset + 1] = pixel.green()
            rgba[offset + 2] = pixel.blue()
            rgba[offset + 3] = pixel.alpha()
    return Image.frombytes("RGBA", (size, size), bytes(rgba))


def _find_font(candidates: Iterable[str], size: int) -> ImageFont.FreeTypeFont:
    """Walk a font candidate list and return the first that loads."""
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _font_bold(size: int) -> ImageFont.FreeTypeFont:
    return _find_font(
        [
            "segoeuib.ttf",       # Windows Segoe UI Bold
            "Inter-Bold.ttf",
            "Arial Bold.ttf",
            "arialbd.ttf",
            "DejaVuSans-Bold.ttf",
        ],
        size,
    )


def _font_regular(size: int) -> ImageFont.FreeTypeFont:
    return _find_font(
        [
            "segoeui.ttf",
            "Inter-Regular.ttf",
            "Arial.ttf",
            "arial.ttf",
            "DejaVuSans.ttf",
        ],
        size,
    )


def _font_semibold(size: int) -> ImageFont.FreeTypeFont:
    return _find_font(
        [
            "seguisb.ttf",        # Segoe UI Semibold
            "segoeuib.ttf",
            "Inter-SemiBold.ttf",
            "Arial Bold.ttf",
            "arialbd.ttf",
            "DejaVuSans-Bold.ttf",
        ],
        size,
    )


def _build_background() -> Image.Image:
    """Dark navy vertical gradient with a soft warm glow upper-right."""
    width, height = CANVAS
    base = Image.new("RGB", (width, height), BG_INNER)
    # Vertical gradient inner -> outer.
    grad = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / (height - 1)
        r = int(BG_INNER[0] + (BG_OUTER[0] - BG_INNER[0]) * t)
        g = int(BG_INNER[1] + (BG_OUTER[1] - BG_INNER[1]) * t)
        b = int(BG_INNER[2] + (BG_OUTER[2] - BG_INNER[2]) * t)
        grad.putpixel((0, y), (r, g, b))
    base = grad.resize((width, height))

    # Radial highlight: small bright teal core fading out, used as a soft
    # overlay so the canvas doesn't read flat. Mirrors the warm corner glow
    # in alert-alert's hero.
    glow = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(glow)
    cx, cy = GLOW_CENTER
    for r in range(GLOW_RADIUS, 0, -40):
        alpha = max(0, 60 - (r * 60 // GLOW_RADIUS))
        if alpha <= 0:
            continue
        color = (alpha, alpha + alpha // 6, alpha + alpha // 4)  # cool blue-teal
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(120))
    base = Image.eval(
        Image.blend(base.convert("RGB"), Image.blend(base, glow, 0.0), 0.0),
        lambda x: x,
    )
    # Combine: brighten base by the glow.
    out = Image.new("RGB", (width, height))
    base_px = base.load()
    glow_px = glow.load()
    for y in range(height):
        for x in range(width):
            br, bg, bb = base_px[x, y]
            gr, gg, gb = glow_px[x, y]
            out.putpixel(
                (x, y),
                (min(255, br + gr), min(255, bg + gg), min(255, bb + gb)),
            )
    return out


def _draw_centered(draw: ImageDraw.ImageDraw, y: int, text: str, font: ImageFont.FreeTypeFont, fill) -> int:
    """Draw ``text`` horizontally centered at ``y`` and return the next y position."""
    bbox = draw.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text((CENTER_X - text_w // 2, y), text, font=font, fill=fill)
    return y + text_h


def _draw_step_row(
    draw: ImageDraw.ImageDraw,
    y: int,
    steps: list[tuple[str, str]],
    font: ImageFont.FreeTypeFont,
) -> int:
    """Render ``1 Ingest   2 Cut   3 Caption   4 Ship``, centered, single baseline.

    Number and label use the **same font and size** so digit bboxes and word
    bboxes have matching ascent/descent — earlier versions used semibold-50
    for digits and regular-48 for labels and the numbers sat visibly above
    the words. Color is what carries the visual emphasis on the number now,
    not weight.
    """
    gap_between_items = 96
    gap_num_label = 18
    ascent, descent = font.getmetrics()
    row_h = ascent + descent

    widths: list[int] = []
    for num, label in steps:
        nw = draw.textlength(num, font=font)
        lw = draw.textlength(label, font=font)
        widths.append(int(nw + gap_num_label + lw))

    total = sum(widths) + gap_between_items * (len(steps) - 1)
    x = CENTER_X - total // 2

    for (num, label), w in zip(steps, widths):
        # ``anchor="lt"`` (the default) measures from the top of the line box,
        # so both glyphs share the same baseline at ``y + ascent``.
        nw = draw.textlength(num, font=font)
        draw.text((x, y), num, font=font, fill=ACCENT)
        draw.text((x + nw + gap_num_label, y), label, font=font, fill=INK_MUTED)
        x += w + gap_between_items
    return y + row_h


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def main() -> None:
    bg = _build_background()

    # Stack sizes tuned to fit comfortably inside 2560x1200 with the same
    # visual rhythm as alert-alert's hero (content fills the middle third,
    # generous breathing room top and bottom).
    icon_size = 220
    head_font = _font_bold(140)
    sub_font = _font_regular(46)
    step_font = _font_semibold(50)  # single font for numbers + labels

    head_text = "Clipline"
    sub_line1 = "Turn livestream VOD moments into shortform clips."
    sub_line2 = "Native preview, mark in/out, one-click longform stitch."
    steps = [("1", "Ingest"), ("2", "Cut"), ("3", "Caption"), ("4", "Ship")]

    canvas_img = bg.convert("RGBA")
    draw = ImageDraw.Draw(canvas_img)

    # Measure each block's height to compute the centered stack.
    _, head_h = _measure_text(draw, head_text, head_font)
    _, sub1_h = _measure_text(draw, sub_line1, sub_font)
    _, sub2_h = _measure_text(draw, sub_line2, sub_font)
    step_ascent, step_descent = step_font.getmetrics()
    step_h = step_ascent + step_descent

    gap_icon_head = 72
    gap_head_sub = 56
    gap_sub_lines = 14
    gap_sub_steps = 80

    stack_h = (
        icon_size + gap_icon_head
        + head_h + gap_head_sub
        + sub1_h + gap_sub_lines + sub2_h + gap_sub_steps
        + step_h
    )
    stack_top = (CANVAS[1] - stack_h) // 2

    # Icon
    icon = render_icon_png(icon_size)
    icon_y = stack_top
    canvas_img.paste(icon, (CENTER_X - icon_size // 2, icon_y), icon)

    # Headline
    y = icon_y + icon_size + gap_icon_head
    y = _draw_centered(draw, y, head_text, head_font, INK_BRIGHT) + gap_head_sub

    # Subtitle (2 lines)
    y = _draw_centered(draw, y, sub_line1, sub_font, INK_MUTED) + gap_sub_lines
    y = _draw_centered(draw, y, sub_line2, sub_font, INK_MUTED) + gap_sub_steps

    # Step row — Clipline's funnel. No tag row underneath — earlier draft
    # had three feature callouts that were just paraphrasing the step labels.
    _draw_step_row(draw, y, steps, step_font)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    canvas_img.convert("RGB").save(OUTPUT_PATH, format="PNG", optimize=True)
    print(f"Wrote {OUTPUT_PATH} ({CANVAS[0]}x{CANVAS[1]})")


if __name__ == "__main__":
    sys.exit(main() or 0)
