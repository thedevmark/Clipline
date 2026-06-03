import unittest
from pathlib import Path

from native.workers import _build_ytdlp_args


class TestYtdlpArgs(unittest.TestCase):
    def test_no_no_part_and_filenames_restricted(self):
        args = _build_ytdlp_args("yt-dlp", None, "https://x/videos/1", Path("/tmp/out"))
        self.assertNotIn("--no-part", args)          # --no-part broke HLS writes
        self.assertIn("--restrict-filenames", args)
        self.assertEqual(args[-1], "https://x/videos/1")

    def test_paths_route_to_out_dir_and_template_is_relative(self):
        out = Path("/tmp/out")
        args = _build_ytdlp_args("yt-dlp", None, "u", out)
        self.assertEqual(args[args.index("-P") + 1], str(out))
        self.assertEqual(args[args.index("-o") + 1], "%(title).80s-%(id)s.%(ext)s")

    def test_ffmpeg_location_included_only_when_given(self):
        with_ff = _build_ytdlp_args("yt-dlp", "C:/ff/bin", "u", Path("o"))
        self.assertEqual(with_ff[with_ff.index("--ffmpeg-location") + 1], "C:/ff/bin")
        without = _build_ytdlp_args("yt-dlp", None, "u", Path("o"))
        self.assertNotIn("--ffmpeg-location", without)


if __name__ == "__main__":
    unittest.main()
