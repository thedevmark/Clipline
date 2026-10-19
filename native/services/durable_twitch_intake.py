"""Durable own-clip acquisition. Native Moment preparation is a separate owner."""
from dataclasses import asdict, dataclass
from pathlib import Path
import json
import math
import os
import sqlite3
import threading
import time
import uuid

from native.core.durable_jobs import DurableJobs, StaleTransition, VerificationRequired
from native.core.twitch_inbox import TwitchClip, timestamp_identity
from native.services.caption_worker.evidence import canonical, digest, hash_file
from native.services.caption_worker.client import write_once
from native.services.native_export_client import ExportExecution, ExportCancelled
from native.services.durable_native_exports import ExportOwnershipError, identifier
from native.services.twitch_intake_client import TwitchIntakeClient, IntakeError, PROFILE
from native.services.twitch_publication import VerifiedTwitchPublication, PublicationGuardError


@dataclass(frozen=True)
class IntakeOutcome:
    job: dict | None
    asset: object | None
    binding: dict | None
    execution: ExportExecution | None
    reused: bool = False
    error_code: str | None = None


def identity(clip):
    return clip.id,clip.broadcaster_id,clip.creator_id,timestamp_identity(clip.created_at)


class DurableTwitchIntake:
    def __init__(self,library,directory,client,*,owner_id=None):
        if type(client) is not TwitchIntakeClient:
            raise IntakeError('QUALIFIED_CLIENT_REQUIRED')
        self.library=library; self.client=client; self.directory=Path(directory).resolve()
        self.directory.mkdir(parents=True,exist_ok=True)
        self.owner_id=identifier(owner_id or str(uuid.uuid4()))
        self._busy=threading.Lock(); self._execution=None; self._publication=None; self._attestation=None
        self.jobs=DurableJobs(library.connection,settlement_verifier=self._verify,completed_output_committer=self._commit)
        row=library.connection.execute('PRAGMA database_list').fetchone()
        if not row or not row[2]: raise IntakeError('PERSISTENT_LIBRARY_REQUIRED')
        self.database=Path(row[2]).resolve()

    def _background(self):
        if threading.current_thread() is threading.main_thread():
            raise IntakeError('BACKGROUND_OWNER_REQUIRED')

    def _write(self,kind,key,value):
        identifier(key)
        if len(canonical(value))>65536: raise IntakeError('METADATA_LIMIT')
        folder=self.directory/kind; folder.mkdir(exist_ok=True)
        if not folder.resolve().is_relative_to(self.directory): raise IntakeError('OWNED_PATH_ESCAPED')
        path=folder/(key+'.json'); reference={'id':key,'sha256':digest(value)}
        try: write_once(path,value)
        except FileExistsError:
            if self._read(kind,reference)!=value: raise IntakeError('ARTIFACT_COLLISION')
        return reference

    def _read(self,kind,reference):
        from native.core.durable_jobs import _reference
        reference=_reference(reference)
        path=self.directory/kind/(reference['id']+'.json')
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(self.directory):
            raise IntakeError('OWNED_PATH_ESCAPED')
        with path.open('rb') as stream: encoded=stream.read(65537)
        if len(encoded)>65536: raise IntakeError('METADATA_LIMIT')
        value=json.loads(encoded)
        if digest(value)!=reference['sha256']: raise IntakeError('ARTIFACT_CHANGED')
        return value

    def submit(self,clip_id,*,idempotency_key):
        self._background(); identifier(idempotency_key)
        selected=self.library.twitch_clip(clip_id)
        bound=self.library.twitch_source(clip_id)
        if bound is not None:
            asset=self.library.asset(bound['asset_id']); guard=ExportExecution({'reuse_clip':clip_id})
            try:
                guard.hold(asset.path,asset.sha256)
                if self.library.verify_asset(asset.id)!=asset: raise IntakeError('SOURCE_CHANGED')
            finally:
                guard.stop()
                if not guard.closed: raise ExportOwnershipError('INTAKE_GUARD_CLEANUP_UNKNOWN',[guard])
            return IntakeOutcome(None,asset,bound,None,True)
        if selected.duration_us>PROFILE['max_duration_us']: raise IntakeError('DURATION_LIMIT')
        envelope={'schema':1,'clip':asdict(selected),'profile':PROFILE,'runtime':self.client.identity}
        key=str(uuid.uuid5(uuid.UUID(idempotency_key),'twitch-intake-request'))
        reference=self._write('requests',key,envelope)
        job=self.jobs.submit('io',{'request_ref':reference,'profile_sha256':digest(PROFILE)},idempotency_key=idempotency_key)
        return IntakeOutcome(job,None,None,None)

    def request_cancel(self,jid,*,expected_revision):
        self._background()
        return self.jobs.request_cancel(jid,expected_revision=expected_revision)

    def _watch(self,job,cancel,done,faults):
        try:
            c=sqlite3.connect(self.database,isolation_level=None,timeout=2)
            try:
                c.execute('PRAGMA query_only=ON')
                while not done.is_set():
                    row=c.execute('SELECT state,cancel_requested,claim_token FROM durable_job_heads WHERE job_id=?',(job['id'],)).fetchone()
                    if not row or row[2]!=job['claim_token']: raise IntakeError('CLAIM_CHANGED')
                    if row[1] or row[0]=='interrupted': cancel.set()
                    done.wait(.025)
            finally: c.close()
        except BaseException:
            faults.append('CANCEL_WATCH_FAILED'); cancel.set()

    def _interrupt(self,job):
        current=self.jobs.job(job['id'])
        if current['state'] in ('running','cancel-requested'):
            current=self.jobs.mark_interrupted(job['id'],expected_revision=current['revision'],reason='cleanup_uncertain')
        return current

    def run_next(self,*,cancel=None,timeout=300):
        self._background()
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=300:
            raise IntakeError('FINITE_INTAKE_DEADLINE_REQUIRED')
        if not self._busy.acquire(False): raise IntakeError('INTAKE_BUSY')
        try:
            if self._execution is not None and not self._execution.closed:
                raise ExportOwnershipError('INTAKE_PREVIOUS_OWNER_UNKNOWN',[self._execution])
            if self._publication is not None and not self._publication.closed:
                raise PublicationGuardError(self._publication)
            return self._run(cancel or threading.Event(),time.monotonic()+timeout)
        except PublicationGuardError as error:
            self._publication=error.publication
            raise
        except ExportOwnershipError:
            raise
        except BaseException:
            if self._publication is not None and not self._publication.closed:
                raise PublicationGuardError(self._publication) from None
            if self._execution is not None and not self._execution.closed:
                raise ExportOwnershipError('INTAKE_BOOKKEEPING_UNKNOWN',[self._execution]) from None
            raise
        finally: self._busy.release()

    def _run(self,cancel,deadline):
        if cancel.is_set(): return None
        job=self.jobs.claim_next(owner_id=self.owner_id,kinds=['io'])
        if job is None: return None
        execution=ExportExecution({'database':str(self.database),'job_id':job['id'],'claim_token':job['claim_token'],
                                   'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id']})
        self._execution=execution
        root=self.directory/'executions'/job['output_set_id']
        staged=root/'download.part'; managed=None; owned_root=False; fault=None; unknown=False; done=threading.Event(); watcher=None; faults=[]
        self._cancel=cancel
        self._deadline=deadline
        try:
            envelope=self._read('requests',job['input']['request_ref'])
            if (not isinstance(envelope,dict) or set(envelope)!={'schema','clip','profile','runtime'}
                    or envelope['schema']!=1 or envelope['profile']!=PROFILE or envelope['runtime']!=self.client.identity
                    or job['input']['profile_sha256']!=digest(PROFILE) or job['input']['assets']):
                raise IntakeError('REQUEST_BINDING_CHANGED')
            selected=TwitchClip(**envelope['clip'])
            if identity(selected)!=identity(self.library.twitch_clip(selected.id)):
                raise IntakeError('SELECTED_IDENTITY_CHANGED')
            root.parent.mkdir(exist_ok=True)
            if not root.parent.resolve().is_relative_to(self.directory): raise IntakeError('OWNED_PATH_ESCAPED')
            root.mkdir(exist_ok=False)
            owned_root=True
            self.client.admit(execution)
            launch=self._write('launches',job['claim_token'],{'job_id':job['id'],'claim_token':job['claim_token'],
                'request_ref':job['input']['request_ref'],'output_set_id':job['output_set_id'],'runtime':self.client.identity})
            job=self.jobs.record_launch_intent(job['id'],expected_revision=job['revision'],claim_token=job['claim_token'],launch_ref=launch)
            watcher=threading.Thread(target=self._watch,args=(job,cancel,done,faults),daemon=True); watcher.start()
            self.client.transfer(envelope['clip'],staged,root/'transfer',execution,cancel,deadline)
            if cancel.is_set(): raise ExportCancelled('CANCELLED')
            sha256=hash_file(staged); execution.hold(staged,sha256)
            facts=self.client.probe(staged,root/'probe',execution,cancel,deadline)
            size=staged.stat().st_size
            if hash_file(staged)!=sha256: raise IntakeError('SOURCE_CHANGED')
            if cancel.is_set(): raise ExportCancelled('CANCELLED')
            if time.monotonic()>=deadline: raise IntakeError('DEADLINE')
        except ExportOwnershipError:
            unknown=True
            self._interrupt(job)
            raise
        except BaseException as error:
            safe={'AUTH_REQUIRED','SOURCE_UNAVAILABLE','INVALID_TRANSPORT','REDIRECT_LIMIT','INVALID_LIMIT',
                  'DEADLINE','HTTP_STATUS','INVALID_LENGTH','ENCODING','BYTE_LIMIT','INVALID_BODY','TRUNCATED',
                  'TRANSFER_FAILED','INVALID_REQUEST','DURATION_LIMIT','WORKER_FAILED','UNSUPPORTED_SOURCE',
                  'PROBE_FAILED','SOURCE_CHANGED','REQUEST_BINDING_CHANGED','SELECTED_IDENTITY_CHANGED'}
            fault=str(error) if isinstance(error,IntakeError) and str(error) in safe else 'CANCELLED' if isinstance(error,ExportCancelled) else 'INTAKE_FAILED'
        finally:
            done.set()
            if watcher:
                watcher.join(3)
                if watcher.is_alive(): faults.append('CANCEL_WATCH_UNKNOWN')
            if not unknown:
                cleanup=execution.stop()
                if not execution.closed or cleanup['cleanup_errors']:
                    self._interrupt(job)
                    raise ExportOwnershipError('INTAKE_STOP_UNKNOWN',[execution])
        if faults:
            return IntakeOutcome(self._interrupt(job),None,None,execution,error_code='CANCEL_WATCH_FAILED')
        current=self.jobs.job(job['id'])
        if current['state']=='interrupted': return IntakeOutcome(current,None,None,execution,error_code='INTAKE_INTERRUPTED')
        if cancel.is_set() and not current['cancel_requested']:
            current=self.jobs.request_cancel(current['id'],expected_revision=current['revision'])
        if fault is None and not current['cancel_requested']:
            try:
                # Per-output-set ownership avoids moving a shared hash-cache file
                # during failed settlement. Asset SHA dedup still remains global.
                folder=self.directory/'media'; folder.mkdir(exist_ok=True)
                if not folder.resolve().is_relative_to(self.directory): raise IntakeError('OWNED_PATH_ESCAPED')
                owned=folder/job['output_set_id']; owned.mkdir(exist_ok=False)
                target=owned/'source.mp4'
                os.link(staged,target); managed=target
                if time.monotonic()>=deadline: raise IntakeError('DEADLINE')
                result=self._write('results',job['output_set_id'],{'schema':1,'job_id':job['id'],
                    'claim_token':job['claim_token'],'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id'],
                    'selected':envelope['clip'],'profile_sha256':digest(PROFILE),'runtime':self.client.identity,
                    'media':{'path':str(target),'sha256':sha256,'bytes':size},'source_facts':facts,
                    'scope_proof':execution.proof,'candidate_verified':True})
                pub=VerifiedTwitchPublication(self.library,clip=selected,job=current,result_ref=result,
                    profile_sha256=digest(PROFILE),media_path=target,expected_sha256=sha256,expected_bytes=size)
                self._publication=pub
                try:
                    self._attestation=self._proof(current,result,'verified')
                    completed=self.jobs.finish(current['id'],expected_revision=current['revision'],claim_token=current['claim_token'],
                                               outcome='completed',evidence_ref=result)
                finally:
                    self._attestation=None
                    pub.close()
                bound=self.library.twitch_source(selected.id)
                return IntakeOutcome(completed,self.library.asset(bound['asset_id']),bound,execution)
            except PublicationGuardError:
                # SQL may already be completed. Never move that admitted media.
                raise
            except BaseException as error:
                try:
                    observed=self.jobs.job(job['id'])
                    admitted=self.library.connection.execute('SELECT 1 FROM assets WHERE path=?',(str(managed),)).fetchone() if managed else None
                except BaseException:
                    raise IntakeError('PUBLICATION_OUTCOME_UNKNOWN') from None
                if observed['state']=='completed' or admitted:
                    raise IntakeError('COMMITTED_RESULT_READ_FAILED') from None
                fault=str(error) if isinstance(error,IntakeError) and str(error) in ('DEADLINE','CANCELLED') else 'PUBLICATION_FAILED'
        # No process is live and this job's per-output-set files are unadmitted.
        quarantined=False
        try:
            for path in (managed,staged if owned_root else None):
                if path is not None and path.exists():
                    destination=path.with_name(path.name+'.quarantined')
                    if destination.exists(): raise IntakeError('QUARANTINE_COLLISION')
                    os.rename(path,destination); quarantined=True
            current=self.jobs.job(job['id'])
            if cancel.is_set() and current['state']=='running' and not current['cancel_requested']:
                current=self.jobs.request_cancel(current['id'],expected_revision=current['revision'])
            if current['state'] not in ('running','cancel-requested'):
                return IntakeOutcome(current,None,None,execution,error_code=fault or 'INTAKE_FAILED')
            outcome='cancelled' if current['cancel_requested'] else 'failed'
            if outcome=='cancelled': fault='CANCELLED'
            disposition='quarantined' if quarantined else 'absent'
            evidence=self._write('failures',str(uuid.uuid4()),{'job_id':job['id'],'claim_token':job['claim_token'],
                'error_code':fault or 'CANCELLED','process_proof':execution.proof,'outputs_disposition':disposition})
            self._attestation=self._proof(current,evidence,disposition)
            terminal=self.jobs.finish(current['id'],expected_revision=current['revision'],claim_token=current['claim_token'],
                                      outcome=outcome,evidence_ref=evidence)
            return IntakeOutcome(terminal,None,None,execution,error_code=fault or 'CANCELLED')
        except BaseException:
            self._interrupt(job)
            raise
        finally: self._attestation=None

    def _proof(self,job,reference,disposition):
        return {'job_id':job['id'],'claim_token':job['claim_token'],'intent_sha256':job['intent_sha256'],
                'output_set_id':job['output_set_id'],'process_disposition':'stopped' if self._execution.processes else 'never_started',
                'outputs_disposition':disposition,'result_ref':reference,'evidence_ref':reference}

    def _verify(self,job,reference):
        proof=self._attestation
        if (proof is None or self._execution is None or not self._execution.closed
                or proof['job_id']!=job['id'] or proof['claim_token']!=job['claim_token'] or proof['evidence_ref']!=reference):
            raise VerificationRequired('Only the live stopped intake execution may settle')
        artifact=self._read('results' if proof['outputs_disposition']=='verified' else 'failures',reference)
        if artifact['job_id']!=job['id'] or artifact['claim_token']!=job['claim_token']:
            raise VerificationRequired('Intake result artifact belongs to another claim')
        if proof['outputs_disposition']=='verified':
            scopes=self._execution.proof.get('scopes',[])
            if len(scopes)!=2 or any(s.get('disposition')!='stopped' or s.get('active_processes')!=0 or s.get('streams_closed') is not True for s in scopes):
                raise VerificationRequired('Transfer and probe cessation proof required')
            if (artifact['intent_sha256']!=job['intent_sha256'] or artifact['output_set_id']!=job['output_set_id']
                    or artifact['runtime']!=self.client.identity or artifact['scope_proof']!=self._execution.proof
                    or self._publication is None or artifact['media']!={key:self._publication.plan['media'][key] for key in ('path','sha256','bytes')}):
                raise VerificationRequired('Intake result differs from live guarded source proof')
        return json.loads(canonical(proof))

    def _commit(self,connection,job,attestation):
        if self._publication is None: raise VerificationRequired('Guarded publication required')
        if self._cancel.is_set(): raise IntakeError('CANCELLED')
        if time.monotonic()>=self._deadline: raise IntakeError('DEADLINE')
        self._publication.commit(connection,job,attestation)

    def cleanup_pending(self,job_id,*,execution):
        """Explicit live stop/requery only; never retries transfer or settles holds."""
        self._background()
        if not self._busy.acquire(False): raise IntakeError('INTAKE_BUSY')
        try:
            job=self.jobs.job(job_id)
            expected={'database':str(self.database),'job_id':job['id'],'claim_token':job['claim_token'],
                      'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id']}
            if (type(execution) is not ExportExecution or execution.binding!=expected
                    or (not execution.processes and not execution.guards) or job['state']!='interrupted'):
                raise IntakeError('ORIGINAL_INTAKE_OWNER_REQUIRED')
            retained=self._execution
            if (retained is not None and retained.binding==expected and execution is not retained):
                raise IntakeError('ORIGINAL_INTAKE_OWNER_REQUIRED')
            return execution.stop()
        finally: self._busy.release()
