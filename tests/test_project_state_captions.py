# tests/test_project_state_captions.py
import unittest

from native.ui.project_state import ProjectState


class TestCaptionState(unittest.TestCase):
    def setUp(self):
        self.state = ProjectState()

    def test_defaults_empty(self):
        self.assertEqual(self.state.caption_words, [])
        self.assertEqual(self.state.speakers, {})
        self.assertEqual(self.state.line_overrides, {})
        self.assertFalse(self.state.burn_captions)

    def test_set_captions_copies_in_and_emits(self):
        seen = {}
        self.state.captions_changed.connect(lambda: seen.setdefault("hit", True))
        words = [{"text": "hi", "start": 0.0, "end": 0.5, "speaker": "SPEAKER_0", "enabled": True}]
        speakers = {"SPEAKER_0": {"color": "#FFD700", "pos": (0.5, 0.85)}}
        self.state.set_captions(words, speakers, {1.0: (0.2, 0.1)}, burn_in=True)
        self.assertEqual(self.state.caption_words[0]["text"], "hi")
        self.assertEqual(self.state.speakers["SPEAKER_0"]["color"], "#FFD700")
        self.assertEqual(self.state.line_overrides[1.0], (0.2, 0.1))
        self.assertTrue(self.state.burn_captions)
        self.assertTrue(seen.get("hit"))

    def test_set_captions_is_defensive_copy(self):
        words = [{"text": "hi", "start": 0.0, "end": 0.5}]
        self.state.set_captions(words, {}, {}, burn_in=False)
        words[0]["text"] = "mutated"
        self.assertEqual(self.state.caption_words[0]["text"], "hi")


if __name__ == "__main__":
    unittest.main()
