"""Private native render owner over the existing CreatorLibrary/DurableJobs.

Only the live qualified client can create a completed receipt. An interrupted
execution keeps resource holds; no timeout lease, retry or persisted-receipt
replay can settle it. Invoke on the existing background owner, not Qt's thread.
"""
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import json,math,sqlite3,threading,uuid
from native.core.project_store import NativeSnapshot
from native.core.durable_jobs import DurableJobs,StaleTransition,VerificationRequired
from native.services.caption_worker.evidence import canonical,digest,hash_file
from native.services.caption_worker.client import write_once
from native.services.native_export_client import NativeExportClient,ExportExecution,ExportOutcome,verify_probe

RECIPE={'id':'clipline.h264.software.v1','format':'h264','range':'entire','gpuRendering':'off','hardwareEncoding':'off','burnCaptions':True,'audio':{'codec':'aac','sample_rate':48000,'channels':2},'scale':1}
class ExportJobError(ValueError):pass

class ExportOwnershipError(ExportJobError):
    """A caller must retain these actual owners when bookkeeping cannot return."""
    def __init__(self,message,executions):
        self.executions=tuple(executions)
        super().__init__(message)

@dataclass(frozen=True)
class ExportJobOutcome:
    """Keep this outcome/live execution until cleanup confirms it is closed."""
    job:dict
    execution:ExportExecution|None

