"""Real gated Python protocol child; no native engine, codec or media runs."""
import json
import os
from pathlib import Path
import sys

import pytest
import native.services.native_export_client as runtime_module
from native.core.creator_library import CreatorLibrary
from native.services.caption_worker.evidence import hash_file
from native.services.durable_native_exports import DurableNativeExports, ExportJobError
from native.services.native_export_client import NativeExportClient, QualifiedProgram
from tests.test_native_exports import case, owner, submit, background

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows JobObject/read-guard boundary")


def test_render_deadline_with_proven_cleanup_is_failed_and_releases_holds(case, monkeypatch):
    root = case["root"]
    marker = root / "render-entered"
    script = root / "gated-protocol-fixture.py"
    script.write_text('''import json, pathlib, sys, threading
def emit(value):
    print(json.dumps(value), flush=True)
if sys.stdin.buffer.readline() != b"start\\n":
    raise SystemExit(125)
ready = {"revision": 17, "ownerMetadataSHA256": "a" * 64,
         "validation": {"owners": [{"owner": 5, "linksRevision": 0,
             "sequences": {"landscape": 13, "portrait": 21}}]}}
emit({"event": "ready", "observation": ready})
params = json.loads(sys.stdin.buffer.readline())
capture = {"snapshotSHA256": params["expectedSnapshotSHA256"],
           "recipeSHA256": params["expectedRecipeSHA256"],
           "ownerMetadataSHA256": ready["ownerMetadataSHA256"]}
emit({"event": "captured", "capture": capture})
if sys.stdin.buffer.readline() != b"render\\n":
    raise SystemExit(126)
pathlib.Path(sys.argv[1]).write_text("render entered", encoding="utf8")
threading.Event().wait(30)
''', encoding="utf8")
    python = QualifiedProgram(Path(sys.executable), hash_file(sys.executable))
    unused = case["client"].ffprobe
    case["client"] = NativeExportClient(python, unused, unused, python, script,
        engine_version="0.9.0-test-fixture", source_pin_sha256="a" * 64)
    actual_process = runtime_module.OwnedProcess
    captures = []

    def protocol_child(argv, directory, cancel, deadline, **kwargs):
        assert Path(argv[0]) == python.path
        child = actual_process([sys.executable, "-I", "-B", str(script), str(marker)],
            directory, cancel, deadline, **kwargs)
        captures.append(child)
        return child

    monkeypatch.setattr(runtime_module, "OwnedProcess", protocol_child)
    # Only the opaque native inspection boundary is authored. Hash/capture
    # bindings, process ownership and real SQLite settlement remain production.
    monkeypatch.setattr(DurableNativeExports, "_native", lambda *args: None)
    paths = [case["snapshot"].path, Path(case["source"].path), Path(case["template"].path)]
    original = [path.read_bytes() for path in paths]
    with CreatorLibrary(case["database"]) as library:
        job = submit(case, owner(case, library))
    try:
        result = background(case, timeout=2, retain_owner=True)
        assert marker.read_text(encoding="utf8") == "render entered"
        assert len(captures) == 1
        child = captures[0]
        proof = result.execution.stop()
        assert child.closed and child.tree.job is None
        assert child.process.poll() is not None
        assert all(not thread.is_alive() for thread in child.threads)
        assert proof["closed"] and proof["cleanup_errors"] == []
        assert proof["scopes"] and all(scope["disposition"] == "stopped"
            and scope["active_processes"] == 0 and scope["streams_closed"]
            for scope in proof["scopes"])
        assert result.execution.closed and not result.execution.guards
        runtime_dir = root / "jobs" / "executions" / job["output_set_id"]
        receipt = json.loads((runtime_dir / "runtime.json").read_text(encoding="utf8"))
        assert "deadline expired" in receipt["fault"]
        assert receipt["state"] == "failed"
        assert receipt["process_disposition"] == "stopped" and receipt["quarantined"]
        quarantine = json.loads((runtime_dir / "QUARANTINE.json").read_text(encoding="utf8"))
        assert quarantine["export_not_deliverable"] and quarantine["state"] == "failed"
        assert result.job["state"] == "failed"
        with CreatorLibrary(case["database"]) as library:
            runner = owner(case, library)
            assert library.jobs.resources() == []
            settlement = library.jobs.settlement(job["id"])
            assert settlement["outputs_disposition"] == "quarantined"
            assert settlement["process_disposition"] == "stopped"
            assert library.jobs.job(job["id"])["state"] == "failed"
            with pytest.raises(ExportJobError, match="Only settled completed"):
                runner.completed_receipt(job["id"])
        assert not list((runtime_dir / "media").glob("*.mp4"))
        assert [path.read_bytes() for path in paths] == original
    finally:
        # Cleanup only the exact fixture owners, without PID or receipt adoption.
        for child in captures:
            if not child.closed:
                child.execution_owner.stop()
