"""Landscape output must retain source edges for every portrait style."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from native.services.export_presets import STYLE_PRESETS, FormatPreset, build_clip_export_args


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg/ffprobe required")
class LandscapeSourceIntegrityTests(unittest.TestCase):
    def test_anamorphic_source_keeps_display_aspect_with_letterboxing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "anamorphic.mp4", root / "landscape.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=gray:s=320x180:r=30:d=0.2,setsar=2/1", "-c:v", "libx264",
                "-threads", "2", "-pix_fmt", "yuv420p", str(source)],
                check=True, capture_output=True, timeout=30)
            canvas = FormatPreset("integrity", "Landscape", "Small render regression", 160, 90)
            args = build_clip_export_args(0, 200, STYLE_PRESETS[0], canvas, normalize_audio=False)
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(source)] + args +
                           ["-threads", "2", str(output)], check=True, capture_output=True, timeout=30)
            frame = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output),
                "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], timeout=30)
            self.assertEqual(len(frame), 160 * 90 * 3)
            occupied = [y for y in range(90) if frame[(y * 160 + 80) * 3] > 80]
            # A 32:9 display frame fits into 16:9 at half the canvas height.
            self.assertTrue(43 <= len(occupied) <= 47, occupied)
            self.assertLess(frame[80 * 3], 15)
            self.assertLess(frame[(89 * 160 + 80) * 3], 15)

    def test_original_edges_and_square_pixels_survive_all_styles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                "color=gray:s=320x180:r=30:d=0.2,drawbox=x=0:y=0:w=20:h=ih:color=red:t=fill,"
                "drawbox=x=iw-20:y=0:w=20:h=ih:color=blue:t=fill",
                "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", str(source)],
                check=True, capture_output=True, timeout=30)
            canvas = FormatPreset("integrity", "Landscape", "Small render regression", 160, 90)
            for style in STYLE_PRESETS:
                with self.subTest(style=style.key):
                    output = root / f"{style.key}.mp4"
                    args = build_clip_export_args(0, 200, style, canvas, normalize_audio=False)
                    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source)] + args +
                                   ["-threads", "2", str(output)], check=True,
                                   capture_output=True, timeout=30)
                    metadata = json.loads(subprocess.check_output(["ffprobe", "-v", "error",
                        "-select_streams", "v:0", "-show_entries", "stream=width,height,sample_aspect_ratio",
                        "-of", "json", str(output)], timeout=30))
                    stream = metadata["streams"][0]
                    self.assertEqual((stream["width"], stream["height"]), (160, 90))
                    frame = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output),
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], timeout=30)
                    self.assertEqual(len(frame), 160 * 90 * 3)
                    def pixel(x):
                        index = (45 * 160 + x) * 3
                        return frame[index:index + 3]
                    left, right = pixel(4), pixel(155)
                    self.assertGreater(left[0], left[1] + 80, "Original red left edge was cropped")
                    self.assertGreater(right[2], right[0] + 80, "Original blue right edge was cropped")
                    self.assertEqual(stream["sample_aspect_ratio"], "1:1")


if __name__ == "__main__":
    unittest.main()
