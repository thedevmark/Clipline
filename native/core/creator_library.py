"""Shared asset identity, immutable Kit versions, and lightweight moment metadata.

No operation decodes media or implements an editing timeline. Property names are
semantic keys such as ``caption.font_size``; each value is one merge unit.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any

from native.core.twitch_inbox import TwitchClip, timestamp_identity
from native.core.accepted_transcript import AcceptedTranscriptStore, install_schema as install_caption_schema
from native.core.durable_jobs import DurableJobs, install_schema as install_job_schema


class LibraryError(ValueError):
    pass


class FingerprintMismatch(LibraryError):
    pass


class RevisionConflict(LibraryError):
    pass


class KitUpdateConflict(LibraryError):
    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__("Kit changes conflict with local properties: " + ", ".join(c["property"] for c in conflicts))


@dataclass(frozen=True)
class Asset:
    id: str
    sha256: str
    path: str
    bytes: int


@dataclass(frozen=True)
class KitRecord:
    kit_id: str
    version: int
    descriptor_json: str
    descriptor_sha256: str


@dataclass(frozen=True)
class KitSummary:
    id: str
    version: int
    name: str


@dataclass(frozen=True)
class Moment:
    id: str
    title: str
    source_asset_id: str
    source_in_us: int
    source_out_us: int
    kit_id: str
    kit_version: int
    overrides: dict
    revision: int


_SCHEMA = """
CREATE TABLE assets (
 id TEXT PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE,
 path TEXT NOT NULL, path_key TEXT NOT NULL UNIQUE, bytes INTEGER NOT NULL CHECK(bytes>=0)
);
CREATE TABLE kits (id TEXT PRIMARY KEY, latest_version INTEGER NOT NULL CHECK(latest_version>=1));
CREATE TABLE kit_versions (
 kit_id TEXT NOT NULL REFERENCES kits(id), version INTEGER NOT NULL CHECK(version>=1),
 descriptor TEXT NOT NULL, PRIMARY KEY(kit_id,version)
);
CREATE TABLE kit_assets (
 kit_id TEXT NOT NULL, version INTEGER NOT NULL, role TEXT NOT NULL,
 asset_id TEXT NOT NULL REFERENCES assets(id), PRIMARY KEY(kit_id,version,role),
 FOREIGN KEY(kit_id,version) REFERENCES kit_versions(kit_id,version)
);
CREATE TABLE moments (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
 source_asset_id TEXT NOT NULL REFERENCES assets(id),
 source_in_us INTEGER NOT NULL CHECK(source_in_us>=0),
 source_out_us INTEGER NOT NULL CHECK(source_out_us>source_in_us),
 kit_id TEXT NOT NULL, kit_version INTEGER NOT NULL,
 overrides TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 FOREIGN KEY(kit_id,kit_version) REFERENCES kit_versions(kit_id,version)
);
CREATE INDEX moments_kit ON moments(kit_id,kit_version);
CREATE TRIGGER kit_versions_no_update BEFORE UPDATE ON kit_versions BEGIN
 SELECT RAISE(ABORT,'Kit versions are immutable'); END;
CREATE TRIGGER kit_versions_no_delete BEFORE DELETE ON kit_versions BEGIN
 SELECT RAISE(ABORT,'Kit versions are immutable'); END;
CREATE TRIGGER kit_assets_no_update BEFORE UPDATE ON kit_assets BEGIN
 SELECT RAISE(ABORT,'Kit asset bindings are immutable'); END;
CREATE TRIGGER kit_assets_no_delete BEFORE DELETE ON kit_assets BEGIN
 SELECT RAISE(ABORT,'Kit asset bindings are immutable'); END;
