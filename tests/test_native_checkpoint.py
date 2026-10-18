import copy
import json
from pathlib import Path

import pytest

from native.core.project_store import ProjectStore, StaleGeneration
from native.services.filmcraft_bridge import OutcomeUnknown
from native.services.native_checkpoint import (
    CheckpointError, MAX_DOCUMENT, WorkingCopy, checkout, publish_checkpoint,
)


VERSION = "0.9.0+clipline-test"
OWNERS = [{"owner": 5, "revision": 3}]


def initial(store, document=b"opaque initial native document"):
    return store.publish(document, expected_generation=None, revision=1,
                         engine="filmcraft", engine_version=VERSION, suffix=".fcproj")


class NativeCapture:
    """Contract fixture; native command correctness is tested in the Rust engine."""
    def __init__(self, *, change=None, after_capture=None):
        self.calls = []
        self.change = change
        self.after_capture = after_capture

    def execute(self, command, params):
        self.calls.append((command, copy.deepcopy(params)))
        Path(params["path"]).write_bytes(b"opaque captured native document")
        result = {"path": params["path"], "validation": {
            "valid": True, "revision": params["expectedRevision"], "savedRevision": 2,
            "projectPath": params["expectedProjectPath"],
            "owners": [{"owner": row["owner"], "linksRevision": row["revision"]}
                       for row in params["expectedLinksRevisions"]],
        }}
        if self.after_capture:
            self.after_capture(params)
        if self.change:
            self.change(result, params)
        return result


def save(store, bridge, working, **overrides):
    return publish_checkpoint(store, bridge, working, **({
        "native_revision": 7, "links_revisions": OWNERS, "engine_version": VERSION,
    } | overrides))


def test_checkout_uses_a_fresh_working_path_and_retains_committed_bytes(tmp_path):
    store = ProjectStore(tmp_path / "moment")
    committed = initial(store)
    first, second = checkout(store), checkout(store)
    assert first.path != second.path
    assert first.path.is_relative_to(store.directory / "working")
    assert first.path != committed.path
    first.path.write_bytes(b"working changes")
    assert store.load() == committed
    assert second.path.read_bytes() == committed.document
    origin = json.loads(first.path.with_name("origin.json").read_text())
    assert origin["generation"] == committed.generation


def test_native_snapshot_receipt_and_opaque_bytes_publish_then_advance_the_working_token(tmp_path):
    store = ProjectStore(tmp_path)
    old = initial(store)
    working = checkout(store)
    bridge = NativeCapture()
    snapshot, continued = save(store, bridge, working)
    command, params = bridge.calls[0]
    assert command == "clipline.saveSnapshot"
    assert params["expectedProjectPath"] == str(working.path)
    assert params["expectedRevision"] == 7
    assert params["expectedLinksRevisions"] == OWNERS
    assert snapshot.document == b"opaque captured native document"
    assert snapshot.revision == 2  # Publication and native session revisions have different owners.
    assert continued == WorkingCopy(working.path, snapshot.generation, 2)
    assert store.load() == snapshot
    assert old.path.read_bytes() == old.document
    assert working.path.read_bytes() == old.document  # Save a Copy does not edit the working file.
    capture = Path(params["path"])
    evidence = json.loads(capture.with_name("capture.json").read_text())
    assert evidence["sha256"] == snapshot.sha256
    assert evidence["expected_generation"] == old.generation
    assert evidence["validation"]["revision"] == 7
    assert evidence["state"] == "captured_not_yet_published"
    again, _ = save(store, bridge, continued, native_revision=8)
    assert again.revision == 3


def test_unlinked_inventory_is_explicitly_supported(tmp_path):
    store = ProjectStore(tmp_path); initial(store)
    snapshot, _ = save(store, NativeCapture(), checkout(store), links_revisions=[])
    assert snapshot.revision == 2


def test_old_working_copy_refuses_before_native_io_after_another_publication(tmp_path):
    store = ProjectStore(tmp_path); initial(store)
    working = checkout(store)
    save(store, NativeCapture(), working)
    bridge = NativeCapture()
    before = set((store.directory / "captures").iterdir())
    with pytest.raises(StaleGeneration):
        save(store, bridge, working)
    assert bridge.calls == []
    assert set((store.directory / "captures").iterdir()) == before


def test_competing_publication_after_native_capture_preserves_the_capture_without_retry(tmp_path):
    store = ProjectStore(tmp_path); original = initial(store)
    working = checkout(store)

    def compete(params):
        store.publish(b"other writer", expected_generation=original.generation, revision=2,
                      engine="filmcraft", engine_version=VERSION, suffix=".fcproj")

    bridge = NativeCapture(after_capture=compete)
    with pytest.raises(StaleGeneration):
        save(store, bridge, working)
    assert len(bridge.calls) == 1
    assert store.load().document == b"other writer"
    retained = Path(bridge.calls[0][1]["path"])
    assert retained.read_bytes() == b"opaque captured native document"
    assert retained.with_name("capture.json").exists()


