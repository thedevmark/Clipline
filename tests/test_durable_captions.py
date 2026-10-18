import copy
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
import wave
from unittest.mock import patch

import pytest

from native.core.creator_library import CreatorLibrary
from native.core.durable_jobs import VerificationRequired
from native.services.caption_audio import PreparedAudio
from native.services.caption_worker.client import CaptionClient, write_once
from native.services.caption_worker.evidence import digest, hash_file
from native.services.durable_captions import CaptionJobError, DurableCaptionRunner


pytestmark=pytest.mark.skipif(os.name!="nt",reason="Qualified Windows process settlement")
ROOT=Path(__file__).resolve().parents[1]


@pytest.fixture
def case(tmp_path):
    with CreatorLibrary(tmp_path/"library.sqlite") as library:
        audio=tmp_path/"fixture.wav"
        with wave.open(str(audio),"wb") as stream:
            stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(16000)
            stream.writeframes(b"\0\0"*160)
        asset=library.register_asset(audio)
        profile=tmp_path/"profile.json";profile.write_text("{}")
        request={"schema_version":1,"run_id":str(uuid.uuid4()),"task":"transcribe","language":"en",
                 "profile_path":str(profile),"profile_sha256":hash_file(profile),
                 "source":{"asset_id":asset.id,"path":asset.path,"sha256":asset.sha256,
                           "audio_stream_index":0,"channels":[0],"mix":"unknown"},
                 "audio":{"path":str(audio),"wav_sha256":hash_file(audio),
                          "pcm_sha256":__import__("hashlib").sha256(b"\0\0"*160).hexdigest(),
                          "sample_rate":16000,"sample_width":2,"sample_count":160,"channels":1,
                          "sample_zero_source_us":0,"extraction":{"source_sha256":asset.sha256,"argv":["fixture"],"tool_version":"authored fixture"}},
                 "range":{"start_us":0,"end_us":10000}}
        prepared_dir=tmp_path/"prepared";prepared_dir.mkdir()
        write_once(prepared_dir/"request.json",request)
        write_once(prepared_dir/"receipt.json",{"request_sha256":digest(request)})
        prepared=PreparedAudio("complete",None,prepared_dir,prepared_dir/"request.json")
        transcript=library.captions.create({k:request[k] for k in ("source","audio","range")})
        def client(mode="success"):
            return CaptionClient(sys.executable,ROOT/"native/services/caption_worker/worker.py",
                command=[sys.executable,"-u","-B","-S",str(ROOT/"tests/fixtures/caption_fake_worker.py"),mode])
        runner=DurableCaptionRunner(library,tmp_path/"jobs",client())
        yield library,runner,prepared,transcript,request,client


def submit(case,key=None):
    library,runner,prepared,tid,request,client=case
    return runner.submit(prepared,transcript_id=tid,idempotency_key=key or str(uuid.uuid4()))


def test_actual_worker_completes_releases_slots_and_imports_only_proposals(case):
    library,runner,prepared,tid,request,client=case
    job=submit(case);done=runner.run_next(timeout=10)
    assert done["id"]==job["id"] and done["state"]=="completed"
    assert runner.jobs.resources()==[]
    settlement=runner.jobs.settlement(job["id"])
    assert settlement["process_disposition"]=="stopped"
    assert settlement["outputs_disposition"]=="verified"
    imported=runner.import_completed(job["id"])
    assert runner.import_completed(job["id"])==imported
    assert library.captions.head(tid)==0
    assert len(library.captions.list_proposals(tid)["items"])==2
    with CreatorLibrary(runner.database) as reopened:
        assert reopened.jobs.job(job["id"])["state"]=="completed"
        assert reopened.captions.head(tid)==0


def test_same_idempotency_key_reuses_exact_request_and_terminal_job(case):
    library,runner,prepared,tid,request,client=case
    key=str(uuid.uuid4());job=submit(case,key)
    assert submit(case,key)==job
    done=runner.run_next(timeout=10)
    assert submit(case,key)==done
    assert runner.run_next(timeout=10) is None


def test_changed_request_reference_conflicts_without_replacing_original(case):
    library,runner,prepared,tid,request,client=case
    key=str(uuid.uuid4());job=submit(case,key)
    other=copy.deepcopy(request);other["run_id"]=str(uuid.uuid4())
    # A changed prepared request is an explicit new operation, even with identical PCM.
    other_dir=prepared.directory.parent/"other-prepared";other_dir.mkdir()
    write_once(other_dir/"request.json",other);write_once(other_dir/"receipt.json",{"request_sha256":digest(other)})
    altered=PreparedAudio("complete",None,other_dir,other_dir/"request.json")
    with pytest.raises(CaptionJobError):runner.submit(altered,transcript_id=tid,idempotency_key=key)
    assert runner.jobs.job(job["id"])==job


@pytest.mark.parametrize("mode",["crash","partial","false_complete","malformed"])
def test_failed_worker_output_is_quarantined_and_cannot_import(case,mode):
    library,runner,prepared,tid,request,client=case
    runner.client=client(mode);job=submit(case);done=runner.run_next(timeout=10)
    assert done["state"]=="failed" and runner.jobs.resources()==[]
    assert runner.jobs.settlement(job["id"])["outputs_disposition"]=="quarantined"
    with pytest.raises(CaptionJobError):runner.import_completed(job["id"])
    assert library.captions.head(tid)==0


def test_changed_source_fails_before_worker_launch_and_releases_claim(case):
    library,runner,prepared,tid,request,client=case
    job=submit(case);Path(request["source"]["path"]).write_bytes(b"changed")
    done=runner.run_next(timeout=10)
    assert done["state"]=="failed" and done["launch_ref"] is None
    assert runner.jobs.resources()==[]
    assert runner.jobs.settlement(job["id"])["process_disposition"]=="never_started"