CREATE TRIGGER kit_assets_match_descriptor BEFORE INSERT ON kit_assets
WHEN NOT EXISTS (
 SELECT 1 FROM kit_versions,
 json_each(kit_versions.descriptor, '$.asset_refs') AS binding
 WHERE kit_versions.kit_id=NEW.kit_id AND kit_versions.version=NEW.version
 AND binding.key=NEW.role AND binding.value=NEW.asset_id
)
BEGIN SELECT RAISE(ABORT,'Kit asset bindings must match the immutable descriptor'); END;
PRAGMA user_version=1;
"""


_TWITCH_SCHEMA = """
CREATE TABLE twitch_clips (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL UNIQUE,
 metadata TEXT NOT NULL
);
PRAGMA user_version=2;
"""

_TWITCH_SOURCE_SCHEMA = """
CREATE TABLE twitch_clip_sources (
 clip_id TEXT PRIMARY KEY REFERENCES twitch_clips(clip_id),
 asset_id TEXT NOT NULL REFERENCES assets(id), selected_metadata TEXT NOT NULL,
 profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256)=64),
 result_id TEXT NOT NULL, result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64),
 job_id TEXT NOT NULL UNIQUE REFERENCES durable_job_intents(id)
);
CREATE TRIGGER twitch_clip_sources_no_update BEFORE UPDATE ON twitch_clip_sources BEGIN
 SELECT RAISE(ABORT,'Twitch source bindings are immutable'); END;
CREATE TRIGGER twitch_clip_sources_no_delete BEFORE DELETE ON twitch_clip_sources BEGIN
 SELECT RAISE(ABORT,'Twitch source bindings are immutable'); END;
PRAGMA user_version=3;
"""

_MOMENT_ORIGIN_SCHEMA = """
CREATE TABLE moment_twitch_origins (
 moment_id TEXT PRIMARY KEY REFERENCES moments(id),
 clip_id TEXT NOT NULL REFERENCES twitch_clip_sources(clip_id),
 asset_id TEXT NOT NULL REFERENCES assets(id), source_sha256 TEXT NOT NULL, source_bytes INTEGER NOT NULL,
 selected_metadata TEXT NOT NULL, profile_sha256 TEXT NOT NULL,
 result_id TEXT NOT NULL, result_sha256 TEXT NOT NULL,
 job_id TEXT NOT NULL REFERENCES durable_job_intents(id), source_facts TEXT NOT NULL,
 kit_id TEXT NOT NULL, kit_version INTEGER NOT NULL, kit_descriptor_sha256 TEXT NOT NULL,
 profile_id TEXT NOT NULL, scene_key TEXT NOT NULL,
 FOREIGN KEY(kit_id,kit_version) REFERENCES kit_versions(kit_id,version)
);
CREATE TRIGGER moment_twitch_origins_no_update BEFORE UPDATE ON moment_twitch_origins BEGIN
 SELECT RAISE(ABORT,'Moment Twitch origins are immutable'); END;
CREATE TRIGGER moment_twitch_origins_no_delete BEFORE DELETE ON moment_twitch_origins BEGIN
 SELECT RAISE(ABORT,'Moment Twitch origins are immutable'); END;