def test_lost_native_reply_retains_capture_and_does_not_publish_or_retry(tmp_path):
    store = ProjectStore(tmp_path); original = initial(store)

    def lose_reply(params):
        raise OutcomeUnknown("request-uuid", "engine.execute", "connection lost")

    bridge = NativeCapture(after_capture=lose_reply)
    with pytest.raises(OutcomeUnknown):
        save(store, bridge, checkout(store))
    assert len(bridge.calls) == 1
    assert store.load() == original
    capture = Path(bridge.calls[0][1]["path"])
    assert capture.exists()
    assert json.loads(capture.with_name("intent.json").read_text())["expected_generation"] == original.generation
    assert not capture.with_name("capture.json").exists()


@pytest.mark.parametrize("change", [
    lambda r, p: r.update(path="elsewhere.fcproj"),
    lambda r, p: r.update(validation={}),
    lambda r, p: r["validation"].update(valid=False),
    lambda r, p: r["validation"].update(revision=8),
    lambda r, p: r["validation"].update(revision=True),
    lambda r, p: r["validation"].update(projectPath="other working project"),
    lambda r, p: r["validation"].update(owners=[]),
    lambda r, p: r["validation"].update(owners=[{"owner": 5, "linksRevision": 4}]),
    lambda r, p: r["validation"].update(owners=[{"owner": 5, "linksRevision": 3}] * 2),
    lambda r, p: r["validation"].update(owners=[None]),
    lambda r, p: Path(p["path"]).write_bytes(b""),
])
def test_mismatched_or_empty_capture_never_advances_current(tmp_path, change):
    store = ProjectStore(tmp_path); original = initial(store)
    bridge = NativeCapture(change=change)
    with pytest.raises(CheckpointError):
        save(store, bridge, checkout(store))
    assert len(bridge.calls) == 1
    assert store.load() == original


def test_oversize_capture_is_rejected_before_publication(tmp_path):
    store = ProjectStore(tmp_path); original = initial(store)

    def too_large(result, params):
        with Path(params["path"]).open("r+b") as stream:
            stream.truncate(MAX_DOCUMENT + 1)

    with pytest.raises(CheckpointError, match="64 MiB"):
        save(store, NativeCapture(change=too_large), checkout(store))
    assert store.load() == original


def test_missing_capture_never_advances_current(tmp_path):
    store = ProjectStore(tmp_path); original = initial(store)
    bridge = NativeCapture(after_capture=lambda p: Path(p["path"]).unlink())
    with pytest.raises(CheckpointError, match="Cannot read"):
        save(store, bridge, checkout(store))
    assert len(bridge.calls) == 1
    assert store.load() == original
    assert Path(bridge.calls[0][1]["path"]).with_name("intent.json").exists()


def test_unreadable_capture_is_retained_without_publication(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path); original = initial(store)
    working = checkout(store)
    original_open = Path.open

    def refuse_read(path, mode="r", *args, **kwargs):
        if path.name == "snapshot.fcproj" and mode == "rb":
            raise PermissionError("capture temporarily unavailable")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", refuse_read)
    bridge = NativeCapture()
    with pytest.raises(CheckpointError, match="Cannot read"):
        save(store, bridge, working)
    assert store.load() == original
    assert Path(bridge.calls[0][1]["path"]).exists()


def test_capture_modified_during_read_is_retained_without_publication(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path); original = initial(store)
    working = checkout(store)
    original_open = Path.open

    class ConcurrentWriter:
        def __init__(self, path, stream): self.path, self.stream = path, stream
        def __enter__(self): return self
        def __exit__(self, *args): return self.stream.__exit__(*args)
        def fileno(self): return self.stream.fileno()
        def read(self, size):
            content = self.stream.read(size)
            with original_open(self.path, "ab") as writer:
                writer.write(b"concurrent change")
            return content

    def change_during_read(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path.name == "snapshot.fcproj" and mode == "rb":
            return ConcurrentWriter(path, stream)
        return stream

    monkeypatch.setattr(Path, "open", change_during_read)
    bridge = NativeCapture()
    with pytest.raises(CheckpointError, match="changed while reading"):
        save(store, bridge, working)
    assert store.load() == original
    assert Path(bridge.calls[0][1]["path"]).exists()


def test_committed_generation_path_is_never_given_to_the_editor(tmp_path):
    store = ProjectStore(tmp_path); original = initial(store)
    bridge = NativeCapture()
    invalid = WorkingCopy(original.path, original.generation, original.revision)
    with pytest.raises(CheckpointError, match="owned working file"):
        save(store, bridge, invalid)
    assert bridge.calls == []
    assert original.path.read_bytes() == original.document


@pytest.mark.parametrize("inventory", [None, {}, [{"owner": 5}], [{"owner": True, "revision": 0}],
                                       [{"owner": 5, "revision": -1}], OWNERS * 2])
def test_invalid_inventory_does_not_contact_the_editor(tmp_path, inventory):
    store = ProjectStore(tmp_path); initial(store)
    bridge = NativeCapture()
    with pytest.raises(CheckpointError):
        save(store, bridge, checkout(store), links_revisions=inventory)
    assert bridge.calls == []


def test_empty_store_does_not_create_a_working_copy(tmp_path):
    store = ProjectStore(tmp_path)
    with pytest.raises(CheckpointError, match="initial native document"):
        checkout(store)
    assert not (store.directory / "working").exists()
