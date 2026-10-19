"""Publish one CAS-validated native working copy through ProjectStore.

The editor keeps the editable document and undo history. This boundary captures
opaque native bytes through one native saveSnapshot command; it owns no timeline.
All calls are synchronous and belong on the application's background job owner.
It requires the qualified clipline.saveSnapshot command and has no saveCopy fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import uuid

from native.core.project_store import NativeSnapshot, ProjectStore, StaleGeneration
from native.services.filmcraft_bridge import FilmcraftBridge


MAX_DOCUMENT = 64 * 1024 * 1024


class CheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class WorkingCopy:
    path: Path
    base_generation: str
    base_revision: int

    def __post_init__(self):
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise CheckpointError("The working path must be absolute")
        try:
            if not isinstance(self.base_generation, str) or str(uuid.UUID(self.base_generation)) != self.base_generation:
                raise ValueError()
        except (ValueError, AttributeError) as exc:
            raise CheckpointError("A canonical base generation is required") from exc
        if type(self.base_revision) is not int or self.base_revision < 0:
            raise CheckpointError("A publication base revision is required")


def checkout(store: ProjectStore) -> WorkingCopy:
    """Copy a committed document to a fresh editable path; retain older work."""
    snapshot = store.load()
    if snapshot is None:
        raise CheckpointError("Commit an initial native document before checkout")
    if snapshot.engine != "filmcraft" or snapshot.path.suffix != ".fcproj":
        raise CheckpointError("This editor requires a Filmcraft native document")
    if len(snapshot.document) > MAX_DOCUMENT:
        raise CheckpointError("The moment document exceeds the 64 MiB working-copy limit")
    directory = store.directory / "working" / str(uuid.uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    path = directory / "working.fcproj"
    _write_once(path, snapshot.document)
    _write_once(directory / "origin.json", _json({
        "schema": 1, "generation": snapshot.generation, "revision": snapshot.revision,
        "sha256": snapshot.sha256,
    }))
    return WorkingCopy(path, snapshot.generation, snapshot.revision)


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf8")


def _write_once(path: Path, content: bytes):
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _inventory(value):
    if not isinstance(value, list) or len(value) > 64:
        raise CheckpointError("Expected linked owners must be an explicit bounded list")
    result = {}
    for row in value:
        if not isinstance(row, dict) or set(row) != {"owner", "revision"}:
            raise CheckpointError("Expected owner entries require owner and revision")
        owner, revision = row["owner"], row["revision"]
        if any(type(number) is not int or not 0 <= number < 2**64 for number in (owner, revision)):
            raise CheckpointError("Native owner identities and revisions require unsigned integers")
        if owner in result:
            raise CheckpointError("Duplicate expected owner")
        result[owner] = revision
    return result


def publish_checkpoint(store: ProjectStore, bridge: FilmcraftBridge, working: WorkingCopy, *,
                       native_revision: int, links_revisions: list[dict], engine_version: str,
                       capture_details: dict | None = None) -> tuple[NativeSnapshot, WorkingCopy]:
    """Capture once, then publish with the working copy's base generation CAS.

    A lost native reply never publishes or retries. The owned capture is retained
    for reconciliation. A competing publication also retains the captured bytes.
    The caller keeps the returned WorkingCopy for its next checkpoint. Automatic
    working-copy recovery, engine launch, asset resolution and UI wiring are not
    provided by this boundary.
    """
    if type(working) is not WorkingCopy:
        raise CheckpointError("An explicit native working copy is required")
    if capture_details is not None and (type(capture_details) is not dict or capture_details):
        raise CheckpointError("Capture details must be an empty output dictionary")
    path = working.path.resolve(strict=True)
    working_root = (store.directory / "working").resolve()
    if not path.is_file() or path.suffix != ".fcproj" or not path.is_relative_to(working_root):
        raise CheckpointError("The editor must use an owned working file, outside committed generations")
    if type(native_revision) is not int or not 0 <= native_revision < 2**64:
        raise CheckpointError("A native session revision is required")
    if not isinstance(engine_version, str) or not engine_version.strip() or len(engine_version) > 128:
        raise CheckpointError("The qualified native engine version is required")
    expected = _inventory(links_revisions)
    current = store.load()
    if current is None or current.generation != working.base_generation or current.revision != working.base_revision:
        raise StaleGeneration("The committed project changed; reconcile before saving this working copy")
    if current.engine != "filmcraft":
        raise CheckpointError("The committed project uses another engine")
    directory = store.directory / "captures" / str(uuid.uuid4())
    directory.mkdir(parents=True, exist_ok=False)
    capture = directory / "snapshot.fcproj"
    request = {
        "path": str(capture), "expectedProjectPath": str(path),
        "expectedRevision": native_revision,
        "expectedLinksRevisions": [{"owner": owner, "revision": revision} for owner, revision in expected.items()],
    }
    _write_once(directory / "intent.json", _json({
        "schema": 1, "expected_generation": working.base_generation,
        "expected_publication_revision": working.base_revision, "request": request,
    }))
    result = bridge.execute("clipline.saveSnapshot", request)
    try:
        if not isinstance(result, dict) or result.get("path") != str(capture):
            raise CheckpointError("Native snapshot path differs from the owned capture")
        validation = result["validation"]
        if (not isinstance(validation, dict) or validation.get("valid") is not True
                or type(validation.get("revision")) is not int or validation["revision"] != native_revision
                or validation.get("projectPath") != str(path)):
            raise CheckpointError("Native snapshot does not match the requested working revision")
        owners = validation["owners"]
        if not isinstance(owners, list):
            raise CheckpointError("Native snapshot owner inventory is absent")
        observed = _inventory([{"owner": row["owner"], "revision": row["linksRevision"]} for row in owners])
        if observed != expected:
            raise CheckpointError("Native snapshot owner inventory differs")
        with capture.open("rb") as stream:
            before = os.fstat(stream.fileno())
            document = stream.read(MAX_DOCUMENT + 1)
            after = os.fstat(stream.fileno())
        if not document or len(document) > MAX_DOCUMENT:
            raise CheckpointError("Captured native document is empty or exceeds 64 MiB")
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise CheckpointError("Native capture changed while reading")
    except (KeyError, TypeError) as exc:
        raise CheckpointError("Native snapshot evidence is malformed") from exc
    except OSError as exc:
        raise CheckpointError("Cannot read the retained native capture") from exc
    digest = hashlib.sha256(document).hexdigest()
    _write_once(directory / "capture.json", _json({
        "schema": 1, "state": "captured_not_yet_published", "sha256": digest,
        "bytes": len(document), "expected_generation": working.base_generation,
        "publication_revision": current.revision + 1, "validation": validation,
        "engine": "filmcraft", "engine_version": engine_version,
    }))
    if capture_details is not None:
        capture_details.update(directory=directory, validation=validation)
    snapshot = store.publish(document, expected_generation=working.base_generation,
                             revision=current.revision + 1, engine="filmcraft",
                             engine_version=engine_version, suffix=".fcproj")
    return snapshot, WorkingCopy(path, snapshot.generation, snapshot.revision)
