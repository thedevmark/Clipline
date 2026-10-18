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
