# tests/test_diarize.py
import unittest

from native.services.diarize import Segment, parse_segments


class TestParseSegments(unittest.TestCase):
    def test_parses_standard_lines(self):
        out = "0.000 -- 1.500 speaker_00\n1.500 -- 4.250 speaker_01\n"
        self.assertEqual(
            parse_segments(out),
            [Segment(0.0, 1.5, 0), Segment(1.5, 4.25, 1)],
        )

    def test_ignores_noise_and_blank_lines(self):
        out = "Duration : 10\n\n2.000 -- 3.000 speaker_02\nElapsed seconds: 1\n"
        self.assertEqual(parse_segments(out), [Segment(2.0, 3.0, 2)])

    def test_empty_output_is_empty_list(self):
        self.assertEqual(parse_segments(""), [])


from native.services.diarize import assign_speakers


class TestAssignSpeakers(unittest.TestCase):
    def _w(self, text, start, end):
        return {"text": text, "start": start, "end": end}

    def test_assigns_by_word_midpoint(self):
        words = [self._w("a", 0.0, 0.4), self._w("b", 2.0, 2.4)]
        segs = [Segment(0.0, 1.0, 0), Segment(1.0, 3.0, 1)]
        out = assign_speakers(words, segs)
        self.assertEqual([w["speaker"] for w in out], ["SPEAKER_0", "SPEAKER_1"])

    def test_word_outside_all_segments_inherits_previous(self):
        words = [self._w("a", 0.0, 0.4), self._w("b", 9.0, 9.4)]
        segs = [Segment(0.0, 1.0, 2)]
        out = assign_speakers(words, segs)
        self.assertEqual([w["speaker"] for w in out], ["SPEAKER_2", "SPEAKER_2"])

    def test_no_segments_defaults_speaker_0(self):
        words = [self._w("a", 0.0, 0.4)]
        out = assign_speakers(words, [])
        self.assertEqual(out[0]["speaker"], "SPEAKER_0")

    def test_does_not_mutate_input(self):
        words = [self._w("a", 0.0, 0.4)]
        assign_speakers(words, [Segment(0.0, 1.0, 1)])
        self.assertNotIn("speaker", words[0])


if __name__ == "__main__":
    unittest.main()
