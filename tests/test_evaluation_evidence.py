"""Evidence validation must reject plausible broken editor exports."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.evaluation.common import record_command, validate_probe
from scripts.evaluation.create_fixtures import create


def video_probe(width=1920, height=1080, fps="60000/1001", duration="6.006", audio=True):
    streams = [{"codec_type": "video", "width": width, "height": height,
                "avg_frame_rate": fps, "duration": duration, "sample_aspect_ratio": "1:1"}]
    if audio:
        streams.append({"codec_type": "audio"})
    return {"streams": streams, "format": {"duration": duration}}


class ExportEvidenceTests(unittest.TestCase):
    def check(self, metadata):
        return validate_probe(metadata, width=1920, height=1080, fps="60000/1001",
                              duration=6.006, audio=True)

    def test_rational_rate_is_exact(self):
        self.assertEqual(self.check(video_probe()), [])
        self.assertTrue(self.check(video_probe(fps="60")))

    def test_portrait_export_cannot_pass_as_landscape(self):
        self.assertIn("Video dimensions differ", self.check(video_probe(width=1080, height=1920)))

    def test_truncated_output_and_lost_audio_are_rejected(self):
        errors = self.check(video_probe(duration="2.0", audio=False))
        self.assertEqual(len(errors), 2)

    def test_long_audio_cannot_hide_truncated_video(self):
        metadata = video_probe(duration="6.006")
        metadata["streams"][0]["duration"] = "2.0"
        self.assertTrue(self.check(metadata))

    def test_stretched_pixels_cannot_pass_canvas_dimensions(self):
        metadata = video_probe()
        metadata["streams"][0]["sample_aspect_ratio"] = "4:3"
        self.assertIn("Expected square pixels", self.check(metadata))

    def test_decoded_frame_count_must_cover_expected_video(self):
        metadata = video_probe()
        metadata["streams"][0]["nb_read_frames"] = "120"
        self.assertIn("Decoded video frame count differs", self.check(metadata))

    def test_audio_only_and_invalid_rate_do_not_pass(self):
        self.assertTrue(self.check({"streams": [{"codec_type": "audio"}]}))
        self.assertIn("Invalid frame rate", self.check(video_probe(fps="0/0")))

    def test_nonfinite_or_unavailable_duration_cannot_pass(self):
        for value in ("NaN", "inf", "N/A", "0"):
            with self.subTest(duration=value):
                self.assertIn("Missing or invalid duration", self.check(video_probe(duration=value)))

    def test_missing_tool_is_recorded_as_not_started(self):
        with tempfile.TemporaryDirectory() as folder:
            result = record_command([str(Path(folder) / "missing-tool.exe")],
                                    Path(folder) / "missing.json")
            self.assertFalse(result["started"])
            self.assertIsNone(result["exit_code"])

    def test_command_failure_is_preserved_in_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            receipt = Path(folder) / "failed.json"
            result = record_command([sys.executable, "-c", "raise SystemExit(7)"], receipt)
            self.assertEqual(result["exit_code"], 7)
            self.assertFalse(result["timed_out"])
            self.assertEqual(json.loads(receipt.read_text(encoding="utf-8"))["exit_code"], 7)

    def test_timeout_has_no_success_exit_code(self):
        with tempfile.TemporaryDirectory() as folder:
            result = record_command([sys.executable, "-c", "import time; time.sleep(5)"],
                                    Path(folder) / "timeout.json", timeout=0.2)
            self.assertIsNone(result["exit_code"])
            self.assertTrue(result["timed_out"])


class FixturePreservationTests(unittest.TestCase):
    def test_edited_subtitles_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            subtitles = Path(folder) / "captions.srt"
            subtitles.write_text("accepted manual correction", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Existing subtitles differ"):
                create(Path(folder))
            self.assertEqual(subtitles.read_text(encoding="utf-8"), "accepted manual correction")

    def test_existing_media_without_provenance_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "landscape_60.mp4"
            source.write_bytes(b"untrusted prior file")
            with self.assertRaisesRegex(ValueError, "lacks matching provenance"):
                create(Path(folder))
            self.assertEqual(source.read_bytes(), b"untrusted prior file")


if __name__ == "__main__":
    unittest.main()
