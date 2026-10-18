from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from native.core.creator_library import (CreatorLibrary, FingerprintMismatch,
                                         KitUpdateConflict, LibraryError, RevisionConflict)

ROOT = Path(__file__).resolve().parents[1]


class LibraryTests(unittest.TestCase):
    def setUp(self):
        work = Path(tempfile.gettempdir()).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix="clipline-library-", dir=work)
        self.folder = Path(self.temp.name).resolve()
        assert self.folder.is_relative_to(work.resolve())
        self.path = self.folder / "library.sqlite"
        self.library = CreatorLibrary(self.path)
        self.source = self.folder / "source.mp4"; self.source.write_bytes(b"unchanged source media")
        self.asset = self.library.register_asset(self.source)
        self.descriptor = {"name": "Creator", "asset_refs": {"source": self.asset.id},
                           "properties": {"caption.font_size": 40, "caption.color": "white", "portrait.crop": [0.25, 0, 0.5, 1]},
                           "layout_profiles": [{"id": "gameplay-scene", "scene_key": "OBS Scene A", "source_width": 1920,
                                                "source_height": 1080, "regions": {"facecam": {"x": 0, "y": .1, "w": .2, "h": .3}}}],
                           "vocabulary": ["deutschmark"]}
        self.kit_id = self.library.create_kit(self.descriptor)

    def tearDown(self):
        self.library.close()
        assert self.folder.is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.temp.cleanup()

    def moment(self, **changes):
        args = {"title": "Moment", "source_asset_id": self.asset.id, "source_in_us": 0,
                "source_out_us": 18_000_000, "kit_id": self.kit_id, "kit_version": 1}
        args.update(changes)
        return self.library.create_moment(**args)

    def revise(self, **properties):
        descriptor = copy.deepcopy(self.descriptor); descriptor["properties"].update(properties)
        return self.library.revise_kit(self.kit_id, descriptor, expected_version=1)

    def test_two_moments_reuse_one_source_and_kit_without_copying_media(self):
        before = {p.name for p in self.folder.iterdir()}
        a = self.moment(); b = self.moment(title="Second moment")
        self.assertEqual(a.source_asset_id, b.source_asset_id)
        self.assertEqual((a.kit_id, a.kit_version), (b.kit_id, b.kit_version))
        self.assertEqual(self.library.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)
        self.assertEqual(before, {p.name for p in self.folder.iterdir()})
        self.assertEqual(self.source.read_bytes(), b"unchanged source media")

    def test_register_same_hash_deduplicates_without_implicit_relocation(self):
        duplicate = self.folder / "duplicate.mp4"; duplicate.write_bytes(self.source.read_bytes())
        again = self.library.register_asset(self.source); copied = self.library.register_asset(duplicate)
        self.assertEqual(self.asset, again); self.assertEqual(self.asset, copied)
        self.assertEqual(self.library.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)

    def test_fingerprint_verified_relocation_updates_all_moment_and_kit_references(self):
        a = self.moment(); b = self.moment()
        moved = self.folder / "moved.mp4"; self.source.rename(moved)
        asset = self.library.relocate_asset(self.asset.id, moved, expected_path=self.asset.path)
        for moment in (a, b):
            self.assertEqual(self.library.asset(self.library.moment(moment.id).source_asset_id).path, str(moved))
        kit_ref = self.library.kit(self.kit_id, 1)["asset_refs"]["source"]
        self.assertEqual(self.library.asset(kit_ref).path, str(moved)); self.assertEqual(asset.sha256, self.asset.sha256)
        self.assertTrue(moved.exists())

    def test_foreign_replacement_and_source_path_replacement_are_rejected(self):
        foreign = self.folder / "foreign.mp4"; foreign.write_bytes(b"unrelated replacement")
        with self.assertRaises(FingerprintMismatch):
            self.library.relocate_asset(self.asset.id, foreign, expected_path=self.asset.path)
        self.assertEqual(self.library.asset(self.asset.id), self.asset)
        self.source.write_bytes(b"new bytes at an existing path")
        with self.assertRaises(FingerprintMismatch):self.library.register_asset(self.source)

    def test_relocation_rejects_stale_path_and_missing_target(self):
        duplicate = self.folder / "duplicate.mp4"; duplicate.write_bytes(self.source.read_bytes())
        self.library.relocate_asset(self.asset.id, duplicate, expected_path=self.asset.path)
        with self.assertRaises(RevisionConflict):
            self.library.relocate_asset(self.asset.id, self.source, expected_path=self.asset.path)
        with self.assertRaises(FileNotFoundError):
            self.library.relocate_asset(self.asset.id, self.folder / "missing", expected_path=str(duplicate))

    def test_kit_revision_pins_old_moments_and_all_descriptors_are_copied(self):
        a = self.moment(); self.revise(**{"caption.font_size": 50})
        self.assertEqual(self.library.moment(a.id).kit_version, 1)
        self.assertEqual(self.library.effective_properties(a.id)["caption.font_size"], 40)
        b = self.moment(kit_version=2)
        self.assertEqual(self.library.effective_properties(b.id)["caption.font_size"], 50)
        detached = self.library.kit(self.kit_id, 1); detached["properties"]["caption.font_size"] = 999
        self.assertEqual(self.library.kit(self.kit_id, 1)["properties"]["caption.font_size"], 40)

    def test_kit_revisions_and_asset_bindings_are_immutable_in_sqlite(self):
        for command in ["UPDATE kit_versions SET descriptor='{}'", "DELETE FROM kit_versions", "UPDATE kit_assets SET role='wrong'", "DELETE FROM kit_assets"]:
            with self.assertRaises(sqlite3.IntegrityError):self.library.connection.execute(command)

    def test_stale_kit_publication_rejects_without_partial_version(self):
        self.revise(**{"caption.font_size": 50})
        with self.assertRaises(RevisionConflict):self.library.revise_kit(self.kit_id, self.descriptor, expected_version=1)
        self.assertEqual(self.library.connection.execute("SELECT COUNT(*) FROM kit_versions").fetchone()[0], 2)

    def test_published_kit_cannot_gain_an_extra_asset_binding(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.library.connection.execute("INSERT INTO kit_assets VALUES (?,?,?,?)",
                                            (self.kit_id, 1, "unpublished-role", self.asset.id))
        self.assertEqual(self.library.connection.execute("SELECT COUNT(*) FROM kit_assets").fetchone()[0], 1)

    def test_nonconflicting_kit_update_keeps_local_crop_override(self):
        override = [0.35, 0, 0.5, 1]; m = self.moment(overrides={"portrait.crop": override})
        self.revise(**{"caption.font_size": 50})
        self.assertEqual(self.library.preview_kit_update(m.id, 2)["conflicts"], [])
        updated = self.library.apply_kit_update(m.id, 2, expected_revision=1)
        self.assertEqual(updated.overrides["portrait.crop"], override)
        self.assertEqual(self.library.effective_properties(m.id)["caption.font_size"], 50)

    def test_conflict_requires_resolution_and_keeps_database_unchanged(self):
        m = self.moment(overrides={"caption.font_size": 64}); self.revise(**{"caption.font_size": 50})
        with self.assertRaises(KitUpdateConflict) as caught:self.library.apply_kit_update(m.id, 2, expected_revision=1)
        self.assertEqual(caught.exception.conflicts[0]["local"], 64)
        self.assertEqual(self.library.moment(m.id), m)

    def test_conflict_keep_local_and_use_kit_are_explicit(self):
        a = self.moment(overrides={"caption.font_size": 64}); b = self.moment(overrides={"caption.font_size": 64})
        self.revise(**{"caption.font_size": 50})
        self.library.apply_kit_update(a.id, 2, expected_revision=1, resolutions={"caption.font_size": "keep_local"})
        self.library.apply_kit_update(b.id, 2, expected_revision=1, resolutions={"caption.font_size": "use_kit"})
        self.assertEqual(self.library.effective_properties(a.id)["caption.font_size"], 64)
        self.assertEqual(self.library.effective_properties(b.id)["caption.font_size"], 50)
        self.assertNotIn("caption.font_size", self.library.moment(b.id).overrides)

    def test_removed_property_conflicts_and_keep_local_preserves_it(self):
        m = self.moment(overrides={"caption.font_size": 64})
        descriptor = copy.deepcopy(self.descriptor); descriptor["properties"].pop("caption.font_size")
        self.library.revise_kit(self.kit_id, descriptor, expected_version=1)
        plan = self.library.preview_kit_update(m.id, 2); self.assertFalse(plan["conflicts"][0]["new_present"])
        self.library.apply_kit_update(m.id, 2, expected_revision=1, resolutions={"caption.font_size": "keep_local"})
        self.assertEqual(self.library.effective_properties(m.id)["caption.font_size"], 64)

    def test_override_revision_prevents_stale_update_resolution(self):
        m = self.moment(overrides={"caption.font_size": 64}); self.revise(**{"caption.font_size": 50})
        self.library.set_overrides(m.id, {"caption.font_size": 72}, expected_revision=1)
        with self.assertRaises(RevisionConflict):
            self.library.apply_kit_update(m.id, 2, expected_revision=1, resolutions={"caption.font_size": "use_kit"})
        self.assertEqual(self.library.moment(m.id).kit_version, 1)
        self.assertEqual(self.library.effective_properties(m.id)["caption.font_size"], 72)

    def test_invalid_conflict_decisions_do_not_change_moment(self):
        m = self.moment(overrides={"caption.font_size": 64}); self.revise(**{"caption.font_size": 50})
        for decisions in [{"caption.font_size": "automatic"}, {"unknown": "keep_local"}]:
            with self.assertRaises(LibraryError):self.library.apply_kit_update(m.id, 2, expected_revision=1, resolutions=decisions)
            self.assertEqual(self.library.moment(m.id), m)

    def test_malformed_layout_profiles_fail_before_persistence(self):
        bad_profiles = [None, {}, [{"id": "x"}], [{"id": "x", "scene_key": "", "source_width": 1920, "source_height": 1080, "regions": {}}]]
        for profiles in bad_profiles:
            with self.subTest(profiles=profiles):
                descriptor = copy.deepcopy(self.descriptor); descriptor["layout_profiles"] = profiles
                with self.assertRaises(LibraryError):self.library.create_kit(descriptor)
        for patch_value in [{"w": 2}, {"x": .9, "w": .2}, {"h": float("nan")}, {"x": True}]:
            descriptor = copy.deepcopy(self.descriptor); descriptor["layout_profiles"][0]["regions"]["facecam"].update(patch_value)
            with self.assertRaises(LibraryError):self.library.create_kit(descriptor)
        self.assertEqual(self.library.connection.execute("SELECT COUNT(*) FROM kits").fetchone()[0], 1)

    def test_same_dimensions_do_not_replace_scene_identity(self):
        descriptor = copy.deepcopy(self.descriptor); second = copy.deepcopy(descriptor["layout_profiles"][0]); second["id"] = "scene-b"; second["scene_key"] = "OBS Scene B"; descriptor["layout_profiles"].append(second)
        kit = self.library.create_kit(descriptor)
        self.assertEqual(len(self.library.kit(kit, 1)["layout_profiles"]), 2)
        descriptor["layout_profiles"][1]["id"] = "gameplay-scene"
        with self.assertRaises(LibraryError):self.library.create_kit(descriptor)

    def test_bad_asset_reference_rolls_back_kit_and_revision(self):
        descriptor = copy.deepcopy(self.descriptor); descriptor["asset_refs"]["logo"] = "not-registered"
        with self.assertRaises(LibraryError):self.library.create_kit(descriptor)
        with self.assertRaises(LibraryError):self.library.revise_kit(self.kit_id, descriptor, expected_version=1)
        self.assertEqual(self.library.connection.execute("SELECT latest_version FROM kits WHERE id=?", (self.kit_id,)).fetchone()[0], 1)
        self.assertEqual(self.library.connection.execute("SELECT COUNT(*) FROM kits").fetchone()[0], 1)

    def test_malformed_metadata_and_intervals_are_rejected(self):
        for interval in [(-1, 2), (2, 2), (3, 2), (True, 5), (1.5, 2)]:
            with self.assertRaises(LibraryError):self.moment(source_in_us=interval[0], source_out_us=interval[1])
        for overrides in [[], {"unknown": 1}, {"caption.font_size": float("inf")}]:
            with self.assertRaises(LibraryError):self.moment(overrides=overrides)
        descriptor = copy.deepcopy(self.descriptor); descriptor["properties"]["illegal"] = object()
        with self.assertRaises(LibraryError):self.library.create_kit(descriptor)
        descriptor = copy.deepcopy(self.descriptor); descriptor["properties"]["nested"] = {1: "invalid JSON key"}
        with self.assertRaises(LibraryError):self.library.create_kit(descriptor)

    def test_conflict_comparison_preserves_json_boolean_type(self):
        descriptor = copy.deepcopy(self.descriptor); descriptor["properties"]["transition.enabled"] = False
        kit_id = self.library.create_kit(descriptor)
        m = self.moment(kit_id=kit_id, overrides={"transition.enabled": True})
        descriptor["properties"]["transition.enabled"] = 1
        self.library.revise_kit(kit_id, descriptor, expected_version=1)
        self.assertEqual(self.library.preview_kit_update(m.id, 2)["conflicts"][0]["property"], "transition.enabled")

    def test_reopen_preserves_pins_overrides_and_relocation(self):
        m = self.moment(overrides={"caption.font_size": 64}); self.revise(**{"caption.color": "yellow"})
        moved = self.folder / "moved.mp4"; self.source.rename(moved)
        self.library.relocate_asset(self.asset.id, moved, expected_path=self.asset.path)
        self.library.close(); self.library = CreatorLibrary(self.path)
        self.assertEqual(self.library.moment(m.id), m)
        self.assertEqual(self.library.effective_properties(m.id)["caption.color"], "white")
        self.assertEqual(self.library.asset(m.source_asset_id).path, str(moved))

    def test_assets_are_paginated_without_media_io(self):
        for n in range(4):
            p = self.folder / f"asset{n}"; p.write_bytes(f"asset{n}".encode()); self.library.register_asset(p)
        ids = []; cursor = 0
        with patch("pathlib.Path.open", side_effect=AssertionError("metadata must not open media")):
            while True:
                page = self.library.list_assets(after=cursor, limit=2); ids.extend(a.id for a in page["items"])
                if page["next_cursor"] is None:break
                cursor = page["next_cursor"]
        self.assertEqual(len(ids), 5); self.assertEqual(len(set(ids)), 5)

    def test_10000_archived_moments_are_keyset_paginated_without_media_io(self):
        with patch("pathlib.Path.open", side_effect=AssertionError("archive metadata opened media")):
            for n in range(10_000):self.moment(title=f"Archived moment {n}")
            ids = []; cursor = 0; pages = 0
            while True:
                page = self.library.list_moments(after=cursor, limit=137); pages += 1
                self.assertLessEqual(len(page["items"]), 137); ids.extend(m.id for m in page["items"])
                if page["next_cursor"] is None:break
                cursor = page["next_cursor"]
            self.assertEqual(len(ids), 10_000); self.assertEqual(len(set(ids)), 10_000); self.assertEqual(pages, 73)
        plan = self.library.connection.execute("EXPLAIN QUERY PLAN SELECT * FROM moments WHERE sequence>? ORDER BY sequence LIMIT ?", (5000, 51)).fetchall()
        self.assertIn("INTEGER PRIMARY KEY", plan[0][3])
        self.assertEqual(self.library.connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)

    def test_pagination_rejects_unbounded_or_malformed_requests(self):
        for args in [{"limit": 0}, {"limit": 201}, {"limit": True}, {"after": -1}, {"after": "0"}]:
            with self.assertRaises(LibraryError):self.library.list_moments(**args)
            with self.assertRaises(LibraryError):self.library.list_assets(**args)

    def test_foreign_key_restricts_shared_asset_deletion(self):
        self.moment()
        with self.assertRaises(sqlite3.IntegrityError):self.library.connection.execute("DELETE FROM assets WHERE id=?", (self.asset.id,))
        self.assertTrue(self.source.exists())

    def test_unknown_schema_and_unversioned_foreign_database_are_rejected(self):
        for name, sql in [("future.sqlite", "PRAGMA user_version=99"), ("foreign.sqlite", "CREATE TABLE foreign_data(id)")]:
            path = self.folder / name
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(sql); connection.commit()
            with self.assertRaises(LibraryError):CreatorLibrary(path)

    def test_concurrent_first_open_creates_one_schema(self):
        target = self.folder / "concurrent.sqlite"
        def open_library(_index):
            with CreatorLibrary(target) as library:
                return library.connection.execute("PRAGMA user_version").fetchone()[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(open_library, range(4))), [2, 2, 2, 2])

    def test_second_connection_cannot_publish_a_stale_kit_version(self):
        with CreatorLibrary(self.path) as second:
            self.revise(**{"caption.font_size": 50})
            with self.assertRaises(RevisionConflict):second.revise_kit(self.kit_id, self.descriptor, expected_version=1)
            self.assertEqual(second.kit(self.kit_id, 2)["properties"]["caption.font_size"], 50)


if __name__ == "__main__":unittest.main(verbosity=2)
