-- Standalone pre-origin CreatorLibrary schema3. Contains no user data or Moment origin table.
PRAGMA foreign_keys=ON;
BEGIN IMMEDIATE;

-- catalog v1

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

-- Twitch v2

CREATE TABLE twitch_clips (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL UNIQUE,
 metadata TEXT NOT NULL
);
PRAGMA user_version=2;

-- caption domain
-- Additive domain migration. CreatorLibrary remains the catalog and user_version owner.
CREATE TABLE caption_schema (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL);
INSERT INTO caption_schema VALUES(1,1);
CREATE TABLE caption_transcripts (
 id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id),
 source_sha256 TEXT NOT NULL, identity_json TEXT NOT NULL, created_utc TEXT NOT NULL
);
CREATE TABLE caption_proposals (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
 transcript_id TEXT NOT NULL REFERENCES caption_transcripts(id), kind TEXT NOT NULL,
 target_id TEXT REFERENCES caption_proposals(id), body_sha256 TEXT NOT NULL, body_json TEXT NOT NULL
);
CREATE INDEX caption_proposals_transcript ON caption_proposals(transcript_id,sequence);
CREATE TABLE caption_revisions (
 transcript_id TEXT NOT NULL REFERENCES caption_transcripts(id), revision INTEGER NOT NULL CHECK(revision>0),
 parent_revision INTEGER, words_json TEXT NOT NULL, created_utc TEXT NOT NULL,
 PRIMARY KEY(transcript_id,revision),
 FOREIGN KEY(transcript_id,parent_revision) REFERENCES caption_revisions(transcript_id,revision)
);
CREATE TABLE caption_heads (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, transcript_id TEXT NOT NULL UNIQUE,
 revision INTEGER NOT NULL,
 FOREIGN KEY(transcript_id,revision) REFERENCES caption_revisions(transcript_id,revision)
);
CREATE TABLE caption_events (
 id TEXT PRIMARY KEY, transcript_id TEXT NOT NULL, expected_revision INTEGER NOT NULL,
 resulting_revision INTEGER NOT NULL, event_json TEXT NOT NULL,
 UNIQUE(transcript_id,resulting_revision),
 FOREIGN KEY(transcript_id,resulting_revision) REFERENCES caption_revisions(transcript_id,revision)
);
CREATE TRIGGER caption_transcripts_no_update BEFORE UPDATE ON caption_transcripts BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_transcripts_no_delete BEFORE DELETE ON caption_transcripts BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_proposals_no_update BEFORE UPDATE ON caption_proposals BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_proposals_no_delete BEFORE DELETE ON caption_proposals BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_revisions_no_update BEFORE UPDATE ON caption_revisions BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_revisions_no_delete BEFORE DELETE ON caption_revisions BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_events_no_update BEFORE UPDATE ON caption_events BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_events_no_delete BEFORE DELETE ON caption_events BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_schema_no_update BEFORE UPDATE ON caption_schema BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;
CREATE TRIGGER caption_schema_no_delete BEFORE DELETE ON caption_schema BEGIN SELECT RAISE(ABORT,'caption evidence is immutable'); END;

-- durable job domain
CREATE TABLE durable_job_schema(singleton INTEGER PRIMARY KEY CHECK(singleton=1),version INTEGER NOT NULL);
INSERT INTO durable_job_schema VALUES(1,1);
CREATE TABLE durable_job_intents(sequence INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,idempotency TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,intent_json TEXT NOT NULL,intent_sha256 TEXT NOT NULL,output_set_id TEXT NOT NULL UNIQUE,created_utc TEXT NOT NULL);
CREATE TABLE durable_job_heads(job_id TEXT PRIMARY KEY REFERENCES durable_job_intents(id),state TEXT NOT NULL CHECK(state IN ('queued','running','cancel-requested','completed','failed','cancelled','interrupted')),revision INTEGER NOT NULL CHECK(revision>0),claim_token TEXT,owner_id TEXT,cancel_requested INTEGER NOT NULL CHECK(cancel_requested IN(0,1)),launch_ref TEXT);
CREATE INDEX durable_job_state ON durable_job_heads(state);
CREATE TABLE durable_job_events(job_id TEXT NOT NULL REFERENCES durable_job_intents(id),revision INTEGER NOT NULL,state TEXT NOT NULL,detail_json TEXT NOT NULL,created_utc TEXT NOT NULL,PRIMARY KEY(job_id,revision));
CREATE TABLE durable_job_assets(job_id TEXT NOT NULL REFERENCES durable_job_intents(id),asset_id TEXT NOT NULL REFERENCES assets(id),sha256 TEXT NOT NULL,PRIMARY KEY(job_id,asset_id));
CREATE TABLE durable_job_kits(job_id TEXT PRIMARY KEY REFERENCES durable_job_intents(id),kit_id TEXT NOT NULL,version INTEGER NOT NULL,FOREIGN KEY(kit_id,version) REFERENCES kit_versions(kit_id,version));
CREATE TABLE durable_job_transcripts(job_id TEXT PRIMARY KEY REFERENCES durable_job_intents(id),transcript_id TEXT NOT NULL,revision INTEGER NOT NULL,FOREIGN KEY(transcript_id,revision) REFERENCES caption_revisions(transcript_id,revision));
CREATE TABLE durable_job_slots(resource TEXT NOT NULL,slot INTEGER NOT NULL,CHECK((resource='heavy' AND slot=0) OR (resource='io' AND slot IN(0,1))),PRIMARY KEY(resource,slot));
INSERT INTO durable_job_slots VALUES('heavy',0),('io',0),('io',1);
CREATE TABLE durable_job_holds(resource TEXT NOT NULL,slot INTEGER NOT NULL,job_id TEXT NOT NULL REFERENCES durable_job_intents(id),claim_token TEXT NOT NULL,PRIMARY KEY(resource,slot),UNIQUE(job_id,resource),FOREIGN KEY(resource,slot) REFERENCES durable_job_slots(resource,slot));
CREATE TABLE durable_job_settlements(job_id TEXT PRIMARY KEY REFERENCES durable_job_intents(id),claim_token TEXT NOT NULL,attestation_json TEXT NOT NULL);
CREATE TRIGGER durable_job_schema_no_update BEFORE UPDATE ON durable_job_schema BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_schema_no_delete BEFORE DELETE ON durable_job_schema BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_intents_no_update BEFORE UPDATE ON durable_job_intents BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_intents_no_delete BEFORE DELETE ON durable_job_intents BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_events_no_update BEFORE UPDATE ON durable_job_events BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_events_no_delete BEFORE DELETE ON durable_job_events BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_assets_no_update BEFORE UPDATE ON durable_job_assets BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_assets_no_delete BEFORE DELETE ON durable_job_assets BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_kits_no_update BEFORE UPDATE ON durable_job_kits BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_kits_no_delete BEFORE DELETE ON durable_job_kits BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_transcripts_no_update BEFORE UPDATE ON durable_job_transcripts BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_transcripts_no_delete BEFORE DELETE ON durable_job_transcripts BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_slots_no_update BEFORE UPDATE ON durable_job_slots BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_slots_no_delete BEFORE DELETE ON durable_job_slots BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_settlements_no_update BEFORE UPDATE ON durable_job_settlements BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;
CREATE TRIGGER durable_job_settlements_no_delete BEFORE DELETE ON durable_job_settlements BEGIN SELECT RAISE(ABORT,'durable job evidence is immutable'); END;

-- Twitch source v3

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

COMMIT;
