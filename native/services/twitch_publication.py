"""Trusted local publication context; owns no transfer, probe, or process policy."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import uuid

from native.core.creator_library import FingerprintMismatch, LibraryError, _json
from native.core.durable_jobs import _reference, _sha
from native.services.caption_audio import _source_read_guard
from native.services.caption_worker.evidence import hash_file


WINDOWS_INTAKE_ENV = ("SystemRoot", "WINDIR", "SystemDrive", "TEMP", "TMP", "LOCALAPPDATA")


class PublicationGuardError(LibraryError):
    def __init__(self, publication):
        self.publication = publication
        super().__init__("Publication read guard release is unconfirmed; retain this context and retry explicit cleanup")


def intake_environment(parent):
    """Explicit OS-only child mapping; OwnedProcess adds its fixed thread limits."""
    folded = {key.upper(): value for key, value in parent.items()}
    return {key: folded[key.upper()] for key in WINDOWS_INTAKE_ENV if key.upper() in folded}


class VerifiedTwitchPublication:
    """Keep independent source guards through the completed SQL settlement.

    Construct only after the trusted runtime has verified transfer/probe and
    process cessation. This verifies bytes again; it cannot certify probe facts
    or replace DurableJobs' runtime attestation verifier.
    """
    def __init__(self, library, *, clip, job, result_ref, profile_sha256,
                 media_path, expected_sha256, expected_bytes):
        self.library = library
        self.guards = []
        self.closed = False
        self.publication_revoked = False
        self.plan = None
        _reference(result_ref); _sha(profile_sha256); _sha(expected_sha256)
        if type(expected_bytes) is not int or not 0 < expected_bytes <= 2**63-1:
            raise LibraryError("Verified source size required")
        try:
            path = Path(media_path).resolve(strict=True)
            self._hold_verified(path, expected_sha256, expected_bytes)
            row = library.connection.execute("SELECT * FROM assets WHERE sha256=?", (expected_sha256,)).fetchone()
            existing = dict(row) if row is not None else None
            if existing is not None:
                self._hold_verified(Path(existing["path"]), existing["sha256"], existing["bytes"])
            self.plan = json.loads(_json({
                "job": job, "clip": asdict(clip), "profile_sha256": profile_sha256,
                "result_ref": result_ref, "existing": existing,
                "media": {"id": str(uuid.uuid4()), "sha256": expected_sha256, "path": str(path),
                          "path_key": os.path.normcase(str(path)), "bytes": expected_bytes}}))
        except BaseException:
            self.close()
            raise

    def _hold_verified(self, path, sha256, size):
        if not path.is_absolute() or not path.is_file():
            raise LibraryError("Verified source must be an absolute regular file")
        guard = _source_read_guard(path)
        if guard is None:
            raise LibraryError("Windows publication read guard required")
        self.guards.append(guard)
        if path.stat().st_size != size or hash_file(path) != sha256:
            raise FingerprintMismatch("Publication source changed")

    def commit(self, connection, current_job, attestation):
        if self.publication_revoked or self.closed or self.plan is None:
            raise LibraryError("Publication guard context is closed")
        self.library._commit_twitch_source(connection, current_job, attestation, self.plan)

    def close(self):
        # Any cleanup attempt ends publication eligibility, even if only some
        # guards close. Remaining handles belong to explicit cleanup only.
        self.publication_revoked = True
        failed = False
        for guard in list(self.guards):
            try:
                if guard is not None and not guard[0].CloseHandle(guard[1]):
                    failed = True
                    continue
                self.guards.remove(guard)
            except Exception:
                failed = True
        if failed:
            raise PublicationGuardError(self)
        self.closed = True

    def __enter__(self):
        if self.publication_revoked or self.closed:
            raise LibraryError("Publication guard context is closed")
        return self

    def __exit__(self, *_):
        self.close()
