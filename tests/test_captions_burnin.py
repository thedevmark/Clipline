# tests/test_captions_burnin.py
import unittest

from native.services.captions import escape_ass_path


class TestEscapeAssPath(unittest.TestCase):
    def test_windows_drive_colon_escaped_and_slashes_forward(self):
        # ffmpeg -vf needs the drive colon escaped and forward slashes.
        self.assertEqual(
            escape_ass_path(r"C:\Users\mark\AppData\clip.ass"),
            r"C\:/Users/mark/AppData/clip.ass",
        )

    def test_plain_relative_path_unchanged_slashes(self):
        self.assertEqual(escape_ass_path("out/clip.ass"), "out/clip.ass")


if __name__ == "__main__":
    unittest.main()
