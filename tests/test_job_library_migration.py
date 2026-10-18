from contextlib import closing
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from native.core.creator_library import CreatorLibrary
from native.core.durable_jobs import JobError


def old_library(path):
    schema = (Path(__file__).parent / "fixtures/creator-library-v1.sql").read_text(encoding="utf8")
    with closing(sqlite3.connect(path)) as old:
        old.executescript(schema)
        old.execute("INSERT INTO assets VALUES(?,?,?,?,?)", ("old-asset", "a" * 64, "X:/source.mp4", "x:/source.mp4", 100))
        old.commit()


def test_job_migration_preserves_catalog_and_caption_domain(tmp_path):
    path = tmp_path / "library.sqlite"
    old_library(path)
    with CreatorLibrary(path) as library:
        assert library.jobs.c is library.connection
        assert library.jobs.verifier is None
        assert library.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert library.connection.execute("SELECT version FROM caption_schema").fetchone()[0] == 1
        assert library.connection.execute("SELECT version FROM durable_job_schema").fetchone()[0] == 1
        assert library.asset("old-asset").path == "X:/source.mp4"
        assert library.jobs.list_jobs()["items"] == []
    with CreatorLibrary(path) as library:
        assert library.asset("old-asset").sha256 == "a" * 64
        assert library.jobs.resources() == []


def test_failed_job_migration_rolls_back_and_retries_without_catalog_loss(tmp_path):
    path = tmp_path / "library.sqlite"
    old_library(path)
    original = Path.read_text

    def broken_ddl(file, *args, **kwargs):
        if file.name == "durable_jobs_schema.sql":
            return ("CREATE TABLE durable_job_schema(singleton INTEGER PRIMARY KEY,version INTEGER);\n"
                    "INSERT INTO durable_job_schema VALUES(1,1);\n"
                    "CREATE TABLE durable_job_partial(value INTEGER);\n"
                    "THIS IS INVALID SQL;\n")
        return original(file, *args, **kwargs)

    with patch.object(Path, "read_text", broken_ddl):
        with pytest.raises(sqlite3.OperationalError):
            CreatorLibrary(path)
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT count(*) FROM assets").fetchone()[0] == 1
        assert raw.execute("SELECT name FROM sqlite_master WHERE name LIKE 'durable_job_%'").fetchall() == []
        assert raw.execute("SELECT version FROM caption_schema").fetchone()[0] == 1
    with CreatorLibrary(path) as library:
        assert library.jobs.list_jobs()["items"] == []


def test_unknown_job_schema_preserves_catalog_and_rejects_open(tmp_path):
    path = tmp_path / "library.sqlite"
    old_library(path)
    with closing(sqlite3.connect(path)) as raw:
        raw.execute("CREATE TABLE durable_job_schema(singleton INTEGER PRIMARY KEY,version INTEGER)")
        raw.execute("INSERT INTO durable_job_schema VALUES(1,2)")
        raw.commit()
    with pytest.raises(JobError, match="Unsupported job schema"):
        CreatorLibrary(path)
    with closing(sqlite3.connect(path)) as raw:
        assert raw.execute("SELECT version FROM durable_job_schema").fetchone()[0] == 2
        assert raw.execute("SELECT count(*) FROM assets").fetchone()[0] == 1


def test_library_close_owns_job_connection(tmp_path):
    library = CreatorLibrary(tmp_path / "library.sqlite")
    jobs = library.jobs
    library.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        jobs.list_jobs()
