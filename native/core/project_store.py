"""Publish immutable native-engine snapshots through one atomic current pointer.

The engine must serialize its complete document, including Clipline's embedded
metadata, before calling publish. This owner does not interpret or duplicate an
editing timeline. Guarantees cover process interruption, not hardware failure.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import uuid


class SnapshotError(ValueError):
    pass


class StaleGeneration(SnapshotError):
    pass


class StoreBusy(SnapshotError):
    pass


@dataclass(frozen=True)
class NativeSnapshot:
    generation: str
    revision: int
    engine: str
    engine_version: str
    sha256: str
    path: Path
    document: bytes


class ProjectStore:
    def __init__(self, directory: Path | str):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _writer(self):
        with (self.directory / "writer.lock").open("a+b") as lock:
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0"); lock.flush()
            lock.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise StoreBusy("Another process is saving this project") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def load(self) -> NativeSnapshot | None:
        pointer = self.directory / "CURRENT.json"
        try:
            encoded = pointer.read_bytes()
        except FileNotFoundError:
            return None
        try:
            if len(encoded) > 4096:
                raise SnapshotError("Invalid project pointer size")
            value = json.loads(encoded)
            if not isinstance(value, dict) or set(value) != {
                    "schema", "generation", "revision", "engine", "engine_version", "sha256", "suffix"}:
                raise SnapshotError("Invalid project pointer fields")
            if type(value["schema"]) is not int or value["schema"] != 1:
                raise SnapshotError("Unsupported project pointer schema")
            generation = value["generation"]
            if not isinstance(generation, str) or str(uuid.UUID(generation)) != generation:
                raise SnapshotError("Invalid generation ID")
            self._metadata(value["revision"], value["engine"], value["engine_version"], value["suffix"])
            if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
                raise SnapshotError("Invalid native document fingerprint")
            path = self.directory / (generation + value["suffix"])
            document = path.read_bytes()
            if not document or hashlib.sha256(document).hexdigest() != value["sha256"]:
                raise SnapshotError("Native document fingerprint mismatch")
            return NativeSnapshot(generation, value["revision"], value["engine"], value["engine_version"],
                                  value["sha256"], path, document)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            if isinstance(exc, SnapshotError):
                raise
            raise SnapshotError("Cannot read the committed native snapshot") from exc

    @staticmethod
    def _metadata(revision: int, engine: str, engine_version: str, suffix: str):
        if type(revision) is not int or revision < 0:
            raise SnapshotError("Revision must be a nonnegative integer")
        for text in (engine, engine_version):
            if not isinstance(text, str) or not text.strip() or len(text) > 128:
                raise SnapshotError("Engine and version must be nonempty bounded text")
        if not isinstance(suffix, str) or not re.fullmatch(r"\.[a-z0-9]{1,16}", suffix):
            raise SnapshotError("Invalid native document suffix")

    @staticmethod
    def _write(path: Path, content: bytes):
        with path.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def publish(self, document: bytes, *, expected_generation: str | None, revision: int,
                engine: str, engine_version: str, suffix: str) -> NativeSnapshot:
        """Save one fully serialized document; reject stale saves without mutation.

        Leftover generations after interruption are retained. Only CURRENT.json
        chooses a committed document. Engine parsing and linked-edit validation
        belong to the engine gateway, before publication and after load.
        Revision is the advancing publication number, not an engine session's
        counter or the undoable revision stored in native linked-edit metadata.
        """
        self._metadata(revision, engine, engine_version, suffix)
        if not isinstance(document, bytes) or not document:
            raise SnapshotError("A nonempty native document is required")
        with self._writer():
            current = self.load()
            if (current.generation if current else None) != expected_generation:
                raise StaleGeneration("The project changed; reload before saving")
            if current and revision <= current.revision:
                raise StaleGeneration("The new revision must advance the committed revision")
            generation = str(uuid.uuid4())
            digest = hashlib.sha256(document).hexdigest()
            path = self.directory / (generation + suffix)
            self._write(path, document)
            pointer = {"schema": 1, "generation": generation, "revision": revision,
                       "engine": engine, "engine_version": engine_version, "sha256": digest, "suffix": suffix}
            pending = self.directory / (generation + ".pending")
            self._write(pending, json.dumps(pointer, sort_keys=True).encode("utf-8"))
            os.replace(pending, self.directory / "CURRENT.json")
            return NativeSnapshot(generation, revision, engine, engine_version, digest, path, document)
