
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
