import unittest

from native.services.export_presets import build_clip_export_args, style_preset_by_key, format_preset_by_key


class TestExportArgsSubtitle(unittest.TestCase):
    def test_no_subtitle_has_no_ass_filter(self):
        args = build_clip_export_args(0, 1000, style_preset_by_key("gameplay_focus"))
        vf = args[args.index("-vf") + 1]
        self.assertNotIn("ass=", vf)

    def test_subtitle_appended_to_vf_chain(self):
        args = build_clip_export_args(
            0, 1000, style_preset_by_key("gameplay_focus"),
            fmt=format_preset_by_key("shorts"), subtitle_ass=r"C\:/tmp/clip.ass",
        )
        vf = args[args.index("-vf") + 1]
        self.assertTrue(vf.rstrip().endswith(r"ass='C\:/tmp/clip.ass'"))
        self.assertIn("crop=1080:1920", vf)  # ass comes AFTER scale/crop


if __name__ == "__main__":
    unittest.main()
