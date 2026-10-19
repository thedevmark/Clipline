"""Run admitted caption requests on the caller's background thread.

Only the current live controller can attest its worker result. Restarted jobs
retain their holds for explicit reconciliation; stored receipts cannot settle
them automatically. This owner never accepts or projects machine words.
"""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import threading
import uuid

from native.core.durable_jobs import DurableJobs, JobError, StaleTransition, VerificationRequired
from native.core.creator_library import CreatorLibrary
from native.services.caption_audio import PreparedAudio
from native.services.caption_worker.backend import verify_source_audio
from native.services.caption_worker.client import CaptionClient, write_once, failure_message
from native.services.caption_worker.evidence import canonical, digest, hash_file, validate_request


class CaptionJobError(ValueError):
    pass


def _uuid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:
        raise CaptionJobError("A canonical UUID is required")
    return value


class DurableCaptionRunner:
    def __init__(self,library,directory,client,*,owner_id=None):
        self.library=library
        self.directory=Path(directory).resolve()
        self.directory.mkdir(parents=True,exist_ok=True)
        self.client=client
        self.owner_id=_uuid(owner_id or str(uuid.uuid4()))
        self.jobs=DurableJobs(library.connection,settlement_verifier=self._verify)
        self._attestations={}
        self._busy=threading.Lock()
        self._interrupted_job = None
        self._retained_client = None
        self._active_job = None
        self._active_client = None
        self._retained_execution = None
        self._retained_watcher = None
        row=library.connection.execute("PRAGMA database_list").fetchone()
        if not row or not row[2]:raise CaptionJobError("A persistent Creator Library is required")
        self.database=Path(row[2])

    def _read(self,kind,reference):
        if not isinstance(reference,dict) or set(reference)!={"id","sha256"}:
            raise CaptionJobError("An immutable artifact reference is required")
        identifier=_uuid(reference["id"])
        path=self.directory/kind/(identifier+".json")
        with path.open("rb") as stream:data=stream.read(65537)
        if len(data)>65536 or hashlib.sha256(data).hexdigest()!=reference["sha256"]:
            raise CaptionJobError("The immutable artifact changed or exceeds its bound")
        return json.loads(data)

    def _write(self,kind,identifier,value):
        identifier=_uuid(identifier)
        if len(canonical(value))>65536:raise CaptionJobError("Artifact exceeds 64 KiB")
        folder=self.directory/kind;folder.mkdir(exist_ok=True)
        path=folder/(identifier+".json")
        reference={"id":identifier,"sha256":digest(value)}
        try:write_once(path,value)
        except FileExistsError:
            if self._read(kind,reference)!=value:raise CaptionJobError("Immutable artifact identity collision")
        return reference

    def _bind(self,envelope,intent=None):
        if not isinstance(envelope,dict) or set(envelope)!={"schema","transcript_id","accepted_revision","request"} or envelope["schema"]!=1:
            raise CaptionJobError("Unsupported caption execution envelope")
        request=validate_request(envelope["request"])
        tid=_uuid(envelope["transcript_id"])
        if self.library.captions.identity(tid)!={k:request[k] for k in ("source","audio","range")}:
            raise CaptionJobError("Request differs from its transcript source/audio identity")
        asset=self.library.asset(request["source"]["asset_id"])
        if asset.sha256!=request["source"]["sha256"]:
            raise CaptionJobError("Request differs from its registered source identity")
        revision=envelope["accepted_revision"]
        pin=None
        if revision is not None:
            self.library.captions.revision(tid,revision)
            pin={"transcript_id":tid,"revision":revision}
        expected={"assets":[{"asset_id":asset.id,"sha256":asset.sha256}],
                  "profile_sha256":request["profile_sha256"],"range":request["range"],
                  "transcript":pin,"kit":request.get("kit_revision")}
        if intent is not None:
            if any(intent.get(k)!=v for k,v in expected.items()) or any(intent.get(k) is not None for k in ("snapshot","recipe_sha256")):
                raise CaptionJobError("Execution artifact and normalized retention pins differ")
        return request,expected

    def submit(self,prepared,*,transcript_id,idempotency_key,accepted_revision=None):
        """Persist the complete prepared request; pin existing accepted history explicitly."""
        if type(prepared) is not PreparedAudio or prepared.state!="complete":
            raise CaptionJobError("Successful prepared audio is required")
        key=_uuid(idempotency_key)
        request=prepared.load_request()
        envelope={"schema":1,"transcript_id":transcript_id,"accepted_revision":accepted_revision,"request":request}
        request,expected=self._bind(envelope)
        reference=self._write("requests",str(uuid.uuid5(uuid.UUID(key),"caption-request")),envelope)
        return self.jobs.submit("caption",{**expected,"request_ref":reference},idempotency_key=key)

    def _watch(self,job,cancel,done,faults):
        try:
            with closing(sqlite3.connect(str(self.database),isolation_level=None,timeout=2)) as connection:
                connection.execute("PRAGMA query_only=ON")
                while not done.is_set():
                    row=connection.execute("SELECT state,cancel_requested,claim_token FROM durable_job_heads WHERE job_id=?",(job["id"],)).fetchone()
                    if row is None or row[2]!=job["claim_token"]:
                        raise CaptionJobError("The live claim disappeared or changed")
                    if row[1] or row[0]=="interrupted":cancel.set()
                    if done.wait(.05):break
        except Exception as exc:
            faults.append(type(exc).__name__);cancel.set()

    @property
    def has_unsettled(self):
        client = self._retained_client or self._active_client or self.client
        return bool(getattr(client, 'has_unsettled', False)
                    or self._retained_watcher is not None and self._retained_watcher.is_alive())

    @property
    def retained_owner(self):
        return self._retained_client

    @property
    def interrupted_job(self):
        """Last retained claim, not an inferred current database head."""
        return json.loads(canonical(self._interrupted_job)) if self._interrupted_job else None

    def observe_execution(self, *, wait_seconds=0):
        return self._observe_owner(wait_seconds, cleanup=False)

    def cleanup_execution(self, *, wait_seconds=5):
        # Process cleanup is not durable settlement or output promotion.
        return self._observe_owner(wait_seconds, cleanup=True)

    def _observe_owner(self, wait_seconds, *, cleanup):
        client = self._retained_client or self._active_client or self.client
        if not self._busy.acquire(False):
            error = CaptionJobError('The caption controller operation is still running')
            error.owner = client
            raise error
        try:
            action = client.cleanup_execution if cleanup else client.observe_execution
            proof = action(wait_seconds=wait_seconds, expected_execution=self._retained_execution)
            watcher_stopped = self._retained_watcher is None or not self._retained_watcher.is_alive()
            return {**proof, 'controller_watchers_stopped': watcher_stopped,
                    'owner_unsettled': bool(client.has_unsettled or not watcher_stopped)}
        except BaseException as exc:
            if not hasattr(exc, 'owner'):exc.owner = client
            raise
        finally:
            self._busy.release()

    def _retain(self, job, watcher=None):
        self._interrupted_job = json.loads(canonical(job))
        self._retained_client = self._active_client or self.client
        self._retained_execution = getattr(self._retained_client, 'current_execution', None)
        if watcher is not None:self._retained_watcher = watcher

    def reconcile_execution(self, job_id, *, expected_revision):
        """Explicitly fail/cancel this live owner's interrupted work; never complete it."""
        if not self._busy.acquire(False):
            error = CaptionJobError('This controller already runs an operation')
            error.owner = self._retained_client or self._active_client or self.client
            raise error
        try:
            _uuid(job_id)
            if type(expected_revision) is not int or expected_revision < 0:
                raise CaptionJobError('An explicit nonnegative DB revision is required')
            retained, client, execution = self._interrupted_job, self._retained_client, self._retained_execution
            if (retained is None or retained['id'] != job_id or client is None
                    or type(client) is not CaptionClient or client is not self.client or execution is None):
                raise VerificationRequired('Only the retained original live execution can reconcile')
            if self._retained_watcher is not None and self._retained_watcher.is_alive():
                raise VerificationRequired('The original cancellation watcher has not stopped')
            # The initial generation's SQLite connection may already be closed
            # or belong to another background thread. Never reassign self.library.
            with CreatorLibrary(self.database) as library:
                jobs = DurableJobs(library.connection, settlement_verifier=self._verify)
                current = jobs.job(job_id)
                if current['revision'] != expected_revision:
                    raise StaleTransition('The retained claim DB revision changed')
                pins = ('id', 'claim_token', 'owner_id', 'intent_sha256', 'output_set_id', 'input', 'launch_ref')
                if any(current[key] != retained[key] for key in pins):
                    raise VerificationRequired('The current claim differs from the original execution')
                if current['state'] not in ('running', 'cancel-requested', 'interrupted'):
                    raise StaleTransition('The retained execution is no longer unsettled')
                envelope = self._read('requests', current['input']['request_ref'])
                request = validate_request(envelope['request'])
                directory = self.directory / 'executions' / current['output_set_id'] / request['run_id']
                launch = self._read('launches', current['launch_ref'])
                if (execution is not client.current_execution or execution.run_id != request['run_id']
                        or execution.request_sha256 != digest(request) or execution.directory != directory.resolve()
                        or execution.command_sha256 != launch['worker_command_sha256']
                        or any(launch[key] != current[key] for key in
                               ('claim_token', 'intent_sha256', 'output_set_id'))
                        or launch['job_id'] != job_id or launch['request_ref'] != current['input']['request_ref']):
                    raise VerificationRequired('The live execution/request/launch identity changed')
                # This holds the original client's existing operation lock, so
                # no unrelated direct run can replace its identity before CAS.
                with client.settlement_observation(execution) as proof:
                    if current['state'] in ('running', 'cancel-requested'):
                        current = jobs.mark_interrupted(job_id, expected_revision=current['revision'],
                                                        reason='cleanup_uncertain')
                        self._interrupted_job = json.loads(canonical(current))
                    outcome = 'cancelled' if current['cancel_requested'] else 'failed'
                    outputs = 'quarantined' if directory.exists() else 'absent'
                    receipt_path = directory / 'receipt.json'
                    receipt_hash = None
                    if receipt_path.is_file():
                        with receipt_path.open('rb') as stream:
                            receipt_bytes = stream.read(65537)
                        if len(receipt_bytes) > 65536:
                            raise CaptionJobError('The parent receipt exceeds its 64 KiB bound')
                        receipt_hash = hashlib.sha256(receipt_bytes).hexdigest()
                    summary = {'schema': 1, 'job_id': job_id, 'claim_token': current['claim_token'],
                        'intent_sha256': current['intent_sha256'], 'output_set_id': current['output_set_id'],
                        'request_ref': current['input']['request_ref'], 'outcome': outcome,
                        'outputs_disposition': outputs, 'process_disposition': proof['scope']['disposition'],
                        'fault': {'type': 'ExplicitLiveReconciliation', 'message': 'Outputs were not promoted'},
                        'run_id': request['run_id'], 'receipt_sha256': receipt_hash,
                        'worker_state': None, 'worker_error': None,
                        'live_execution_nonce': execution.nonce, 'process_observation': proof}
                    reference = self._write('results', str(uuid.uuid4()), summary)
                    attestation = {'job_id': job_id, 'claim_token': current['claim_token'],
                        'intent_sha256': current['intent_sha256'], 'output_set_id': current['output_set_id'],
                        'process_disposition': proof['scope']['disposition'], 'outputs_disposition': outputs,
                        'result_ref': reference, 'evidence_ref': reference}
                    self._attestations[current['claim_token']] = attestation
                    try:
                        settled = jobs.reconcile(job_id, expected_revision=current['revision'],
                            claim_token=current['claim_token'], outcome=outcome, evidence_ref=reference)
                    finally:
                        self._attestations.pop(current['claim_token'], None)
            self._interrupted_job = self._retained_client = self._retained_execution = None
            self._retained_watcher = None
            return json.loads(canonical(settled))
        except BaseException as exc:
            if not hasattr(exc, 'owner'):exc.owner = self._retained_client or self._active_client or self.client
            raise
        finally:
            self._busy.release()

    def run_next(self,*,cancel=None,timeout=180,job_id=None):
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=900:
            raise CaptionJobError("A finite worker deadline in (0,900] is required")
        if not self._busy.acquire(False):
            error = CaptionJobError("This controller already runs a job")
            error.owner = self._retained_client or self.client
            raise error
        try:
            if job_id is not None:_uuid(job_id)
            if self.has_unsettled or self._interrupted_job is not None:
                error = CaptionJobError('The original caption execution or interrupted claim needs settlement')
                error.owner = self._retained_client or self.client
                raise error
            result = self._run_next(cancel or threading.Event(),timeout,job_id)
            return json.loads(canonical(result)) if result is not None else None
        except BaseException as exc:
            if self._active_job is not None or self.has_unsettled or self._interrupted_job is not None:
                if self._interrupted_job is None and self._active_job is not None:self._retain(self._active_job)
                if self._retained_client is None:self._retained_client = self._active_client or self.client
                if not hasattr(exc, 'owner'):exc.owner = self._retained_client
            raise
        finally:
            self._active_job = None
            self._active_client = None
            self._busy.release()

    def _run_next(self,cancel,timeout,job_id=None):
        if cancel.is_set():return None
        job=self.jobs.claim_next(owner_id=self.owner_id,kinds=["caption"],job_id=job_id)
        if job is None:return None
        self._active_job = job
        self._active_client = self.client
        launched=False;result=None;fault=None;process_disposition="never_started"
        done=threading.Event();faults=[];watcher=None
        output=self.directory/"executions"/job["output_set_id"]
        try:
            envelope=self._read("requests",job["input"]["request_ref"])
            request,_=self._bind(envelope,job["input"])
            asset=self.library.asset(request["source"]["asset_id"])
            if Path(asset.path).resolve()!=Path(request["source"]["path"]).resolve():
                raise CaptionJobError("The source moved; prepare a new explicit request")
            verify_source_audio(request)
            if hash_file(request["profile_path"])!=request["profile_sha256"]:
                raise CaptionJobError("The cached profile changed")
            launch=self._write("launches",job["claim_token"],{
                "job_id":job["id"],"claim_token":job["claim_token"],"intent_sha256":job["intent_sha256"],
                "output_set_id":job["output_set_id"],"request_ref":job["input"]["request_ref"],
                "worker_command_sha256":digest(self.client.command)})
            job=self.jobs.record_launch_intent(job["id"],expected_revision=job["revision"],claim_token=job["claim_token"],launch_ref=launch)
            self._active_job = job
            watcher=threading.Thread(target=self._watch,args=(job,cancel,done,faults),daemon=True)
            self._retained_watcher = watcher
            watcher.start()
            launched=True
            result=self.client.run(request,output,timeout=timeout,cancel=cancel)
            if result.directory.resolve()!=output/request["run_id"]:
                raise CaptionJobError("Worker output escaped its owned namespace")
            receipt_path=result.directory/"receipt.json"
            with receipt_path.open("rb") as stream:receipt_bytes=stream.read(65537)
            if len(receipt_bytes)>65536:raise CaptionJobError("Parent receipt exceeds its bound")
            receipt=json.loads(receipt_bytes)
            if (receipt["run_id"]!=request["run_id"] or receipt["hashes"].get("request")!=digest(request)
                    or receipt["state"]!=result.state or receipt["error_code"]!=result.error_code):
                raise CaptionJobError("Parent receipt belongs to another request")
            process_disposition=receipt["process_scope"]["disposition"]
            if not receipt["streams_closed"] or process_disposition not in ("stopped","never_started"):
                raise CaptionJobError("The current worker scope or streams remain uncertain")
            artifacts={kind:result.load(kind) for kind in ("hypothesis","alignment")}
            if result.state=="complete" and (receipt["worker_exit_code"]!=0 or any(value is None for value in artifacts.values())):
                raise CaptionJobError("Complete output lacks verified machine evidence")
        except Exception as exc:fault={"type":type(exc).__name__,"message":failure_message(exc)[:500]}
        finally:
            done.set()
            if watcher:watcher.join(3)
        current=self.jobs.job(job["id"])
        if faults or watcher and watcher.is_alive() or launched and (fault is not None or process_disposition=="unknown" or self.has_unsettled):
            self._retain(current, watcher)
            if current["state"] in ("running","cancel-requested"):
                current=self.jobs.mark_interrupted(current["id"],expected_revision=current["revision"],reason="cleanup_uncertain" if not faults else "owner_lost")
            self._retain(current, watcher)
            self._write("uncertain",job["claim_token"],{"job_id":job["id"],"fault":fault,"controller_faults":faults,"resource_holds_retained":True})
            return current
        if cancel.is_set() and current["state"]=="running":
            current=self.jobs.request_cancel(current["id"],expected_revision=current["revision"])
        if current["state"]=="interrupted":
            self._retain(current, watcher)
            return current
        outcome="cancelled" if current["cancel_requested"] else "completed" if result is not None and result.state=="complete" and fault is None else "failed"
        disposition="verified" if outcome=="completed" else "quarantined" if output.exists() else "absent"
        summary={"schema":1,"job_id":job["id"],"claim_token":job["claim_token"],
                 "intent_sha256":job["intent_sha256"],"output_set_id":job["output_set_id"],
                 "request_ref":job["input"]["request_ref"],"outcome":outcome,"outputs_disposition":disposition,
                 "process_disposition":process_disposition,"fault":fault,
                 "run_id":request["run_id"] if result is not None else None,
                 "receipt_sha256":hash_file(result.directory/"receipt.json") if result is not None else None,
                 "worker_state":result.state if result is not None else None,
                 "worker_error":result.error_code if result is not None else None}
        result_ref=self._write("results",job["output_set_id"],summary)
        attested={"job_id":job["id"],"claim_token":job["claim_token"],"intent_sha256":job["intent_sha256"],
                  "output_set_id":job["output_set_id"],"process_disposition":process_disposition,
                  "outputs_disposition":disposition,"result_ref":result_ref,"evidence_ref":result_ref}
        self._attestations[job["claim_token"]]=attested
        try:
            return self.jobs.finish(job["id"],expected_revision=current["revision"],claim_token=job["claim_token"],outcome=outcome,evidence_ref=result_ref)
        except StaleTransition:
            current=self.jobs.job(job["id"])
            if current["claim_token"]==job["claim_token"] and current["state"] in ("running","cancel-requested"):
                current = self.jobs.mark_interrupted(current["id"],expected_revision=current["revision"],reason="owner_lost")
            if current['state'] == 'interrupted':
                self._retain(current, watcher)
            return current
        finally:self._attestations.pop(job["claim_token"],None)

    def _verify(self,job,evidence_ref):
        attested=self._attestations.get(job["claim_token"])
        if attested is None or attested["evidence_ref"]!=evidence_ref:
            raise VerificationRequired("Only this live controller can settle its completed worker")
        summary=self._read("results",evidence_ref)
        if any(summary[k]!=job[k] for k in ("intent_sha256","output_set_id")) or summary["job_id"]!=job["id"] or summary["claim_token"]!=job["claim_token"]:
            raise VerificationRequired("Result belongs to another claim")
        if summary["request_ref"]!=job["input"]["request_ref"]:
            raise VerificationRequired("Execution request changed")
        if summary["outcome"]=="completed":
            envelope=self._read("requests",job["input"]["request_ref"])
            self._bind(envelope,job["input"])
        if summary["run_id"] is not None and summary["receipt_sha256"] is not None:
            path=self.directory/"executions"/job["output_set_id"]/summary["run_id"]/"receipt.json"
            if hash_file(path)!=summary["receipt_sha256"]:
                raise VerificationRequired("The parent output receipt changed")
        return dict(attested)

    def import_completed(self,job_id):
        """Retain machine proposals idempotently; accepted corrections never change."""
        job=self.jobs.job(job_id)
        settlement=self.jobs.settlement(job_id)
        if job["state"]!="completed" or settlement is None:
            raise CaptionJobError("Only verified completed output can be imported")
        summary=self._read("results",settlement["result_ref"])
        envelope=self._read("requests",job["input"]["request_ref"])
        request,_=self._bind(envelope,job["input"])
        directory=self.directory/"executions"/job["output_set_id"]/request["run_id"]
        if hash_file(directory/"receipt.json")!=summary["receipt_sha256"]:
            raise CaptionJobError("The completed parent receipt changed")
        receipt=json.loads((directory/"receipt.json").read_text(encoding="utf8"))
        artifacts={}
        for kind in ("hypothesis","alignment"):
            with (directory/(kind+".json")).open("rb") as stream:data=stream.read(1048577)
            if len(data)>1048576:raise CaptionJobError("Machine evidence exceeds its bound")
            value=json.loads(data)
            if digest(value)!=receipt["hashes"][kind]:raise CaptionJobError("Completed machine evidence changed")
            artifacts[kind]=value
        tid=envelope["transcript_id"]
        hypothesis=self.library.captions.propose_hypothesis(tid,artifacts["hypothesis"])
        alignment=self.library.captions.propose_alignment(tid,hypothesis,artifacts["alignment"])
        return {"transcript_id":tid,"hypothesis_id":hypothesis,"alignment_id":alignment}
