"""Shared asset identity, immutable Kit versions, and lightweight moment metadata.

No operation decodes media or implements an editing timeline. Property names are
semantic keys such as ``caption.font_size``; each value is one merge unit.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any


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
            elif version != 1:
                raise LibraryError(f"Unsupported library schema version {version}")
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            self.connection.close()
            raise
        self.connection.execute("PRAGMA journal_mode=WAL")

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

    def asset(self, asset_id: str) -> Asset:
        row = self.connection.execute("SELECT id,sha256,path,bytes FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise LibraryError(f"Unknown asset {asset_id}")
        return Asset(**dict(row))

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
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise LibraryError("Invalid pagination: cursor>=0 and 1<=limit<=200 required")
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

    def kit(self, kit_id: str, version: int) -> dict:
        row = self.connection.execute("SELECT descriptor FROM kit_versions WHERE kit_id=? AND version=?", (kit_id, version)).fetchone()
        if row is None:
            raise LibraryError("Unknown Kit version")
        return json.loads(row["descriptor"])

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
        title = _name(title, "moment title")
        if type(source_in_us) is not int or type(source_out_us) is not int or not 0 <= source_in_us < source_out_us:
            raise LibraryError("Source interval requires ordered nonnegative integer microseconds")
        overrides = _properties({} if overrides is None else overrides)
        moment_id = str(uuid.uuid4())
        with self._write():
            self.asset(source_asset_id); kit = self.kit(kit_id, kit_version)
            if set(overrides) - set(kit["properties"]):
                raise LibraryError("Local overrides must name existing Kit properties")
            self.connection.execute("INSERT INTO moments(id,title,source_asset_id,source_in_us,source_out_us,kit_id,kit_version,overrides) VALUES (?,?,?,?,?,?,?,?)",
                                    (moment_id, title, source_asset_id, source_in_us, source_out_us, kit_id, kit_version, _json(overrides)))
        return self.moment(moment_id)

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
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise LibraryError("Invalid pagination: cursor>=0 and 1<=limit<=200 required")
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
