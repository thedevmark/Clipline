"""Rasterize static/img/app-icon.svg into logo.png and favicon.ico.

The SVG is the single source of truth for the Clipline mark. This script
renders it at the sizes the app and OS need, using Qt's SVG renderer (already
a runtime dependency via PySide6) plus Pillow for the multi-resolution .ico.

Run:  python scripts/generate_app_icon.py
"""

from __future__ import annotations

import io
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
IMG_DIR = STATIC_DIR / "img"
SVG_PATH = IMG_DIR / "app-icon.svg"

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

# The mark's rounded-square sits at x/y 82..942 inside the 1024 canvas, so the
# raw SVG carries ~8% transparent margin on every side. That's fine for the
# README hero (logo.png keeps it) but makes the *taskbar* icon look shrunken —
# Windows adds its own padding around whatever we hand it. Crop the .ico frames
# to the artwork bounds so the tile is full-bleed and reads at taskbar size.
ARTWORK_BOX = QRectF(82, 82, 860, 860)


def render_png(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    renderer.render(painter)
    painter.end()

    buffer = io.BytesIO()
    width = image.width()
    rgba = bytearray(width * width * 4)
    for y in range(width):
        for x in range(width):
            pixel = image.pixelColor(x, y)
            offset = (y * width + x) * 4
            rgba[offset] = pixel.red()
            rgba[offset + 1] = pixel.green()
            rgba[offset + 2] = pixel.blue()
            rgba[offset + 3] = pixel.alpha()
    Image.frombytes("RGBA", (width, width), bytes(rgba)).save(buffer, format="PNG")
    return buffer.getvalue()


def main() -> None:
    # QSvgRenderer needs a QGuiApplication instance for font/paint init.
    app = QGuiApplication.instance() or QGuiApplication([])

    svg_data = QByteArray(SVG_PATH.read_bytes())
    renderer = QSvgRenderer(svg_data)
    if not renderer.isValid():
        raise SystemExit(f"Failed to parse SVG: {SVG_PATH}")

    IMG_DIR.mkdir(parents=True, exist_ok=True)

    # logo.png keeps the full canvas (with margin) for the README hero.
    (IMG_DIR / "logo.png").write_bytes(render_png(renderer, 1024))

    # Crop to the artwork for the OS icon frames — full-bleed reads better small.
    renderer.setViewBox(ARTWORK_BOX)

    # Render each ICO frame from the SVG at its native size so Windows has a
    # crisp image at every taskbar / Alt-Tab / file-explorer DPI bucket.
    # Save the largest frame first; rely on append_images (NOT the `sizes`
    # kwarg, which makes Pillow downscale a single frame and loses quality).
    frames = sorted(
        (
            Image.open(io.BytesIO(render_png(renderer, size))).convert("RGBA")
            for size in ICO_SIZES
        ),
        key=lambda im: im.size[0],
        reverse=True,
    )
    frames[0].save(
        STATIC_DIR / "favicon.ico",
        format="ICO",
        append_images=frames[1:],
    )

    print("Wrote static/img/logo.png and static/favicon.ico")
    del app


if __name__ == "__main__":
    main()