CREATE TRIGGER moment_twitch_origins_match BEFORE INSERT ON moment_twitch_origins
WHEN NOT EXISTS (
 SELECT 1 FROM moments, twitch_clip_sources
 WHERE moments.id=NEW.moment_id AND moments.source_asset_id=NEW.asset_id
 AND moments.kit_id=NEW.kit_id AND moments.kit_version=NEW.kit_version
 AND twitch_clip_sources.clip_id=NEW.clip_id AND twitch_clip_sources.asset_id=NEW.asset_id
 AND twitch_clip_sources.selected_metadata=NEW.selected_metadata
 AND twitch_clip_sources.profile_sha256=NEW.profile_sha256
 AND twitch_clip_sources.result_id=NEW.result_id AND twitch_clip_sources.result_sha256=NEW.result_sha256
 AND twitch_clip_sources.job_id=NEW.job_id
)
BEGIN SELECT RAISE(ABORT,'Moment origin must match the admitted source and initial Kit'); END;
PRAGMA user_version=4;
"""


def _json(value: Any) -> str:
    def validate_keys(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise LibraryError("JSON object keys must be strings")
            for child in item.values():
                validate_keys(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                validate_keys(child)
    validate_keys(value)
    try:
        text = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError) as exc:
        raise LibraryError("Descriptor values must be finite JSON data") from exc
    if len(text.encode("utf-8")) > 262144:
        raise LibraryError("Metadata exceeds the 256 KiB limit")
    return text


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise LibraryError(f"{field} must be nonempty text of at most 512 characters")
    return value


def _properties(value: Any) -> dict:
    if not isinstance(value, dict):
        raise LibraryError("properties must be an object")
    for key in value:
        _name(key, "property name")
    return json.loads(_json(value))


def _descriptor(value: Any) -> dict:
    if not isinstance(value, dict):
        raise LibraryError("Kit descriptor must be an object")
    allowed = {"name", "asset_refs", "properties", "layout_profiles", "vocabulary"}
    if set(value) - allowed:
        raise LibraryError("Unknown Kit descriptor fields: " + ", ".join(sorted(set(value) - allowed)))
    result = {"name": _name(value.get("name"), "Kit name"),
              "asset_refs": value.get("asset_refs", {}),
              "properties": _properties(value.get("properties", {})),
              "layout_profiles": value.get("layout_profiles", []),
              "vocabulary": value.get("vocabulary", [])}
    if not isinstance(result["asset_refs"], dict):
        raise LibraryError("asset_refs must map roles to stable asset IDs")
    for role, asset_id in result["asset_refs"].items():
        _name(role, "asset role"); _name(asset_id, "asset ID")
    if not isinstance(result["vocabulary"], list) or any(not isinstance(x, str) or not x.strip() for x in result["vocabulary"]):
        raise LibraryError("vocabulary must contain nonempty strings")
    profiles = result["layout_profiles"]
    if not isinstance(profiles, list):
        raise LibraryError("layout_profiles must be an array")
    ids = set()
    for profile in profiles:
        if not isinstance(profile, dict) or set(profile) != {"id", "scene_key", "source_width", "source_height", "regions"}:
            raise LibraryError("Each layout profile requires id, scene_key, source dimensions, and regions")
        pid = _name(profile["id"], "profile ID"); _name(profile["scene_key"], "scene key")
        if pid in ids:
            raise LibraryError("Duplicate profile ID")
        ids.add(pid)
        for dimension in ("source_width", "source_height"):
            if type(profile[dimension]) is not int or profile[dimension] <= 0:
                raise LibraryError("Source dimensions must be positive integers")
        regions = profile["regions"]
        if not isinstance(regions, dict) or not regions:
            raise LibraryError("Profile regions must be a nonempty object")
        for role, rect in regions.items():
            _name(role, "region role")
            if not isinstance(rect, dict) or set(rect) != {"x", "y", "w", "h"}:
                raise LibraryError("Regions require normalized x, y, w, h")
            if any(type(n) not in (int, float) or not math.isfinite(n) for n in rect.values()):
                raise LibraryError("Region coordinates must be finite numbers")
            if not (0 <= rect["x"] < 1 and 0 <= rect["y"] < 1 and 0 < rect["w"] <= 1 and 0 < rect["h"] <= 1
                    and rect["x"] + rect["w"] <= 1 + 1e-9 and rect["y"] + rect["h"] <= 1 + 1e-9):
                raise LibraryError("Region must fit within the normalized source frame")
    return json.loads(_json(result))


def _fingerprint(path: Path | str) -> tuple[str, str, int]:
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_file():
        raise LibraryError("Asset path must be a regular file")
    before = resolved.stat(); digest = hashlib.sha256()
    with resolved.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    after = resolved.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise LibraryError("Asset changed during fingerprint verification")
    return str(resolved), digest.hexdigest(), after.st_size


class CreatorLibrary:
    def __init__(self, database: Path | str):
        self.connection = sqlite3.connect(str(database), isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                tables = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if tables:
                    raise LibraryError("Refusing an unversioned nonempty database")
                statement = ""
                for line in _SCHEMA.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self.connection.execute(statement)
                        statement = ""
                version = 1
            if version == 1:
                statement = ""
                for line in _TWITCH_SCHEMA.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        self.connection.execute(statement)
                        statement = ""
            elif version not in (2, 3, 4):
                raise LibraryError(f"Unsupported library schema version {version}")
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            self.connection.close()
            raise
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            install_caption_schema(self.connection)
            self.captions = AcceptedTranscriptStore(self.connection)
            install_job_schema(self.connection)
            self._install_twitch_source_schema()
            self.jobs = DurableJobs(self.connection)
            self._install_moment_origin_schema()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> CreatorLibrary:
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @contextmanager
    def _write(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise

    def record_twitch_clips(self, clips: tuple[TwitchClip, ...] | list[TwitchClip]) -> None:
        """Upsert one bounded metadata page; never open or download source media."""
        if not isinstance(clips, (list, tuple)) or len(clips) > 100:
            raise LibraryError("A Twitch page must contain at most 100 clips")
        records = []
        for clip in clips:
            if type(clip) is not TwitchClip:
                raise LibraryError("A validated TwitchClip is required")
            descriptor = asdict(clip)
            TwitchClip(**descriptor)
            records.append((clip, _json(descriptor)))
        with self._write():
            for clip, metadata in records:
                current = self.connection.execute("SELECT metadata FROM twitch_clips WHERE clip_id=?", (clip.id,)).fetchone()
                if current:
                    previous = TwitchClip(**json.loads(current["metadata"]))
                    if ((previous.broadcaster_id, previous.creator_id, timestamp_identity(previous.created_at)) !=
                            (clip.broadcaster_id, clip.creator_id, timestamp_identity(clip.created_at))):
                        raise RevisionConflict("Twitch clip identity changed; the existing page is preserved")
                self.connection.execute(
                    "INSERT INTO twitch_clips(clip_id,metadata) VALUES (?,?) "
                    "ON CONFLICT(clip_id) DO UPDATE SET metadata=excluded.metadata", (clip.id, metadata))

    def list_twitch_clips(self, *, after: int = 0, limit: int = 50) -> dict:
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 200:
            raise LibraryError("Invalid pagination: bounded SQLite cursor and 1<=limit<=200 required")
        rows = self.connection.execute(
            "SELECT sequence,metadata FROM twitch_clips WHERE sequence>? ORDER BY sequence LIMIT ?",
            (after, limit + 1)).fetchall()
        page = rows[:limit]
        return {"items": [TwitchClip(**json.loads(row["metadata"])) for row in page],
                "next_cursor": page[-1]["sequence"] if len(rows) > limit else None}

    def _install_twitch_source_schema(self) -> None:
        with self._write():
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version in (3, 4):
                return
            if version != 2:
                raise LibraryError("Twitch source migration requires schema2")
            statement = ""
            for line in _TWITCH_SOURCE_SCHEMA.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    self.connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise LibraryError("Incomplete Twitch source migration")

    def _install_moment_origin_schema(self) -> None:
        with self._write():
            version = self.connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 4:
                return
            if version != 3:
                raise LibraryError('Moment origin migration requires schema3')
            statement = ''
            for line in _MOMENT_ORIGIN_SCHEMA.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    self.connection.execute(statement)
                    statement = ''
            if statement.strip():
                raise LibraryError('Incomplete Moment origin migration')

    def twitch_clip(self, clip_id: str) -> TwitchClip:
        _name(clip_id, "clip ID")
        row = self.connection.execute("SELECT metadata FROM twitch_clips WHERE clip_id=?", (clip_id,)).fetchone()
        if row is None:
            raise LibraryError("Unknown Twitch clip")
        return TwitchClip(**json.loads(row["metadata"]))

    def twitch_source(self, clip_id: str) -> dict | None:
        _name(clip_id, "clip ID")
        row = self.connection.execute("SELECT * FROM twitch_clip_sources WHERE clip_id=?", (clip_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["selected_metadata"] = json.loads(value["selected_metadata"])
        return value

    def _commit_twitch_source(self, connection, current_job, attestation, verified_plan) -> None:
        """SQL-only publisher for the trusted guarded intake settlement owner."""
        from native.core.durable_jobs import _id, _reference, _sha

        if connection is not self.connection or not connection.in_transaction:
            raise LibraryError("Twitch publication requires the settlement transaction")
        plan = json.loads(_json(verified_plan))
        if set(plan) != {"job", "clip", "profile_sha256", "result_ref", "media", "existing"}:
            raise LibraryError("Exact verified publication plan required")
        if plan["job"] != current_job or current_job["kind"] != "io":
            raise RevisionConflict("Publication belongs to another claim")
        _reference(plan["result_ref"]); _sha(plan["profile_sha256"])
        if (attestation["result_ref"] != plan["result_ref"]
                or current_job["input"]["profile_sha256"] != plan["profile_sha256"]):
            raise RevisionConflict("Publication result or profile differs")
        selected = TwitchClip(**plan["clip"])
        cached = self.twitch_clip(selected.id)
        identity = lambda clip: (clip.broadcaster_id, clip.creator_id, timestamp_identity(clip.created_at))
        if identity(selected) != identity(cached):
            raise RevisionConflict("Selected Twitch identity changed")
        if self.twitch_source(selected.id) is not None:
            raise RevisionConflict("Twitch source already bound")
        media = plan["media"]
        if not isinstance(media, dict) or set(media) != {"id", "sha256", "path", "path_key", "bytes"}:
            raise LibraryError("Exact verified source fingerprint required")
        _id(media["id"]); _sha(media["sha256"])
        if (type(media["bytes"]) is not int or not 0 < media["bytes"] <= 2**63-1
                or not isinstance(media["path"], str) or len(media["path"]) > 32767
                or not Path(media["path"]).is_absolute() or media["path_key"] != os.path.normcase(media["path"])):
            raise LibraryError("Invalid verified source fingerprint")
        row = connection.execute("SELECT * FROM assets WHERE sha256=?", (media["sha256"],)).fetchone()
        actual = dict(row) if row is not None else None
        if actual != plan["existing"]:
            raise RevisionConflict("Source registration changed after guarded verification")
        if actual is None:
            if connection.execute("SELECT 1 FROM assets WHERE path_key=?", (media["path_key"],)).fetchone():
                raise RevisionConflict("Managed source path is already registered")
            connection.execute("INSERT INTO assets VALUES (?,?,?,?,?)",
                               tuple(media[key] for key in ("id", "sha256", "path", "path_key", "bytes")))
            asset_id = media["id"]
        else:
            if actual["bytes"] != media["bytes"]:
                raise RevisionConflict("Existing source size differs")
            asset_id = actual["id"]
        connection.execute("INSERT INTO twitch_clip_sources VALUES(?,?,?,?,?,?,?)", (
            selected.id, asset_id, _json(plan["clip"]), plan["profile_sha256"],
            plan["result_ref"]["id"], plan["result_ref"]["sha256"], current_job["id"]))

    def asset(self, asset_id: str) -> Asset:
        row = self.connection.execute("SELECT id,sha256,path,bytes FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise LibraryError(f"Unknown asset {asset_id}")
        return Asset(**dict(row))

    def verify_asset(self, asset_id: str) -> Asset:
        """Read the current bytes without changing identity or source location.

        This is a point-in-time fingerprint check, not a lifetime file lock.
        """
        asset = self.asset(asset_id)
        path, digest, size = _fingerprint(asset.path)
        if self.asset(asset_id) != asset:
            raise RevisionConflict("The source registration changed during verification")
        if path != asset.path or digest != asset.sha256 or size != asset.bytes:
            raise FingerprintMismatch("Current source bytes differ from the registered asset")
        return asset

    def register_asset(self, path: Path | str) -> Asset:
        path, digest, size = _fingerprint(path)
        key = os.path.normcase(path)
        with self._write():
            current = self.connection.execute("SELECT id,sha256 FROM assets WHERE path_key=?", (key,)).fetchone()
            if current and current["sha256"] != digest:
                raise FingerprintMismatch("A registered source path now contains different bytes")
            current = self.connection.execute("SELECT id FROM assets WHERE sha256=?", (digest,)).fetchone()
            if current:
                asset_id = current["id"]
            else:
                asset_id = str(uuid.uuid4())
                self.connection.execute("INSERT INTO assets VALUES (?,?,?,?,?)", (asset_id, digest, path, key, size))
        return self.asset(asset_id)

    def list_assets(self, *, after: int = 0, limit: int = 50) -> dict:
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 200:
            raise LibraryError("Invalid pagination: bounded SQLite cursor and 1<=limit<=200 required")
        rows = self.connection.execute("SELECT rowid AS cursor,id,sha256,path,bytes FROM assets WHERE rowid>? ORDER BY rowid LIMIT ?", (after, limit + 1)).fetchall()
        more = len(rows) > limit; page = rows[:limit]
        return {"items": [Asset(row["id"], row["sha256"], row["path"], row["bytes"]) for row in page],
                "next_cursor": page[-1]["cursor"] if more else None}

    def relocate_asset(self, asset_id: str, new_path: Path | str, *, expected_path: str) -> Asset:
        path, digest, size = _fingerprint(new_path)
        with self._write():
            current = self.asset(asset_id)
            if current.path != expected_path:
                raise RevisionConflict("The source location changed; read it again")
            if digest != current.sha256 or size != current.bytes:
                raise FingerprintMismatch("Replacement does not match the registered source")
            self.connection.execute("UPDATE assets SET path=?,path_key=? WHERE id=?", (path, os.path.normcase(path), asset_id))
        return self.asset(asset_id)

    def _insert_version(self, kit_id: str, version: int, descriptor: dict) -> None:
        for asset_id in descriptor["asset_refs"].values():
            self.asset(asset_id)
        self.connection.execute("INSERT INTO kit_versions VALUES (?,?,?)", (kit_id, version, _json(descriptor)))
        self.connection.executemany("INSERT INTO kit_assets VALUES (?,?,?,?)", [(kit_id, version, role, asset) for role, asset in descriptor["asset_refs"].items()])

    def create_kit(self, descriptor: dict) -> str:
        descriptor = _descriptor(descriptor); kit_id = str(uuid.uuid4())
        with self._write():
            self.connection.execute("INSERT INTO kits VALUES (?,1)", (kit_id,))
            self._insert_version(kit_id, 1, descriptor)
        return kit_id

    def kit_record(self, kit_id: str, version: int) -> KitRecord:
        """Read the exact immutable descriptor bytes for native creation/provenance.

        Hash the stored UTF-8 text, not another serializer's representation.
        """
        row = self.connection.execute("SELECT descriptor FROM kit_versions WHERE kit_id=? AND version=?", (kit_id, version)).fetchone()
        if row is None:
            raise LibraryError("Unknown Kit version")
        text = row["descriptor"]
        return KitRecord(kit_id, version, text, hashlib.sha256(text.encode("utf8")).hexdigest())

    def kit(self, kit_id: str, version: int) -> dict:
        return json.loads(self.kit_record(kit_id, version).descriptor_json)

    def list_kits(self, *, after: int = 0, limit: int = 50) -> dict:
        """Page current Kit summaries without loading assets or native templates."""
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 200:
            raise LibraryError("Invalid pagination: bounded SQLite cursor and 1<=limit<=200 required")
        rows = self.connection.execute(
            "SELECT k.rowid AS cursor,k.id,k.latest_version AS version,"
            "json_extract(v.descriptor,'$.name') AS name FROM kits k "
            "JOIN kit_versions v ON v.kit_id=k.id AND v.version=k.latest_version "
            "WHERE k.rowid>? ORDER BY k.rowid LIMIT ?", (after, limit + 1)).fetchall()
        page = rows[:limit]
        return {"items": [KitSummary(row["id"], row["version"], row["name"]) for row in page],
                "next_cursor": page[-1]["cursor"] if len(rows) > limit else None}

    def list_kit_versions(self, kit_id: str, *, after: int = 0, limit: int = 50) -> dict:
        """List immutable versions for explicit selection, never a latest alias."""
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 200:
            raise LibraryError("Invalid pagination: bounded SQLite cursor and 1<=limit<=200 required")
        if self.connection.execute("SELECT 1 FROM kits WHERE id=?", (kit_id,)).fetchone() is None:
            raise LibraryError("Unknown Kit")
        rows = self.connection.execute(
            "SELECT version,json_extract(descriptor,'$.name') AS name FROM kit_versions "
            "WHERE kit_id=? AND version>? ORDER BY version LIMIT ?", (kit_id, after, limit + 1)).fetchall()
        page = rows[:limit]
        return {"items": [KitSummary(kit_id, row["version"], row["name"]) for row in page],
                "next_cursor": page[-1]["version"] if len(rows) > limit else None}

    def revise_kit(self, kit_id: str, descriptor: dict, *, expected_version: int) -> int:
        descriptor = _descriptor(descriptor)
        with self._write():
            row = self.connection.execute("SELECT latest_version FROM kits WHERE id=?", (kit_id,)).fetchone()
            if row is None:
                raise LibraryError("Unknown Kit")
            if row[0] != expected_version:
                raise RevisionConflict("The Kit changed; read its latest version")
            new_version = expected_version + 1
            self._insert_version(kit_id, new_version, descriptor)
            self.connection.execute("UPDATE kits SET latest_version=? WHERE id=?", (new_version, kit_id))
        return new_version

    def create_moment(self, *, title: str, source_asset_id: str, source_in_us: int,
                      source_out_us: int, kit_id: str, kit_version: int, overrides: dict | None = None) -> Moment:
        with self._write():
            moment_id = self._insert_moment(title=title, source_asset_id=source_asset_id,
                source_in_us=source_in_us, source_out_us=source_out_us,
                kit_id=kit_id, kit_version=kit_version, overrides=overrides)
        return self.moment(moment_id)

    def _insert_moment(self, *, title, source_asset_id, source_in_us, source_out_us,
                       kit_id, kit_version, overrides=None):
        title = _name(title, "moment title")
        if type(source_in_us) is not int or type(source_out_us) is not int or not 0 <= source_in_us < source_out_us:
            raise LibraryError("Source interval requires ordered nonnegative integer microseconds")
        overrides = _properties({} if overrides is None else overrides)
        moment_id = str(uuid.uuid4())
        self.asset(source_asset_id); kit = self.kit(kit_id, kit_version)
        if set(overrides) - set(kit["properties"]):
            raise LibraryError("Local overrides must name existing Kit properties")
        self.connection.execute("INSERT INTO moments(id,title,source_asset_id,source_in_us,source_out_us,kit_id,kit_version,overrides) VALUES (?,?,?,?,?,?,?,?)",
                                (moment_id, title, source_asset_id, source_in_us, source_out_us, kit_id, kit_version, _json(overrides)))
        return moment_id

    def admitted_twitch_source(self, clip_id):
        """SQL admission only. The caller must separately verify the bounded artifacts and files."""
        binding = self.twitch_source(clip_id)
        if binding is None:
            raise LibraryError('Download and admit the selected Twitch source before preparing a Moment.')
        selected = TwitchClip(**binding['selected_metadata'])
        job = self.jobs.job(binding['job_id'])
        settlement = self.jobs.settlement(binding['job_id'])
        reference = {'id': binding['result_id'], 'sha256': binding['result_sha256']}
        if (selected.id != clip_id or job['kind'] != 'io' or job['state'] != 'completed'
                or job['cancel_requested'] or not job['launch_ref']
                or job['input']['profile_sha256'] != binding['profile_sha256'] or job['input']['assets']
                or binding['result_id'] != job['output_set_id'] or not isinstance(settlement, dict)
                or any(settlement.get(key) != job[field] for key, field in (
                    ('job_id','id'), ('claim_token','claim_token'),
                    ('intent_sha256','intent_sha256'), ('output_set_id','output_set_id')))
                or settlement.get('process_disposition') != 'stopped'
                or settlement.get('outputs_disposition') != 'verified'
                or settlement.get('result_ref') != reference or settlement.get('evidence_ref') != reference):
            raise LibraryError('The saved source is not bound to its completed intake job and verified result.')
        return binding, self.asset(binding['asset_id']), job, settlement

    def create_twitch_moment(self, *, title, source_in_us, source_out_us, kit_id, kit_version,
                            profile_id, scene_key, expected_kit_sha256,
                            expected_binding, expected_asset, verified_source_facts):
        """Atomically attach the chosen clip origin after background artifact/file verification."""
        facts = json.loads(_json(verified_source_facts))
        try:
            duration = facts['duration']
            if not isinstance(duration, str) or len(duration) > 64:
                raise ValueError()
            bound = Fraction(duration) * 1_000_000
            if not 0 < bound <= 180_000_000:
                raise ValueError()
            duration_us = bound.numerator // bound.denominator
        except (TypeError, KeyError, ValueError, ZeroDivisionError) as exc:
            raise LibraryError('Verified source duration is unavailable.') from exc
        if type(source_out_us) is not int or source_out_us > duration_us:
            raise LibraryError('The Moment end exceeds the verified source duration bound.')
        with self._write():
            binding, asset, _, _ = self.admitted_twitch_source(expected_binding['clip_id'])
            if binding != expected_binding or asset != expected_asset:
                raise RevisionConflict('The selected source binding changed. Inspect the source again.')
            record = self.kit_record(kit_id, kit_version)
            kit = self.kit(kit_id, kit_version)
            if record.descriptor_sha256 != expected_kit_sha256:
                raise RevisionConflict('The selected Kit descriptor differs. Select its exact version again.')
            profiles = [p for p in kit['layout_profiles'] if p['id'] == profile_id and p['scene_key'] == scene_key]
            if len(profiles) != 1:
                raise LibraryError('Choose an exact saved OBS scene profile from the selected Kit version.')
            moment_id = self._insert_moment(title=title, source_asset_id=asset.id,
                source_in_us=source_in_us, source_out_us=source_out_us, kit_id=kit_id, kit_version=kit_version)
            self.connection.execute('INSERT INTO moment_twitch_origins VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                moment_id, binding['clip_id'], asset.id, asset.sha256, asset.bytes,
                _json(binding['selected_metadata']), binding['profile_sha256'], binding['result_id'],
                binding['result_sha256'], binding['job_id'], _json(facts), kit_id, kit_version,
                record.descriptor_sha256, profile_id, scene_key))
        return self.moment(moment_id)

    def moment_origin(self, moment_id):
        self.moment(moment_id)
        row = self.connection.execute('SELECT * FROM moment_twitch_origins WHERE moment_id=?', (moment_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for field in ('selected_metadata', 'source_facts'):
            value[field] = json.loads(value[field])
        return value

    @staticmethod
    def _moment(row: sqlite3.Row) -> Moment:
        data = dict(row); data.pop("sequence", None); data["overrides"] = json.loads(data["overrides"])
        return Moment(**data)

    def moment(self, moment_id: str) -> Moment:
        row = self.connection.execute("SELECT * FROM moments WHERE id=?", (moment_id,)).fetchone()
        if row is None:
            raise LibraryError("Unknown moment")
        return self._moment(row)

    def list_moments(self, *, after: int = 0, limit: int = 50) -> dict:
        if type(after) is not int or not 0 <= after <= 2**63 - 1 or type(limit) is not int or not 1 <= limit <= 200:
            raise LibraryError("Invalid pagination: bounded SQLite cursor and 1<=limit<=200 required")
        rows = self.connection.execute("SELECT * FROM moments WHERE sequence>? ORDER BY sequence LIMIT ?", (after, limit + 1)).fetchall()
        more = len(rows) > limit; page = rows[:limit]
        return {"items": [self._moment(row) for row in page], "next_cursor": page[-1]["sequence"] if more else None}

    def effective_properties(self, moment_id: str) -> dict:
        moment = self.moment(moment_id)
        return self.kit(moment.kit_id, moment.kit_version)["properties"] | moment.overrides

    def set_overrides(self, moment_id: str, overrides: dict, *, expected_revision: int) -> Moment:
        overrides = _properties(overrides)
        with self._write():
            moment = self.moment(moment_id)
            if moment.revision != expected_revision:
                raise RevisionConflict("The moment changed; read it again")
            allowed = set(self.kit(moment.kit_id, moment.kit_version)["properties"]) | set(moment.overrides)
            if set(overrides) - allowed:
                raise LibraryError("Unknown local property")
            self.connection.execute("UPDATE moments SET overrides=?,revision=revision+1 WHERE id=?", (_json(overrides), moment_id))
        return self.moment(moment_id)

    def preview_kit_update(self, moment_id: str, target_version: int) -> dict:
        moment = self.moment(moment_id)
        old = self.kit(moment.kit_id, moment.kit_version)["properties"]
        new = self.kit(moment.kit_id, target_version)["properties"]
        conflicts = []
        for key, local in moment.overrides.items():
            changed = (key in old, _json(old.get(key))) != (key in new, _json(new.get(key)))
            if changed and (key not in new or _json(new[key]) != _json(local)):
                conflicts.append({"property": key, "old_present": key in old, "old": old.get(key),
                                  "new_present": key in new, "new": new.get(key), "local": local})
        return {"moment_id": moment.id, "revision": moment.revision, "from_version": moment.kit_version,
                "to_version": target_version, "conflicts": conflicts}

    def apply_kit_update(self, moment_id: str, target_version: int, *, expected_revision: int,
                         resolutions: dict[str, str] | None = None) -> Moment:
        resolutions = dict(resolutions or {})
        with self._write():
            moment = self.moment(moment_id)
            if moment.revision != expected_revision:
                raise RevisionConflict("The moment changed; preview the Kit update again")
            plan = self.preview_kit_update(moment_id, target_version)
            keys = {c["property"] for c in plan["conflicts"]}
            if set(resolutions) - keys or any(v not in {"keep_local", "use_kit"} for v in resolutions.values()):
                raise LibraryError("Resolutions must name current conflicts and keep_local or use_kit")
            if keys - set(resolutions):
                raise KitUpdateConflict(plan["conflicts"])
            overrides = dict(moment.overrides)
            for key, resolution in resolutions.items():
                if resolution == "use_kit":
                    overrides.pop(key)
            self.connection.execute("UPDATE moments SET kit_version=?,overrides=?,revision=revision+1 WHERE id=?",
                                    (target_version, _json(overrides), moment_id))
        return self.moment(moment_id)
