"""Twitch metadata cannot create a timeline, invent VOD context, or load media."""
from dataclasses import asdict, replace
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from native.core.creator_library import CreatorLibrary, LibraryError, RevisionConflict
from native.core.twitch_inbox import TwitchClip
from native.services import twitch_api


def helix(**changes):
    return dict(id="Clip-One", broadcaster_id="123", creator_id="456", game_id="789",
                title="A real reaction", created_at="2026-10-18T12:34:56Z", duration=12.9,
                view_count=42, url="https://clips.twitch.tv/Clip-One", video_id="", vod_offset=None,
                thumbnail_url="https://example.invalid/thumbnail?temporary=not-persisted") | changes


class TwitchProvenanceTests(unittest.TestCase):
    def test_decimal_duration_null_context_and_allowlisted_fields(self):
        clip = TwitchClip.from_helix(helix())
        self.assertEqual(clip.duration_us, 12_900_000)
        self.assertIsNone(clip.video_id)
        self.assertIsNone(clip.vod_offset_us)
        self.assertFalse(clip.has_vod_context)
        self.assertNotIn("thumbnail_url", asdict(clip))

    def test_zero_offset_is_known_and_missing_video_stays_clip_only(self):
        raw = helix(); raw.update(video_id="1234", vod_offset=0)
        clip = TwitchClip.from_helix(raw)
        self.assertTrue(clip.has_vod_context)
        self.assertEqual(clip.vod_offset_us, 0)
        self.assertFalse(replace(clip, video_id=None).has_vod_context)
        self.assertFalse(replace(clip, vod_offset_us=None).has_vod_context)

    def test_submicrosecond_fraction_cannot_round_into_a_valid_duration(self):
        raw = helix(); raw["duration"] = "1.000000000000000000000000000000000001"
        with self.assertRaises(ValueError): TwitchClip.from_helix(raw)

    def test_invalid_provider_values_cannot_become_persistent_provenance(self):
        cases = [dict(duration=True), dict(duration="NaN"), dict(duration="1e10000"), dict(duration=-1),
                 dict(duration="0.0000001"), dict(vod_offset=0.5), dict(vod_offset=True),
                 dict(created_at="2026-10-18T12:00:00"), dict(created_at="2026-10-18X12:00:00Z"), dict(view_count=-1),
                 dict(url="https://clips.twitch.tv/Other"), dict(url="https://attacker.invalid/Clip-One"),
                 dict(url="https://www.twitch.tv/Clip-One"),
                 dict(url="https://clips.twitch.tv/Clip-One?token=secret"), dict(id="../../outside")]
        for changes in cases:
            with self.subTest(changes=changes):
                raw = helix(); raw.update(changes)
                with self.assertRaises(ValueError):
                    TwitchClip.from_helix(raw)


class TwitchLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clipline-inbox-")
        self.root = Path(self.temp.name)
        self.db = self.root / "library.sqlite"

    def tearDown(self):
        self.temp.cleanup()

    def test_repeated_pages_update_metadata_without_duplicate_clips_or_media_access(self):
        with CreatorLibrary(self.db) as library, patch.object(Path, "open", side_effect=AssertionError("No media reads")):
            clip = TwitchClip.from_helix(helix())
            library.record_twitch_clips([clip])
            library.record_twitch_clips([replace(clip, view_count=900)])
            page = library.list_twitch_clips(limit=1)
            self.assertEqual(len(page["items"]), 1)
            self.assertEqual(page["items"][0].view_count, 900)
            self.assertIsNone(page["next_cursor"])
            self.assertEqual(library.list_assets()["items"], [])
            self.assertEqual(library.list_moments()["items"], [])
            raw = library.connection.execute("SELECT metadata FROM twitch_clips").fetchone()[0]
            self.assertNotIn("temporary", raw)

    def test_bounded_pages_keep_stable_cursors_when_existing_clips_refresh(self):
        with CreatorLibrary(self.db) as library:
            for batch in range(3):
                clips = []
                for number in range(batch * 100, min((batch + 1) * 100, 257)):
                    raw = helix(); raw.update(id=f"Clip-{number}", url=f"https://clips.twitch.tv/Clip-{number}")
                    clips.append(TwitchClip.from_helix(raw))
                library.record_twitch_clips(clips)
            first = library.list_twitch_clips(limit=100)
            library.record_twitch_clips([replace(first["items"][0], view_count=901)])
            second = library.list_twitch_clips(after=first["next_cursor"], limit=100)
            third = library.list_twitch_clips(after=second["next_cursor"], limit=100)
            self.assertEqual([len(p["items"]) for p in (first, second, third)], [100, 100, 57])
            self.assertEqual(len({c.id for p in (first, second, third) for c in p["items"]}), 257)
            self.assertIsNone(third["next_cursor"])
            with self.assertRaises(LibraryError): library.record_twitch_clips([first["items"][0]] * 101)

    def test_invalid_batch_leaves_previously_recorded_page_unchanged(self):
        with CreatorLibrary(self.db) as library:
            clip = TwitchClip.from_helix(helix())
            library.record_twitch_clips([clip])
            with self.assertRaises(LibraryError):
                library.record_twitch_clips([replace(clip, view_count=900), {"raw": "not validated"}])
            self.assertEqual(library.list_twitch_clips()["items"][0].view_count, 42)

    def test_refresh_preserves_fixed_identity_and_accepts_changed_availability(self):
        with CreatorLibrary(self.db) as library:
            clip = TwitchClip.from_helix(helix())
            library.record_twitch_clips([clip])
            for changed in (replace(clip, broadcaster_id="999"), replace(clip, creator_id="888"),
                            replace(clip, created_at="2025-10-18T12:34:56Z"),
                            replace(clip, created_at="2026-10-18T12:34:56.0000001Z")):
                with self.subTest(changed=changed), self.assertRaises(RevisionConflict):
                    library.record_twitch_clips([replace(clip, view_count=901), changed])
                self.assertEqual(library.list_twitch_clips()["items"], [clip])
            refreshed = replace(clip, created_at="2026-10-18T08:34:56-04:00", video_id="known-vod",
                                vod_offset_us=123_000_000, title="A revised title", view_count=902)
            library.record_twitch_clips([refreshed])
            self.assertEqual(library.list_twitch_clips()["items"], [refreshed])

    def test_oversized_library_cursors_fail_before_sqlite_binding(self):
        with CreatorLibrary(self.db) as library:
            for listing in (library.list_twitch_clips, library.list_assets, library.list_moments):
                with self.subTest(listing=listing.__name__), self.assertRaises(LibraryError):
                    listing(after=2**100)

    def test_v1_migration_preserves_existing_shared_asset_and_kit(self):
        schema = (Path(__file__).parent / "fixtures/creator-library-v1.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.db)) as old:
            old.executescript(schema)
            old.execute("INSERT INTO assets VALUES ('asset-existing',?,?,?,?)", ("a" * 64, "source-not-opened.mp4", "source-key", 42))
            old.execute("INSERT INTO kits VALUES ('kit-existing',1)")
            old.execute("INSERT INTO kit_versions VALUES ('kit-existing',1,?)", ('{"name":"Existing Kit"}',))
            old.commit()
        from native.core import accepted_transcript, durable_jobs
        migration_paths = {
            Path(accepted_transcript.__file__).with_name("caption_schema.sql"),
            Path(durable_jobs.__file__).with_name("durable_jobs_schema.sql"),
        }
        original_open = Path.open

        def migration_only(path, *args, **kwargs):
            if path not in migration_paths:
                raise AssertionError("Migration must not open media")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", migration_only):
            with CreatorLibrary(self.db) as library:
                self.assertEqual(library.connection.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(library.asset("asset-existing").sha256, "a" * 64)
                self.assertEqual(library.kit("kit-existing", 1)["name"], "Existing Kit")
                self.assertEqual(library.list_twitch_clips()["items"], [])


class TwitchPageTests(unittest.TestCase):
    def test_api_cursor_and_date_window_are_explicit_and_provenance_is_retained(self):
        with patch.object(twitch_api, "_get", return_value={"data": [helix()], "pagination": {"cursor": "next-page"}}) as request:
            page = twitch_api.clips_page("123", first=20, after="old-page",
                                         started_at="2026-10-17T00:00:00Z", ended_at="2026-10-18T00:00:00Z")
        self.assertEqual(page.next_cursor, "next-page")
        self.assertEqual(page.items[0].creator_id, "456")
        self.assertEqual(request.call_args.args, ("clips", {"broadcaster_id": "123", "first": 20,
            "after": "old-page", "started_at": "2026-10-17T00:00:00Z", "ended_at": "2026-10-18T00:00:00Z"}))

    def test_malformed_page_and_other_channel_do_not_produce_partial_results(self):
        other = helix(); other["broadcaster_id"] = "other"
        for response in ([], {"data": None}, {"data": [helix()], "pagination": []}, {"data": [helix(), other]},
                         {"data": [helix()], "pagination": {"cursor": 42}},
                         {"data": [], "pagination": {"cursor": False}}):
            with self.subTest(response=response), patch.object(twitch_api, "_get", return_value=response):
                with self.assertRaises(ValueError): twitch_api.clips_page("123")

    def test_invalid_page_sizes_are_rejected_before_any_network_call(self):
        with patch.object(twitch_api, "_get", side_effect=AssertionError("No network")):
            for size in (0, 101, True):
                with self.subTest(size=size), self.assertRaises(ValueError): twitch_api.clips_page("123", first=size)

    def test_invalid_date_windows_are_rejected_before_any_network_call(self):
        with patch.object(twitch_api, "_get", side_effect=AssertionError("No network")):
            for start, end in (("2026-10-18X12:00:00Z", None), ("2026-10-18T12:00:00Z", "2026-10-18T11:00:00Z")):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    twitch_api.clips_page("123", started_at=start, ended_at=end)


if __name__ == "__main__":
    unittest.main()
