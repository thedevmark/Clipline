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


import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from native.ui.caption_editor import CaptionEditor


class TestEditorResults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_result_speakers_seeds_colour_and_default_pos(self):
        words = [{"text": "hi", "start": 0.0, "end": 0.4, "speaker": "SPEAKER_0", "enabled": True}]
        ed = CaptionEditor(words)
        sp = ed.result_speakers()
        self.assertIn("SPEAKER_0", sp)
        self.assertEqual(sp["SPEAKER_0"]["pos"], (0.5, 0.85))
        self.assertTrue(sp["SPEAKER_0"]["color"].startswith("#"))
        self.assertEqual(ed.result_words()[0]["text"], "hi")

    def test_selected_speaker_count_auto_returns_none(self):
        words = [{"text": "x", "start": 0.0, "end": 0.1, "speaker": "SPEAKER_0", "enabled": True}]
        ed = CaptionEditor(words)
        self.assertIsNone(ed.selected_speaker_count())

    def test_result_overrides_empty_initially(self):
        words = [{"text": "x", "start": 0.0, "end": 0.1, "speaker": "SPEAKER_0", "enabled": True}]
        ed = CaptionEditor(words)
        self.assertEqual(ed.result_overrides(), {})

    def test_result_speakers_multi_speaker(self):
        words = [
            {"text": "hi", "start": 0.0, "end": 0.4, "speaker": "SPEAKER_0", "enabled": True},
            {"text": "there", "start": 0.5, "end": 0.9, "speaker": "SPEAKER_1", "enabled": True},
        ]
        ed = CaptionEditor(words)
        sp = ed.result_speakers()
        self.assertIn("SPEAKER_0", sp)
        self.assertIn("SPEAKER_1", sp)
        # Second speaker should get second palette colour.
        self.assertNotEqual(sp["SPEAKER_0"]["color"], sp["SPEAKER_1"]["color"])


if __name__ == "__main__":
    unittest.main()
