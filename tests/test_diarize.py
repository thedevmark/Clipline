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


if __name__ == "__main__":
    unittest.main()
