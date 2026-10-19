from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from native.core.accepted_transcript import TranscriptError
from native.core.creator_library import CreatorLibrary


def old_library(path):
    schema = (Path(__file__).parent / "fixtures/creator-library-v1.sql").read_text(encoding="utf8")
    with closing(sqlite3.connect(path)) as old:
        old.executescript(schema)
        old.execute("INSERT INTO assets VALUES(?,?,?,?,?)", ("old-asset", "a" * 64, "X:/source.mp4", "x:/source.mp4", 100))
        old.execute("INSERT INTO kits VALUES('old-kit',1)")
        old.execute("INSERT INTO kit_versions VALUES('old-kit',1,?)", ('{"name":"My existing Kit"}',))
        old.commit()


def test_caption_migration_preserves_old_catalog_and_uses_its_connection(tmp_path):
    path = tmp_path / "library.sqlite"
    old_library(path)
    with CreatorLibrary(path) as library:
        assert library.captions.connection is library.connection
        assert library.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert library.connection.execute("SELECT version FROM caption_schema").fetchone()[0] == 1
        assert library.captions.list_accepted()["items"] == []
        assert library.asset("old-asset").path == "X:/source.mp4"
        assert library.kit("old-kit", 1) == {"name": "My existing Kit"}
    with CreatorLibrary(path) as library:
        assert library.asset("old-asset").sha256 == "a" * 64
        assert library.captions.list_accepted()["items"] == []


def test_failed_additive_migration_rolls_back_and_can_retry_without_catalog_loss(tmp_path):
    path = tmp_path / "library.sqlite"
    old_library(path)
    original = Path.read_text

    def broken_ddl(file, *args, **kwargs):
        if file.name == "caption_schema.sql":
            return ("CREATE TABLE caption_schema(singleton INTEGER PRIMARY KEY,version INTEGER);\n"
                    "INSERT INTO caption_schema VALUES(1,1);\n"
                    "CREATE TABLE caption_partial(value INTEGER);\n"
                    "THIS IS INVALID SQL;\n")
        return original(file, *args, **kwargs)

    with patch.object(Path, "read_text", broken_ddl):
        with pytest.raises(sqlite3.OperationalError):
            CreatorLibrary(path)
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT count(*) FROM assets").fetchone()[0] == 1
        assert raw.execute("SELECT name FROM sqlite_master WHERE name LIKE 'caption_%'").fetchall() == []
    with CreatorLibrary(path) as library:
        assert library.kit("old-kit", 1) == {"name": "My existing Kit"}
        assert library.captions.list_accepted()["items"] == []


def test_unknown_caption_schema_preserves_existing_data_and_rejects_open(tmp_path):
    path = tmp_path / "library.sqlite"
    old_library(path)
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("CREATE TABLE caption_schema(singleton INTEGER PRIMARY KEY,version INTEGER)")
        raw.execute("INSERT INTO caption_schema VALUES(1,2)")
        raw.commit()
    with pytest.raises(TranscriptError, match="Unsupported caption schema"):
        CreatorLibrary(path)
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT version FROM caption_schema").fetchone()[0] == 2
        assert raw.execute("SELECT descriptor FROM kit_versions").fetchone()[0] == '{"name":"My existing Kit"}'


def test_library_close_owns_caption_connection_lifetime(tmp_path):
    library = CreatorLibrary(tmp_path / "library.sqlite")
    captions = library.captions
    library.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        captions.list_accepted()