def test_changed_immutable_request_fails_before_launch_without_releasing_other_work(case):
    library,runner,prepared,tid,request,client=case
    job=submit(case)
    path=runner.directory/"requests"/(job["input"]["request_ref"]["id"]+".json")
    path.write_text("{}")
    done=runner.run_next(timeout=10)
    assert done["state"]=="failed" and done["launch_ref"] is None
    assert runner.jobs.resources()==[]


def test_unsupported_job_kinds_are_not_claimed(case):
    library,runner,prepared,tid,request,client=case
    io=library.jobs.submit("io",{"request_ref":{"id":str(uuid.uuid4()),"sha256":"a"*64}},idempotency_key=str(uuid.uuid4()))
    caption=submit(case)
    done=runner.run_next(timeout=10)
    assert done["id"]==caption["id"] and done["state"]=="completed"
    assert library.jobs.job(io["id"])["state"]=="queued"


def test_preexisting_interrupted_job_is_not_reclaimed_by_new_controller(case):
    library,runner,prepared,tid,request,client=case
    job=submit(case);claimed=library.jobs.claim_next(owner_id=str(uuid.uuid4()))
    library.jobs.mark_interrupted(job["id"],expected_revision=claimed["revision"],reason="restart")
    another=DurableCaptionRunner(library,runner.directory,client())
    assert another.run_next(timeout=10) is None
    with pytest.raises(VerificationRequired):
        another.jobs.reconcile(job["id"],expected_revision=claimed["revision"]+1,claim_token=claimed["claim_token"],outcome="failed",evidence_ref={"id":str(uuid.uuid4()),"sha256":"a"*64})
    assert len(library.jobs.resources())==2


def test_unknown_runtime_exception_retains_interrupted_resource_holds(case):
    library,runner,prepared,tid,request,client=case
    class UncertainClient:
        command=["fixture"]
        def run(self,*args,**kwargs):raise OSError("lost controller outcome")
    runner.client=UncertainClient();job=submit(case)
    done=runner.run_next(timeout=10)
    assert done["state"]=="interrupted" and len(runner.jobs.resources())==2
    assert runner.jobs.settlement(job["id"]) is None


def test_catalog_cancel_stops_actual_worker_before_releasing_slots(case):
    library,runner,prepared,tid,request,client=case
    runner.client=client("phase");job=submit(case);observations=[];errors=[]
    def cancel_from_other_owner():
        try:
            with CreatorLibrary(runner.database) as other:
                deadline=time.monotonic()+15
                while time.monotonic()<deadline:
                    current=other.jobs.job(job["id"])
                    log=runner.directory/"executions"/current["output_set_id"]/request["run_id"]/"stderr.log"
                    if log.exists() and b"phase=waiting" in log.read_bytes():break
                    time.sleep(.02)
                else:raise AssertionError("Worker did not reach readiness")
                cancelled=other.jobs.request_cancel(job["id"],expected_revision=current["revision"])
                observations.append((cancelled["state"],len(other.jobs.resources())))
        except BaseException as exc:errors.append(exc)
    thread=threading.Thread(target=cancel_from_other_owner);thread.start()
    try:done=runner.run_next(timeout=30)
    finally:thread.join(20)
    assert not thread.is_alive() and not errors
    assert observations==[("cancel-requested",2)]
    assert done["state"]=="cancelled" and runner.jobs.resources()==[]
    assert runner.jobs.settlement(job["id"])["process_disposition"]=="stopped"


def test_changed_completed_output_is_not_imported_or_accepted(case):
    library,runner,prepared,tid,request,client=case
    job=submit(case);runner.run_next(timeout=10)
    path=runner.directory/"executions"/job["output_set_id"]/request["run_id"]/"hypothesis.json"
    path.write_text("{}")
    with pytest.raises(CaptionJobError):runner.import_completed(job["id"])
    assert library.captions.list_proposals(tid)["items"]==[]
    assert library.captions.head(tid)==0


def test_invalid_deadline_does_not_claim_a_job(case):
    library,runner,prepared,tid,request,client=case
    job=submit(case)
    for timeout in (0,float("nan"),True,901):
        with pytest.raises(CaptionJobError):runner.run_next(timeout=timeout)
    assert runner.jobs.job(job["id"])==job and runner.jobs.resources()==[]


def test_unknown_windows_scope_retains_holds_after_actual_worker_stops(case):
    from native.services.caption_worker.process_tree import ProcessTree
    original=ProcessTree.stop
    def uncertain(self):
        proof=original(self);proof["disposition"]="unknown";return proof
    library,runner,prepared,tid,request,client=case
    job=submit(case)
    with patch.object(ProcessTree,"stop",uncertain):done=runner.run_next(timeout=10)
    assert done["state"]=="interrupted" and len(runner.jobs.resources())==2
    assert runner.jobs.settlement(job["id"]) is None


def test_cancel_during_final_verification_cannot_publish_completed_result(case):
    library,runner,prepared,tid,request,client=case
    job=submit(case);verify=runner.jobs.verifier
    def concurrent_cancel(current,evidence):
        proof=verify(current,evidence)
        with CreatorLibrary(runner.database) as other:
            live=other.jobs.job(job["id"])
            other.jobs.request_cancel(job["id"],expected_revision=live["revision"])
        return proof
    runner.jobs.verifier=concurrent_cancel
    done=runner.run_next(timeout=10)
    assert done["state"]=="interrupted" and done["cancel_requested"]
    assert len(runner.jobs.resources())==2
    assert runner.jobs.settlement(job["id"]) is None