def identifier(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ExportJobError('Canonical UUID required')
    return value
def sha(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):raise ExportJobError('Canonical SHA256 required')
    return value
def integer(value,minimum=0):
    if type(value) is not int or not minimum<=value<=2**63-1:raise ExportJobError('Bounded integer required')
    return value
def selected_compositions(value):
    if not isinstance(value,list) or not 1<=len(value)<=2 or any(type(x) is not str or x not in ('landscape','portrait') for x in value) or len(set(value))!=len(value):
        raise ExportJobError('Select distinct native landscape/portrait compositions')
    return value
def owner_inventory(value):
    if not isinstance(value,list) or len(value)!=1:raise ExportJobError('One explicit linked owner inventory required')
    for pin in value:
        if not isinstance(pin,dict) or set(pin)!={'owner','revision'}:raise ExportJobError('Exact owner/revision pin required')
        integer(pin['owner'],1);integer(pin['revision'])
    return value

class DurableNativeExports:
    def __init__(self,library,directory,client,*,owner_id=None):
        if type(client) is not NativeExportClient:raise ExportJobError('The qualified native export client is required')
        self.library=library;self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True)
        self.client=client;self.owner_id=identifier(owner_id or str(uuid.uuid4()));self._attestations={};self._busy=threading.Lock();self._executions={}
        self.jobs=DurableJobs(library.connection,settlement_verifier=self._verify)
        row=library.connection.execute('PRAGMA database_list').fetchone()
        if not row or not row[2]:raise ExportJobError('Persistent CreatorLibrary required')
        self.database=Path(row[2])
    def _write(self,kind,uid,value):
        identifier(uid)
        if len(canonical(value))>65536:raise ExportJobError('Durable metadata exceeds 64 KiB')
        folder=self.directory/kind;folder.mkdir(exist_ok=True);path=folder/(uid+'.json');reference={'id':uid,'sha256':digest(value)}
        try:write_once(path,value)
        except FileExistsError:
            if self._read(kind,reference)!=value:raise ExportJobError('Immutable execution metadata collision')
        return reference
    def _read(self,kind,reference):
        if not isinstance(reference,dict) or set(reference)!={'id','sha256'}:raise ExportJobError('Stable artifact reference required')
        identifier(reference['id']);sha(reference['sha256']);path=self.directory/kind/(reference['id']+'.json')
        with path.open('rb') as stream:encoded=stream.read(65537)
        if len(encoded)>65536:raise ExportJobError('Durable artifact exceeds bound')
        value=json.loads(encoded)
        if digest(value)!=reference['sha256']:raise ExportJobError('Durable artifact changed')
        return value
    def submit(self,snapshot,*,project_id,kit_pin,accepted_pin,owners,compositions,idempotency_key):
        if type(snapshot) is not NativeSnapshot or snapshot.engine!='filmcraft' or snapshot.path.suffix!='.fcproj':raise ExportJobError('A real immutable Filmcraft ProjectStore snapshot is required')
        identifier(project_id);identifier(idempotency_key);identifier(snapshot.generation)
        if snapshot.path.stem!=snapshot.generation or hash_file(snapshot.path)!=snapshot.sha256:raise ExportJobError('Snapshot file identity changed')
        selected_compositions(compositions);owner_inventory(owners)
        if not isinstance(kit_pin,dict) or set(kit_pin)!={'kit_id','version'}:raise ExportJobError('Select an explicit immutable Kit pin')
        moment=self.library.moment(project_id);record=self.library.kit_record(kit_pin['kit_id'],kit_pin['version']);descriptor=json.loads(record.descriptor_json)
        pins={asset:self.library.asset(asset) for asset in {moment.source_asset_id,*descriptor['asset_refs'].values()}}
        if accepted_pin is not None:
            if not isinstance(accepted_pin,dict) or set(accepted_pin)!={'transcript_id','revision','payload_sha256'}:raise ExportJobError('Complete accepted caption pin required')
            identifier(accepted_pin['transcript_id']);sha(accepted_pin['payload_sha256']);self.library.captions.projection_input(accepted_pin['transcript_id'],revision=accepted_pin['revision'])
        envelope={'schema':1,'project_id':project_id,'snapshot':{'path':str(snapshot.path),'project_id':project_id,'generation':snapshot.generation,'revision':snapshot.revision,'sha256':snapshot.sha256,'engine':snapshot.engine,'engine_version':snapshot.engine_version},
            'kit':{**kit_pin,'descriptor_sha256':record.descriptor_sha256},'accepted_pin':accepted_pin,
            'source_asset_id':moment.source_asset_id,'assets':[{'asset_id':a.id,'sha256':a.sha256,'path':a.path} for a in sorted(pins.values(),key=lambda a:a.id)],
            'owners':owners,'compositions':compositions,'recipe_sha256':digest(RECIPE),
            'engine':{'helper_sha256':self.client.helper.sha256,'source_pin_sha256':self.client.source_pin_sha256,'version':self.client.engine_version}}
        envelope=json.loads(canonical(envelope));intent=self._bind(envelope)
        reference=self._write('requests',str(uuid.uuid5(uuid.UUID(idempotency_key),'native-export-request')),envelope)
        return self.jobs.submit('render',{**intent,'request_ref':reference},idempotency_key=idempotency_key)
    def _bind(self,envelope,intent=None,*,live_paths=True):
        if not isinstance(envelope,dict) or set(envelope)!={'schema','project_id','snapshot','kit','accepted_pin','source_asset_id','assets','owners','compositions','recipe_sha256','engine'}:
            raise ExportJobError('Exact native execution envelope required')
        if type(envelope['schema']) is not int or envelope['schema']!=1 or envelope['recipe_sha256']!=digest(RECIPE):raise ExportJobError('Unsupported pinned export recipe')
        selected_compositions(envelope['compositions']);owner_inventory(envelope['owners'])
        identifier(envelope['project_id']);snapshot=envelope['snapshot'];identifier(snapshot['generation']);sha(snapshot['sha256'])
        if set(snapshot)!={'path','project_id','generation','revision','sha256','engine','engine_version'}:raise ExportJobError('Exact immutable snapshot binding required')
        integer(snapshot['revision'])
        if not isinstance(snapshot['path'],str) or len(snapshot['path'])>32768 or not Path(snapshot['path']).is_absolute() or Path(snapshot['path']).stem!=snapshot['generation'] or Path(snapshot['path']).suffix!='.fcproj':raise ExportJobError('Canonical immutable snapshot path required')
        if snapshot['project_id']!=envelope['project_id'] or snapshot['engine']!='filmcraft':raise ExportJobError('Snapshot project/engine identity differs')
        expected_engine={'helper_sha256':self.client.helper.sha256,'source_pin_sha256':self.client.source_pin_sha256,'version':self.client.engine_version}
        if envelope['engine']!=expected_engine:raise ExportJobError('Queued qualified render engine changed')
        if not isinstance(envelope['kit'],dict) or set(envelope['kit'])!={'kit_id','version','descriptor_sha256'}:raise ExportJobError('Exact Kit provenance required')
        identifier(envelope['kit']['kit_id']);integer(envelope['kit']['version'],1);sha(envelope['kit']['descriptor_sha256'])
        record=self.library.kit_record(envelope['kit']['kit_id'],envelope['kit']['version'])
        if record.descriptor_sha256!=envelope['kit']['descriptor_sha256']:raise ExportJobError('Pinned Kit descriptor changed')
        moment=self.library.moment(envelope['project_id']);identifier(envelope['source_asset_id'])
        expected_assets={moment.source_asset_id,*json.loads(record.descriptor_json)['asset_refs'].values()}
        if envelope['source_asset_id']!=moment.source_asset_id:raise ExportJobError('Queued moment source changed')
        if not isinstance(envelope['assets'],list) or not 1<=len(envelope['assets'])<=64:raise ExportJobError('Bounded source/Kit asset closure required')
        seen=set()
        assets=[]
        for pin in envelope['assets']:
            if not isinstance(pin,dict) or set(pin)!={'asset_id','sha256','path'}:raise ExportJobError('Exact registered asset binding required')
            identifier(pin['asset_id']);sha(pin['sha256'])
            if pin['asset_id'] in seen:raise ExportJobError('Duplicate asset pin')
            seen.add(pin['asset_id'])
            asset=self.library.asset(pin['asset_id'])
            if asset.sha256!=pin['sha256'] or live_paths and Path(asset.path).resolve()!=Path(pin['path']).resolve():raise ExportJobError('Pinned asset changed or moved; create a new explicit render request')
            assets.append({'asset_id':asset.id,'sha256':asset.sha256})
        if seen!=expected_assets:raise ExportJobError('Queued source/Kit closure differs from its immutable descriptor')
        if envelope['accepted_pin'] is not None:
            pin=envelope['accepted_pin']
            if not isinstance(pin,dict) or set(pin)!={'transcript_id','revision','payload_sha256'}:raise ExportJobError('Exact accepted caption pin required')
            identifier(pin['transcript_id']);integer(pin['revision'],1);sha(pin['payload_sha256'])
            self.library.captions.projection_input(pin['transcript_id'],revision=pin['revision'])
        expected={'assets':assets,'snapshot':{k:v for k,v in snapshot.items() if k!='path'},'kit':{k:envelope['kit'][k] for k in ('kit_id','version')},
            'transcript':{k:envelope['accepted_pin'][k] for k in ('transcript_id','revision')} if envelope['accepted_pin'] else None,'recipe_sha256':envelope['recipe_sha256']}
        if intent is not None and (any(intent[k]!=v for k,v in expected.items()) or intent['profile_sha256'] is not None or intent['range'] is not None):raise ExportJobError('Durable intent differs from full native execution request')
        return expected
    def _native(self,envelope,observation):
        if observation['recipe']!=RECIPE or observation['recipeSHA256']!=envelope['recipe_sha256'] or observation['validation']['valid'] is not True:raise ExportJobError('Native profile/document verification differs')
        owners=observation['validation']['owners']
        if len(owners)!=1:raise ExportJobError('One linked moment is required')
        owner=str(owners[0]['owner']);metadata=observation['ownerMetadata'][owner]
        kit=json.loads(metadata['clipline.creatorKit.v1'])
        if kit['schema']!=1 or kit['momentId']!=envelope['project_id'] or kit['kit']!={'id':envelope['kit']['kit_id'],'version':envelope['kit']['version'],'descriptorSha256':envelope['kit']['descriptor_sha256']}:raise ExportJobError('Native Kit/moment identity differs from queued provenance')
        expected_assets={x['asset_id']:x for x in envelope['assets']};source=expected_assets[envelope['source_asset_id']]
        if kit['sourceAsset']['id']!=source['asset_id'] or kit['sourceAsset']['sha256']!=source['sha256']:raise ExportJobError('Native source pin differs from registered source')
        units=owners[0]['units']
        if not units:raise ExportJobError('A nonempty linked moment source inventory is required')
        for unit in units:
            for role in unit['roles']:
                if (role['role'] not in ('gameplay','facecam') or role['resolvedMediaPath'] is None
                    or Path(role['resolvedMediaPath']).resolve(strict=True)!=Path(source['path']).resolve(strict=True)):
                    raise ExportJobError('Native linked source role differs from the queued canonical source')
        descriptor=self.library.kit(envelope['kit']['kit_id'],envelope['kit']['version'])
        if kit['assetRefs']!=descriptor['asset_refs']:raise ExportJobError('Native Kit dependency bindings differ')
        template_id=descriptor['asset_refs'].get('native_template')
        if template_id is None:raise ExportJobError('This native Kit profile requires its immutable template asset')
        template=expected_assets[template_id];native_template=kit['templateAsset']
        if native_template['id']!=template_id or native_template['sha256']!=template['sha256'] or Path(native_template['path']).resolve(strict=True)!=Path(template['path']).resolve(strict=True):raise ExportJobError('Native template provenance differs from its pinned Kit asset')
        caption=metadata.get('clipline.acceptedCaptions.v1')
        if envelope['accepted_pin'] is None:
            if caption is not None:raise ExportJobError('Snapshot has an unpinned accepted transcript')
        else:
            body=json.loads(caption)
            if body['pin']!=envelope['accepted_pin'] or body['input']!=self.library.captions.projection_input(envelope['accepted_pin']['transcript_id'],revision=envelope['accepted_pin']['revision']):raise ExportJobError('Native accepted revision differs from queued canonical words')
        by_path={Path(x['path']).resolve(strict=True):x for x in envelope['assets']}
        dependencies=observation['mediaDependencies']
        if not isinstance(dependencies,list) or len(dependencies)>64:raise ExportJobError('Native media dependency closure exceeds profile')
        for dependency in dependencies:
            if dependency['offline'] is not False or Path(dependency['path']).resolve(strict=True) not in by_path:raise ExportJobError('Native media requires an explicit registered source/Kit dependency')
        for pin in envelope['assets']:
            asset=self.library.verify_asset(pin['asset_id'])
            if asset.sha256!=pin['sha256']:raise ExportJobError('Source/Kit asset bytes changed')
    def _watch(self,job,cancel,done,faults):
        try:
            with closing(sqlite3.connect(str(self.database),isolation_level=None,timeout=2)) as c:
                c.execute('PRAGMA query_only=ON')
                while not done.is_set():
                    row=c.execute('SELECT state,cancel_requested,claim_token FROM durable_job_heads WHERE job_id=?',(job['id'],)).fetchone()
                    if not row or row[2]!=job['claim_token']:raise ExportJobError('Live render claim changed')
                    if row[1] or row[0]=='interrupted':cancel.set()
                    done.wait(.05)
        except Exception as exc:faults.append(str(exc)[:500]);cancel.set()
    def run_next(self,*,cancel=None,timeout=900):
        if threading.current_thread() is threading.main_thread():raise ExportJobError('Run native export persistence/hash work on the background owner')
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=3600:raise ExportJobError('Finite export deadline within one hour required before admission')
        if not self._busy.acquire(False):raise ExportJobError('This render owner is busy')
        try:return self._run(cancel or threading.Event(),timeout)
        except Exception as exc:
            live=tuple(owner for owner in self._executions.values() if not owner.closed)
            if live:raise ExportOwnershipError('Export bookkeeping failed while live ownership remains: '+str(exc)[:1000],live) from exc
            raise
        finally:self._busy.release()
    def _run(self,cancel,timeout):
        if cancel.is_set():return None
        job=self.jobs.claim_next(owner_id=self.owner_id,kinds=['render'])
        if job is None:return None
        runtime=None;fault=None;launched=False;done=threading.Event();faults=[];watcher=None;execution=self.directory/'executions'/job['output_set_id'];scope_owner=None;cleanup=None
        try:
            envelope=self._read('requests',job['input']['request_ref']);self._bind(envelope,job['input'])
            if hash_file(envelope['snapshot']['path'])!=envelope['snapshot']['sha256']:raise ExportJobError('Queued immutable snapshot changed')
            scope_owner=ExportExecution({'database':str(self.database.resolve()),'job_id':job['id'],'claim_token':job['claim_token'],'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id']})
            self._executions[job['claim_token']]=scope_owner
            scope_owner.hold(envelope['snapshot']['path'],envelope['snapshot']['sha256'])
            for pin in envelope['assets']:
                scope_owner.hold(pin['path'],pin['sha256'])
                if self.library.verify_asset(pin['asset_id']).sha256!=pin['sha256']:raise ExportJobError('Pinned source/Kit content changed')
            self.client.admit(scope_owner)
            launch=self._write('launches',job['claim_token'],{'job_id':job['id'],'claim_token':job['claim_token'],'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id'],'request_ref':job['input']['request_ref'],'engine':envelope['engine']})
            job=self.jobs.record_launch_intent(job['id'],expected_revision=job['revision'],claim_token=job['claim_token'],launch_ref=launch)
            watcher=threading.Thread(target=self._watch,args=(job,cancel,done,faults),daemon=True);watcher.start();launched=True
            outcome=self.client.run(envelope,execution,cancel=cancel,timeout=timeout,verify_native=lambda observed:self._native(envelope,observed),execution=scope_owner)
            if type(outcome) is not ExportOutcome or outcome.owner is not scope_owner:raise ExportJobError('Runtime discarded or changed its live execution owner')
            runtime=outcome.receipt
            if runtime['state'] not in ('completed','failed','cancelled','unknown'):raise ExportJobError('Unknown runtime state')
            if runtime['process_disposition']=='stopped' and (not runtime['scopes'] or any(s.get('disposition')!='stopped' or s.get('active_processes')!=0 or s.get('streams_closed') is not True for s in runtime['scopes'])):
                raise ExportJobError('Runtime omitted actual owned process cessation evidence')
            if runtime['state']=='completed':self._check_completed_runtime(envelope,runtime,execution)
        except Exception as exc:fault=type(exc).__name__+': '+str(exc)[:1000]
        finally:
            done.set()
            if watcher:watcher.join(3)
            if scope_owner:
                cleanup=scope_owner.stop()
                if scope_owner.closed:self._executions.pop(job['claim_token'],None)
        current=self.jobs.job(job['id'])
        if cleanup and (not cleanup['closed'] or cleanup['cleanup_errors']):fault=fault or 'Actual owned process or guard cleanup remains unknown'
        if runtime and runtime['process_disposition']=='stopped' and cleanup and runtime['scopes']!=cleanup['scopes']:fault=fault or 'Runtime proof differs from its live process owner'
        if faults or watcher and watcher.is_alive() or launched and (fault or runtime is None or runtime['state']=='unknown' or runtime['process_disposition']!='stopped') or cleanup and not cleanup['closed']:
            if current['state'] in ('running','cancel-requested'):current=self.jobs.mark_interrupted(job['id'],expected_revision=current['revision'],reason='cleanup_uncertain')
            self._write('uncertain',job['claim_token'],{'fault':fault,'controller_faults':faults,'outputs_retained':True,'resource_holds_retained':True,'live_execution':scope_owner.key if scope_owner else None,'guards_retained':bool(scope_owner and scope_owner.guards)})
            return ExportJobOutcome(current,scope_owner)
        if cancel.is_set() and current['state']=='running':current=self.jobs.request_cancel(job['id'],expected_revision=current['revision'])
        if current['state']=='interrupted':return ExportJobOutcome(current,scope_owner)
        outcome='cancelled' if current['cancel_requested'] else 'completed' if runtime and runtime['state']=='completed' and fault is None else 'failed'
        disposition='verified' if outcome=='completed' else 'quarantined' if execution.exists() else 'absent'
        summary={'schema':1,'job_id':job['id'],'claim_token':job['claim_token'],'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id'],'outcome':outcome,
            'runtime_sha256':hash_file(execution/'runtime.json') if runtime else None,'outputs_disposition':disposition,'fault':fault,'process_disposition':'stopped' if runtime else 'never_started'}
        result_ref=self._write('results',job['output_set_id'],summary)
        attested={k:summary[k] for k in ['job_id','claim_token','intent_sha256','output_set_id','process_disposition','outputs_disposition']};attested.update(result_ref=result_ref,evidence_ref=result_ref)
        self._attestations[job['claim_token']]={'attestation':attested,'owner':scope_owner}
        try:return ExportJobOutcome(self.jobs.finish(job['id'],expected_revision=current['revision'],claim_token=job['claim_token'],outcome=outcome,evidence_ref=result_ref),scope_owner)
        except StaleTransition:
            current=self.jobs.job(job['id'])
            if current['state'] in ('running','cancel-requested'):current=self.jobs.mark_interrupted(job['id'],expected_revision=current['revision'],reason='owner_lost')
            return ExportJobOutcome(current,scope_owner)
        finally:self._attestations.pop(job['claim_token'],None)
    def _verify(self,job,reference):
        live=self._attestations.get(job['claim_token'])
        if live is None:raise VerificationRequired('Only this live owned execution may settle render resources')
        attested=live['attestation'];owner=live['owner']
        if attested['evidence_ref']!=reference or owner is not None and not owner.closed:raise VerificationRequired('Live process and guard ownership is not settled')
        summary=self._read('results',reference)
        if summary['runtime_sha256'] is not None:
            path=self.directory/'executions'/job['output_set_id']/'runtime.json'
            if hash_file(path)!=summary['runtime_sha256']:raise VerificationRequired('Runtime proof changed before settlement')
            if summary['outcome']=='completed':
                self._check_completed_runtime(self._read('requests',job['input']['request_ref']),json.loads(path.read_text(encoding='utf8')),path.parent)
        return dict(attested)
    def cleanup_pending(self,jid,*,execution=None):
        """Requery a live owner only. This never retries render or settles DB holds."""
        if threading.current_thread() is threading.main_thread():raise ExportJobError('Run owned process cleanup on the background owner')
        if not self._busy.acquire(False):raise ExportJobError('This render owner is busy')
        try:
            job=self.jobs.job(jid)
            if job['state']!='interrupted':raise ExportJobError('Explicit cleanup requires an interrupted render')
            live=execution if execution is not None else self._executions.get(job['claim_token'])
            expected={'database':str(self.database.resolve()),'job_id':job['id'],'claim_token':job['claim_token'],'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id']}
            if type(live) is not ExportExecution or live.binding!=expected or not live.processes:raise ExportJobError('The original live execution owner is required; receipts and PIDs cannot replace it')
            retained=self._executions.get(job['claim_token'])
            if retained is not None and live is not retained:raise ExportJobError('A different live execution owner is already retained')
            proof=live.stop()
            reference=self._write('cleanup-attempts',str(uuid.uuid4()),{'job_id':jid,'scope':proof,'resource_holds_retained':True,'render_retried':False})
            if live.closed:self._executions.pop(job['claim_token'],None)
            return {'job':self.jobs.job(jid),'cleanup_ref':reference,'scope':proof,'resource_holds_retained':True}
        finally:self._busy.release()
    def completed_receipt(self,jid):
        job=self.jobs.job(jid)
        if job['state']!='completed':raise ExportJobError('Only settled completed native exports are deliverable')
        settlement=self.jobs.settlement(jid);summary=self._read('results',settlement['result_ref'])
        path=self.directory/'executions'/job['output_set_id']/'runtime.json'
        if hash_file(path)!=summary['runtime_sha256']:raise ExportJobError('Completed export runtime receipt changed')
        with path.open('rb') as stream:encoded=stream.read(32*1024*1024+1)
        if len(encoded)>32*1024*1024:raise ExportJobError('Runtime proof exceeds its bound')
        runtime=json.loads(encoded)
        envelope=self._read('requests',job['input']['request_ref']);self._bind(envelope,job['input'],live_paths=False)
        if (summary['job_id']!=job['id'] or summary['intent_sha256']!=job['intent_sha256'] or summary['output_set_id']!=job['output_set_id'] or summary['outcome']!='completed'
            or runtime['state']!='completed' or runtime['process_disposition']!='stopped' or runtime['engine']!=envelope['engine'] or runtime['snapshot_sha256']!=envelope['snapshot']['sha256']):raise ExportJobError('Incomplete or mismatched native proof')
        self._check_completed_runtime(envelope,runtime,path.parent)
        return {'schema':'clipline.native-export.completed/1','job_id':job['id'],'intent_sha256':job['intent_sha256'],'output_set_id':job['output_set_id'],'request':envelope,'runtime':runtime}
    def _check_completed_runtime(self,envelope,runtime,root):
        if runtime['state']!='completed' or runtime['process_disposition']!='stopped' or runtime['engine']!=envelope['engine'] or runtime['snapshot_sha256']!=envelope['snapshot']['sha256']:raise ExportJobError('Incomplete or mismatched native runtime')
        if not runtime['scopes'] or any(s.get('disposition')!='stopped' or s.get('active_processes')!=0 or s.get('streams_closed') is not True for s in runtime['scopes']):raise ExportJobError('Owned process cessation proof is incomplete')
        root=Path(root).resolve(strict=True)
        def owned(value):
            candidate=Path(value)
            if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve(strict=True).is_relative_to(root):raise ExportJobError('Export artifact escaped its owned execution')
            return candidate
        capture_path=owned(root/'native-capture.json')
        if hash_file(capture_path)!=runtime['capture_sha256']:raise ExportJobError('Native capture evidence changed')
        capture=json.loads(capture_path.read_text(encoding='utf8'))
        if runtime['native']['state']!='completed' or runtime['native']['capture']!=capture or capture['snapshotSHA256']!=envelope['snapshot']['sha256']:raise ExportJobError('Native terminal/capture binding differs')
        if any([o['composition'] for o in collection]!=envelope['compositions'] for collection in (runtime['outputs'],runtime['native']['outputs'],capture['outputs'])):raise ExportJobError('Completed composition inventory differs')
        for output in runtime['outputs']:
            media=owned(output['path'])
            if media!=root/'media'/(output['composition']+'.mp4') or hash_file(media)!=output['sha256']:raise ExportJobError('Completed exported media changed')
            captured=next(o for o in capture['outputs'] if o['composition']==output['composition'])
            terminal=next(o for o in runtime['native']['outputs'] if o['composition']==output['composition'])
            if (output['facts']!=captured['facts'] or output['sequence_id']!=captured['sequence'] or output['facts']!=terminal['facts'] or output['sequence_id']!=terminal['sequence']
                or output['native_job']!=terminal['job'] or output['terminal']!=terminal['terminal'] or terminal['terminal'].get('finished') is not True
                or terminal['terminal'].get('offlineMedia')!=[] or terminal['terminal'].get('result',{}).get('error') is not None
                or terminal['terminal'].get('result',{}).get('frames')!=output['facts']['frames']):raise ExportJobError('Output proof differs from native immutable capture/terminal')
            for key in ('probe','decode'):
                if hash_file(owned(output[key]['path']))!=output[key]['sha256']:raise ExportJobError('Completed decode/probe evidence changed')
            verify_probe(json.loads(Path(output['probe']['path']).read_text(encoding='utf8')),output['facts'])
            decode=json.loads(Path(output['decode']['path']).read_text(encoding='utf8'))
            if decode!={'schema':'clipline.native-export.decode/1','artifact_sha256':output['sha256'],'exit_code':0,'stderr_empty':True}:raise ExportJobError('Decode evidence does not bind this artifact')
