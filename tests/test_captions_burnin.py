# tests/test_captions_burnin.py
import unittest

from native.services.captions import escape_ass_path, build_clip_ass


class TestEscapeAssPath(unittest.TestCase):
    def test_windows_drive_colon_escaped_and_slashes_forward(self):
        # ffmpeg -vf needs the drive colon escaped and forward slashes.
        self.assertEqual(
            escape_ass_path(r"C:\Users\mark\AppData\clip.ass"),
            r"C\:/Users/mark/AppData/clip.ass",
        )

    def test_plain_relative_path_unchanged_slashes(self):
        self.assertEqual(escape_ass_path("out/clip.ass"), "out/clip.ass")


class TestBuildClipAss(unittest.TestCase):
    def _words(self):
        return [
            {"text": "hello", "start": 1.0, "end": 1.4, "speaker": "SPEAKER_0", "enabled": True},
            {"text": "world", "start": 1.4, "end": 1.9, "speaker": "SPEAKER_0", "enabled": True},
            {"text": "later", "start": 9.0, "end": 9.4, "speaker": "SPEAKER_1", "enabled": True},
        ]

    def test_keeps_only_words_in_clip_and_shifts_to_zero(self):
        speakers = {"SPEAKER_0": {"color": "#FFD700", "pos": (0.5, 0.85)}}
        ass = build_clip_ass(self._words(), speakers, {}, 1000, 3000, 1080, 1920)
        self.assertIn("hello", ass)
        self.assertNotIn("later", ass)            # outside [1s,3s]
        self.assertIn("Dialogue: 0,0:00:00.00", ass)  # 1.0s shifted to 0

    def test_position_scaled_to_output_pixels(self):
        speakers = {"SPEAKER_0": {"color": "#FFD700", "pos": (0.5, 0.85)}}
        ass = build_clip_ass(self._words(), speakers, {}, 1000, 3000, 1080, 1920)
        self.assertIn(r"\pos(540,1632)", ass)     # 0.5*1080, 0.85*1920
        self.assertIn(r"\an5", ass)

    def test_line_override_replaces_speaker_pos(self):
        speakers = {"SPEAKER_0": {"color": "#FFD700", "pos": (0.5, 0.85)}}
        ass = build_clip_ass(self._words(), speakers, {1.0: (0.1, 0.1)}, 1000, 3000, 1080, 1920)
        self.assertIn(r"\pos(108,192)", ass)       # 0.1*1080, 0.1*1920
        self.assertNotIn(r"\pos(540,1632)", ass)


if __name__ == "__main__":
    unittest.main()
