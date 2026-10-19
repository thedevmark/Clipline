"""Read authored vocabulary from one immutable canonical Kit version."""
import hashlib
import json

from native.services.caption_worker.evidence import (
    EvidenceError, digest, validate_vocabulary,
)


def kit_vocabulary(record):
    raw = record.descriptor_json.encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != record.descriptor_sha256:
        raise EvidenceError("VOCABULARY_MISMATCH", "Canonical Kit descriptor hash differs")
    terms = json.loads(raw).get("vocabulary", [])
    if terms == []:
        return None
    value = {
        "schema": 1, "kit_id": record.kit_id, "version": record.version,
        "descriptor_sha256": record.descriptor_sha256,
        "vocabulary_sha256": digest(terms), "terms": terms,
    }
    validate_vocabulary(value, {"kit_id": record.kit_id, "version": record.version})
    return value


def verify_kit_vocabulary(library, request):
    value = request.get("vocabulary_revision")
    if value is None:
        return  # Legacy requests intentionally have no hints.
    pin = request.get("kit_revision")
    validate_vocabulary(value, pin)
    record = library.kit_record(pin["kit_id"], pin["version"])
    if kit_vocabulary(record) != value:
        raise EvidenceError("VOCABULARY_MISMATCH", "Hints differ from the exact saved Kit version")
