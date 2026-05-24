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
    num_font: ImageFont.FreeTypeFont,
    label_font: ImageFont.FreeTypeFont,
) -> int:
    """Render ``1 Ingest   2 Cut   3 Caption   4 Ship`` style row, centered."""
    gap_between_items = 96
    gap_num_label = 18

    # Measure first to compute centering.
    widths = []
    heights = []
    for num, label in steps:
        nb = draw.textbbox((0, 0), num, font=num_font)
        lb = draw.textbbox((0, 0), label, font=label_font)
        nw, nh = nb[2] - nb[0], nb[3] - nb[1]
        lw, lh = lb[2] - lb[0], lb[3] - lb[1]
        widths.append(nw + gap_num_label + lw)
        heights.append(max(nh, lh))

    total = sum(widths) + gap_between_items * (len(steps) - 1)
    x = CENTER_X - total // 2
    row_h = max(heights)

    for (num, label), w in zip(steps, widths):
        nb = draw.textbbox((0, 0), num, font=num_font)
        nh = nb[3] - nb[1]
        # Vertically align number and label to the same baseline by tweaking
        # the y of the smaller piece.
        draw.text((x, y + (row_h - nh) // 2), num, font=num_font, fill=ACCENT)
        nw = nb[2] - nb[0]
        lb = draw.textbbox((0, 0), label, font=label_font)
        lh = lb[3] - lb[1]
        draw.text(
            (x + nw + gap_num_label, y + (row_h - lh) // 2),
            label,
            font=label_font,
            fill=INK_MUTED,
        )
        x += w + gap_between_items
    return y + row_h


def _draw_feature_tags(
    draw: ImageDraw.ImageDraw,
    y: int,
    tags: list[str],
    font: ImageFont.FreeTypeFont,
) -> int:
    sep = "   ·   "
    pieces: list[tuple[str, tuple[int, int, int]]] = []
    for i, tag in enumerate(tags):
        if i > 0:
            # Separator in the same accent as the tags so it reads as one
            # unified row (alert-alert uses the same orange for both).
            pieces.append((sep, ACCENT))
        pieces.append((tag, ACCENT))
    total = 0
    heights = []
    for text, _ in pieces:
        bbox = draw.textbbox((0, 0), text, font=font)
        total += bbox[2] - bbox[0]
        heights.append(bbox[3] - bbox[1])
    x = CENTER_X - total // 2
    row_h = max(heights)
    for text, color in pieces:
        bbox = draw.textbbox((0, 0), text, font=font)
        h = bbox[3] - bbox[1]
        draw.text((x, y + (row_h - h) // 2), text, font=font, fill=color)
        x += bbox[2] - bbox[0]
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
    step_num_font = _font_semibold(50)
    step_label_font = _font_regular(48)
    tag_font = _font_regular(32)

    head_text = "Clipline"
    sub_line1 = "Turn livestream VOD moments into shortform clips."
    sub_line2 = "Batch crop, auto-caption, ship. One stream becomes a tray of shorts."
    steps = [("1", "Ingest"), ("2", "Cut"), ("3", "Caption"), ("4", "Ship")]
    tags = ["Twitch VOD import", "Auto captions", "Longform builder"]

    canvas_img = bg.convert("RGBA")
    draw = ImageDraw.Draw(canvas_img)

    # Measure each block's height to compute the centered stack.
    _, head_h = _measure_text(draw, head_text, head_font)
    _, sub1_h = _measure_text(draw, sub_line1, sub_font)
    _, sub2_h = _measure_text(draw, sub_line2, sub_font)
    _, step_h = _measure_text(draw, "Cy", step_num_font)
    _, tag_h = _measure_text(draw, "Cy", tag_font)

    gap_icon_head = 72
    gap_head_sub = 56
    gap_sub_lines = 14
    gap_sub_steps = 76
    gap_steps_tags = 48

    stack_h = (
        icon_size + gap_icon_head
        + head_h + gap_head_sub
        + sub1_h + gap_sub_lines + sub2_h + gap_sub_steps
        + step_h + gap_steps_tags
        + tag_h
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

    # Step row — Clipline's funnel.
    y = _draw_step_row(draw, y, steps, step_num_font, step_label_font) + gap_steps_tags

    # Feature tags
    _draw_feature_tags(draw, y, tags, tag_font)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    canvas_img.convert("RGB").save(OUTPUT_PATH, format="PNG", optimize=True)
    print(f"Wrote {OUTPUT_PATH} ({CANVAS[0]}x{CANVAS[1]})")


if __name__ == "__main__":
    sys.exit(main() or 0)
