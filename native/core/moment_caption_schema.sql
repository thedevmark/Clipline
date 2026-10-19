-- Explicit proposal choices. Words, jobs and accepted revisions retain their existing owners.
CREATE TABLE moment_caption_schema (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL
);
INSERT INTO moment_caption_schema VALUES(1,1);
CREATE TABLE moment_caption_selections (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 moment_id TEXT NOT NULL REFERENCES moments(id), moment_revision INTEGER NOT NULL CHECK(moment_revision>0),
 selection_revision INTEGER NOT NULL CHECK(selection_revision>0),
 job_id TEXT NOT NULL REFERENCES durable_job_intents(id),
 transcript_id TEXT NOT NULL REFERENCES caption_transcripts(id),
 hypothesis_id TEXT NOT NULL REFERENCES caption_proposals(id),
 alignment_id TEXT REFERENCES caption_proposals(id),
 source_asset_id TEXT NOT NULL REFERENCES assets(id), source_sha256 TEXT NOT NULL,
 kit_id TEXT NOT NULL, kit_version INTEGER NOT NULL, kit_descriptor_sha256 TEXT NOT NULL,
 request_id TEXT NOT NULL, request_sha256 TEXT NOT NULL, profile_sha256 TEXT NOT NULL,
 result_id TEXT NOT NULL, result_sha256 TEXT NOT NULL, receipt_sha256 TEXT NOT NULL,
 UNIQUE(moment_id,moment_revision,selection_revision),
 FOREIGN KEY(kit_id,kit_version) REFERENCES kit_versions(kit_id,version)
);
CREATE TRIGGER moment_caption_selections_no_update BEFORE UPDATE ON moment_caption_selections
 BEGIN SELECT RAISE(ABORT,'Moment caption choices are immutable'); END;
CREATE TRIGGER moment_caption_selections_no_delete BEFORE DELETE ON moment_caption_selections
 BEGIN SELECT RAISE(ABORT,'Moment caption choices are immutable'); END;
CREATE TRIGGER moment_caption_schema_no_update BEFORE UPDATE ON moment_caption_schema
 BEGIN SELECT RAISE(ABORT,'Moment caption schema is immutable'); END;
CREATE TRIGGER moment_caption_schema_no_delete BEFORE DELETE ON moment_caption_schema
 BEGIN SELECT RAISE(ABORT,'Moment caption schema is immutable'); END;
