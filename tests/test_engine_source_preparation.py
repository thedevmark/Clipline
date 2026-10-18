"""Keep pinned source preparation separate from compilation/runtime certification."""
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
import zipfile

from scripts.engine.prepare_filmcraft import prepare, sha256


@unittest.skipUnless(shutil.which("git"), "Git is required for source preparation")
class SourcePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clipline-source-")
        self.root = Path(self.temporary.name)
        self.archive = self.root / "source.zip"
        self.patch = self.root / "source.patch"
        self.original = "// café\npub fn value() -> u8 { 1 }\n"
        self.make_archive({"filmcraft-test/src/lib.rs": self.original})
        self.patch.write_text("diff --git a/src/lib.rs b/src/lib.rs\n"
                              "--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1,2 +1,2 @@\n"
                              " // café\n-pub fn value() -> u8 { 1 }\n+pub fn value() -> u8 { 2 }\n", encoding="utf-8", newline="\n")
        self.destination = self.root / "prepared"

    def tearDown(self):
        self.temporary.cleanup()

    def make_archive(self, entries):
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, content in entries.items():
                archive.writestr(name, content)

    def pin(self):
        return {"archive_sha256": sha256(self.archive), "patch_sha256": sha256(self.patch),
                "archive_root": "filmcraft-test", "upstream_commit": "fixture-only"}

    def test_exact_source_and_patch_apply_with_unicode_preserved(self):
        receipt = prepare(self.archive, self.patch, self.pin(), self.destination)
        source = Path(receipt["source_root"]) / "src/lib.rs"
        self.assertEqual(source.read_text(encoding="utf-8"), self.original.replace("{ 1 }", "{ 2 }"))
        self.assertEqual([c["exit_code"] for c in receipt["commands"]], [0, 0])
        self.assertEqual(json.loads((self.destination / "prepared-source.json").read_text())["status"], "prepared")
        self.assertIn("runtime checks are separate", receipt["scope"])

    def test_new_native_source_file_is_created(self):
        with self.patch.open("a", encoding="utf-8", newline="\n") as patch:
            patch.write("diff --git a/src/new.rs b/src/new.rs\nnew file mode 100644\n"
                        "--- /dev/null\n+++ b/src/new.rs\n@@ -0,0 +1 @@\n+// new native owner\n")
        receipt = prepare(self.archive, self.patch, self.pin(), self.destination)
        self.assertEqual((Path(receipt["source_root"]) / "src/new.rs").read_bytes(), b"// new native owner\n")

    def test_patched_bytes_must_match_declared_identity(self):
        pin = self.pin() | {"patched_files_sha256": {"src/lib.rs": "0" * 64}}
        with self.assertRaisesRegex(ValueError, "fingerprint differs"):
            prepare(self.archive, self.patch, pin, self.destination)
        receipt = json.loads((self.destination / "preparation-failure.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("src/lib.rs", receipt["patched_files_sha256"])
        self.assertFalse((self.destination / "prepared-source.json").exists())

    def test_existing_source_is_never_replaced(self):
        self.destination.mkdir()
        sentinel = self.destination / "user-change.rs"
        sentinel.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            prepare(self.archive, self.patch, self.pin(), self.destination)
        self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_wrong_archive_or_patch_is_rejected_before_writes(self):
        for key in ("archive_sha256", "patch_sha256"):
            with self.subTest(key=key):
                pin = self.pin() | {key: "0" * 64}
                with self.assertRaises(ValueError):
                    prepare(self.archive, self.patch, pin, self.destination)
                self.assertFalse(self.destination.exists())

    def test_traversal_other_roots_and_case_collisions_fail_before_writes(self):
        for entries in [
            {"filmcraft-test/../../escape": "bad"}, {"/absolute": "bad"},
            {"other/src.rs": "bad"}, {"filmcraft-test/.git/HEAD": "bad"},
            {"filmcraft-test/a": "one", "filmcraft-test/A": "two"},
        ]:
            with self.subTest(entries=list(entries)):
                self.make_archive(entries)
                with self.assertRaises(ValueError):
                    prepare(self.archive, self.patch, self.pin(), self.destination)
                self.assertFalse(self.destination.exists())
                self.assertFalse((self.root / "escape").exists())

    def test_symbolic_link_entry_is_rejected(self):
        with zipfile.ZipFile(self.archive, "w") as archive:
            entry = zipfile.ZipInfo("filmcraft-test/link")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "../../outside")
        with self.assertRaises(ValueError):
            prepare(self.archive, self.patch, self.pin(), self.destination)
        self.assertFalse(self.destination.exists())

    def test_patch_failure_keeps_diagnostic_and_original_source(self):
        self.patch.write_text(self.patch.read_text(encoding="utf-8").replace("-pub fn value()", "-pub fn missing()"), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            prepare(self.archive, self.patch, self.pin(), self.destination)
        failure = json.loads((self.destination / "preparation-failure.json").read_text())
        self.assertEqual(failure["status"], "failed")
        self.assertNotEqual(failure["commands"][0]["exit_code"], 0)
        self.assertEqual((self.destination / "filmcraft-test/src/lib.rs").read_text(encoding="utf-8"), self.original)

    def test_enclosing_git_repository_cannot_redirect_patch_paths(self):
        subprocess.run([shutil.which("git"), "init", "--quiet", str(self.root)], check=True, capture_output=True)
        parent_file = self.root / "src/lib.rs"
        parent_file.parent.mkdir()
        parent_file.write_text("Preserve parent checkout", encoding="utf-8")
        receipt = prepare(self.archive, self.patch, self.pin(), self.destination)
        self.assertEqual(parent_file.read_text(encoding="utf-8"), "Preserve parent checkout")
        self.assertIn("{ 2 }", (Path(receipt["source_root"]) / "src/lib.rs").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
