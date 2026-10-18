"""Exercise interruption and competing writers against real filesystem snapshots."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from native.core.project_store import ProjectStore, SnapshotError, StaleGeneration, StoreBusy


CHILD = r'''
import os, sys
from pathlib import Path
from native.core.project_store import ProjectStore
directory, expected, phase = sys.argv[1:]
if phase == "lock":
    with ProjectStore(directory)._writer():
        print("locked", flush=True)
        sys.stdin.readline()
    sys.exit(0)
class InterruptedStore(ProjectStore):
    @staticmethod
    def _write(path, content):
        ProjectStore._write(path, content)
        if (phase == "document" and path.suffix == ".fcproj") or (phase == "pointer" and path.suffix == ".pending"):
            os._exit(71)
store = InterruptedStore(directory)
store.publish(b"native-revision-two", expected_generation=expected, revision=2,
              engine="filmcraft", engine_version="0.9.0", suffix=".fcproj")
os._exit(72)
'''


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="clipline-generation-")
        self.root = Path(self.temporary.name)
        self.store = ProjectStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def publish(self, content=b"native-revision-one", **changes):
        settings = dict(expected_generation=None, revision=1, engine="filmcraft",
                        engine_version="0.9.0", suffix=".fcproj")
        settings.update(changes)
        return self.store.publish(content, **settings)

    def test_reopen_returns_exact_native_document_and_keeps_previous_generation(self):
        self.assertIsNone(self.store.load())
        first = self.publish()
        second = self.publish(b"native-revision-two", expected_generation=first.generation, revision=2)
        self.assertEqual(ProjectStore(self.root).load(), second)
        self.assertEqual(first.path.read_bytes(), b"native-revision-one")
        self.assertEqual(second.document, b"native-revision-two")

    def test_stale_writer_and_revision_do_not_create_or_replace_documents(self):
        first = self.publish()
        before = {p.name for p in self.root.iterdir()}
        for options in [dict(expected_generation=None, revision=2),
                        dict(expected_generation=first.generation, revision=1)]:
            with self.assertRaises(StaleGeneration):
                self.publish(b"stale", **options)
        self.assertEqual(self.store.load(), first)
        self.assertEqual(before, {p.name for p in self.root.iterdir()})

    def test_actual_process_exit_recovers_old_or_published_generation(self):
        first = self.publish()
        for phase in ("document", "pointer", "published"):
            with self.subTest(phase=phase):
                command = [sys.executable, "-c", CHILD, str(self.root), first.generation, phase]
                process = subprocess.run(command, capture_output=True, text=True, timeout=30)
                self.assertEqual(process.returncode, 72 if phase == "published" else 71, process.stderr)
                loaded = self.store.load()
                if phase == "published":
                    self.assertEqual(loaded.revision, 2)
                    self.assertEqual(loaded.document, b"native-revision-two")
                else:
                    self.assertEqual(loaded, first)
        # Abandoned native documents remain available for explicit recovery.
        self.assertEqual(len(list(self.root.glob("*.fcproj"))), 4)

    def test_process_lock_rejects_second_writer_and_releases_after_exit(self):
        first = self.publish()
        child = subprocess.Popen([sys.executable, "-c", CHILD, str(self.root), first.generation, "lock"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            with self.assertRaises(StoreBusy):
                self.publish(b"blocked", expected_generation=first.generation, revision=2)
        finally:
            _out, error = child.communicate("release\n", timeout=30)
        self.assertEqual(child.returncode, 0, error)
        self.publish(b"after-lock", expected_generation=first.generation, revision=2)
        self.assertEqual(self.store.load().document, b"after-lock")

    def test_corrupt_or_missing_document_never_silently_opens_an_orphan(self):
        first = self.publish()
        first.path.write_bytes(b"changed")
        with self.assertRaisesRegex(SnapshotError, "fingerprint"):
            self.store.load()
        first.path.unlink()
        with self.assertRaises(SnapshotError):
            self.store.load()

    def test_pointer_refuses_traversal_unknown_schema_and_invalid_metadata(self):
        first = self.publish()
        path = self.root / "CURRENT.json"
        original = json.loads(path.read_text())
        for fields in [{"generation": "../elsewhere"}, {"suffix": "/../../elsewhere"},
                       {"schema": True}, {"revision": False}, {"sha256": "unknown"},
                       {"engine": ""}, {"unexpected": 1}]:
            with self.subTest(fields=fields):
                path.write_text(json.dumps(original | fields))
                with self.assertRaises(SnapshotError):
                    self.store.load()
        self.assertEqual(first.path.read_bytes(), first.document)


if __name__ == "__main__":
    unittest.main()
