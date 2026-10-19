"""Private headless export runtime candidate. No engine command retry or GUI.

The helper gates before Session construction, so the parent assigns its Windows
Job Object before any native document/codec work. Probe/decode tools use the
existing caption_audio_gate with their own owned scope. Every process and pipe
must stop before the durable owner can release resource admission.
"""
from dataclasses import dataclass
from pathlib import Path
from fractions import Fraction
import json,math,os,queue,re,subprocess,threading,time,uuid
from native.services.caption_worker.process_tree import ProcessTree
from native.services.caption_worker.evidence import canonical,digest,hash_file
from native.services.caption_worker.client import write_once
from native.services.caption_audio import _source_read_guard

MAX_WIRE=16*1024*1024
class ExportRuntimeError(RuntimeError):pass
class ExportCancelled(ExportRuntimeError):pass
def finite_json(encoded):
    def invalid(value):raise ExportRuntimeError('Non-finite JSON number: '+value)
    return json.loads(encoded,parse_constant=invalid)

@dataclass(frozen=True)
class QualifiedProgram:
    path:Path
    sha256:str
    def verify(self):
        if not self.path.is_absolute() or not self.path.is_file() or hash_file(self.path)!=self.sha256:
            raise ExportRuntimeError('Qualified local executable changed')

class ExportExecution:
    """Retain process handles and read guards until actual cessation is known."""
    def __init__(self,binding):
        self.binding=json.loads(canonical(binding));self.key=digest(self.binding)
        self.processes=[];self.guards={};self.closed=False;self.proof={};self._lock=threading.RLock()
    def hold(self,path,expected):
        with self._lock:
            if self.closed:raise ExportRuntimeError('Execution owner is already closed')
            path=Path(path).resolve(strict=True)
            if path not in self.guards:
                guard=_source_read_guard(path)
                if os.name=='nt' and guard is None:raise ExportRuntimeError('Windows read guard was not acquired')
                self.guards[path]=(guard,expected)
            elif self.guards[path][1]!=expected:raise ExportRuntimeError('Conflicting read-guard fingerprint')
            if hash_file(path)!=expected:raise ExportRuntimeError('Guarded source or program bytes changed')
    def register(self,process):
        with self._lock:
            if self.closed:raise ExportRuntimeError('Execution owner is already closed')
            self.processes.append(process)
    def stop(self):
        with self._lock:
            if self.closed:return self.proof
            scopes=[];cleanup_errors=[]
            for process in self.processes:
                try:scopes.append(process.close())
                except Exception as exc:
                    scopes.append(dict(process.proof));cleanup_errors.append(str(exc)[:500])
            stopped=all(process.closed for process in self.processes)
            faults=[]
            if stopped:
                for path,(guard,expected) in list(self.guards.items()):
                    try:
                        if guard and not guard[0].CloseHandle(guard[1]):raise OSError('CloseHandle did not confirm release')
                        del self.guards[path]
                    except Exception as exc:faults.append(str(exc)[:500])
            self.closed=stopped and not self.guards
            self.proof={'owner':self.key,'binding':self.binding,'scopes':scopes,'processes_stopped':stopped,
                'guards_released':not self.guards,'guarded_paths':[str(path) for path in self.guards],'closed':self.closed,'guard_faults':faults,'cleanup_errors':cleanup_errors}
            return self.proof

@dataclass(frozen=True)
class ExportOutcome:
    receipt:dict
    owner:ExportExecution

class OwnedProcess:
    def __init__(self,argv,directory,cancel,deadline,*,events=False,execution=None):
        self.directory=directory;directory.mkdir(exist_ok=False)
        self.cancel=cancel;self.deadline=deadline;self.events=queue.Queue(8) if events else None
        self.faults=[];self.threads=[];self.process=None;self.tree=None;self.proof={'disposition':'never_started','active_processes':0};self.closed=False
        self._closing=threading.Lock();self.close_attempts=0
        self.execution_owner=execution
        self.gate_released=False
        write_once(directory/'launch.json',{'argv':argv,'gate_state_at_launch':'not_released'})
        if execution is not None:execution.register(self)
        try:
            self.process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,env={**os.environ,'RAYON_NUM_THREADS':'2','OMP_NUM_THREADS':'2'})
            self.proof={'disposition':'unknown'};self.tree=ProcessTree(self.process)
            for pipe,name,limit in [(self.process.stdout,'stdout.log',MAX_WIRE),(self.process.stderr,'stderr.log',1024*1024)]:
                thread=threading.Thread(target=self._read,args=(pipe,name,limit),daemon=True);thread.start();self.threads.append(thread)
        except BaseException:
            self.close();raise
    def _read(self,pipe,name,limit):
        try:
            total=0
            with (self.directory/name).open('xb') as output:
                while True:
                    line=pipe.readline(min(limit+1,MAX_WIRE+1))
                    if not line:break
                    total+=len(line)
                    if total>limit:raise ExportRuntimeError('Native/tool output exceeds finite bound')
                    output.write(line)
                    if name=='stdout.log' and self.events is not None:
                        value=finite_json(line)
                        self.events.put_nowait(value)
        except Exception as exc:self.faults.append(str(exc)[:1000])
        finally:pipe.close()
    def guard(self):
        if self.cancel.is_set():raise ExportCancelled('Export cancellation requested')
        if time.monotonic()>=self.deadline:raise ExportRuntimeError('Export set deadline expired')
        if self.faults:raise ExportRuntimeError('Owned child output failed: '+self.faults[0])
    def send(self,value):
        self.guard();self.gate_released=True;self.process.stdin.write(value);self.process.stdin.flush()
    def event(self,expected):
        while True:
            self.guard()
            try:value=self.events.get(timeout=.05)
            except queue.Empty:
                if self.process.poll() is not None and not self.threads[0].is_alive():raise ExportRuntimeError('Native process ended without its expected reply')
                continue
            if not isinstance(value,dict) or value.get('event')!=expected:raise ExportRuntimeError('Unexpected native reply; no mutation will be retried')
            return value
    def wait(self):
        while self.process.poll() is None:self.guard();time.sleep(.025)
        self.guard();return self.process.returncode
    def close(self):
        with self._closing:
            if self.closed:return self.proof
            self.close_attempts+=1
            try:
                if self.tree:self.proof=self.tree.stop()
                elif self.process:
                    if not self.gate_released:
                        if self.process.poll() is None:self.process.kill()
                        self.process.wait(timeout=5)
                        self.proof={'disposition':'stopped','scope':'unreleased_gate_process','active_processes':0,
                            'gate_released':False,'reason':'Exact owned gate parent reaped before any instruction'}
                    else:
                        self.proof={'disposition':'unknown','reason':'Job Object attachment was not proven after gate release'}
                stopped=self.process is None or self.proof.get('disposition')=='stopped' and self.proof.get('active_processes')==0
                if stopped:
                    if self.process:
                        if self.process.stdin and not self.process.stdin.closed:self.process.stdin.close()
                        self.process.wait(timeout=5)
                    for thread in self.threads:thread.join(3)
                    if any(t.is_alive() for t in self.threads):self.proof={**self.proof,'disposition':'unknown','reason':'owned pipe reader remains active'}
                    else:
                        for pipe in (self.process.stdout,self.process.stderr) if self.process else ():
                            if pipe is not None and not pipe.closed:pipe.close()
                        if self.tree:self.tree.close()
                        self.closed=True
            except Exception as exc:self.proof={**self.proof,'disposition':'unknown','reason':str(exc)[:1000]}
            self.proof={**self.proof,'exit_code':self.process.returncode if self.process else None,'streams_closed':all(not t.is_alive() for t in self.threads),'close_attempt':self.close_attempts}
            name='stop.json' if self.close_attempts==1 else f'stop-{self.close_attempts:03}.json'
            write_once(self.directory/name,self.proof)
            return self.proof

class NativeExportClient:
    def __init__(self,helper,ffprobe,ffmpeg,python,gate,*,engine_version,source_pin_sha256):
        self.helper=helper;self.ffprobe=ffprobe;self.ffmpeg=ffmpeg;self.python=python;self.gate=Path(gate)
        self.engine_version=engine_version;self.source_pin_sha256=source_pin_sha256;self.gate_sha256=hash_file(self.gate)
    def admit(self,execution):
        if type(execution) is not ExportExecution:raise ExportRuntimeError('A live export execution owner is required')
        for program in (self.helper,self.ffprobe,self.ffmpeg,self.python):execution.hold(program.path,program.sha256)
        execution.hold(self.gate,self.gate_sha256)
    def _tool(self,program,args,directory,cancel,deadline,execution):
        execution.hold(program.path,program.sha256);execution.hold(self.python.path,self.python.sha256);execution.hold(self.gate,self.gate_sha256)
        program.verify();self.python.verify()
        if hash_file(self.gate)!=self.gate_sha256:raise ExportRuntimeError('Owned tool gate changed')
        child=OwnedProcess([str(self.python.path),'-I',str(self.gate)],directory,cancel,deadline,execution=execution)
        try:
            child.send(canonical([str(program.path),*args])+b'\n');child.process.stdin.close()
            code=child.wait()
        finally:child.close()
        if not child.closed:raise ExportRuntimeError('Owned tool scope remains unknown; no further tool will launch')
        if code!=0:raise ExportRuntimeError('Probe/decode child failed')
        return directory/'stdout.log',directory/'stderr.log'
    def run(self,envelope,directory,*,cancel,timeout,verify_native,execution):
        if os.name!='nt':raise ExportRuntimeError('Owned render runtime is qualified on Windows only')
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=3600:raise ExportRuntimeError('A finite deadline within one hour is required')
        self.admit(execution)
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=False);deadline=time.monotonic()+timeout;child=None;native=None;fault=None;state='unknown';outputs=[]
        working=directory/'working.fcproj';snapshot=Path(envelope['snapshot']['path'])
        with snapshot.open('rb') as source,working.open('xb') as target:
            remaining=64*1024*1024+1
            while remaining:
                block=source.read(min(1024*1024,remaining))
                if not block:break
                target.write(block);remaining-=len(block)
            target.flush();os.fsync(target.fileno())
        if not remaining or hash_file(working)!=envelope['snapshot']['sha256']:raise ExportRuntimeError('Pinned native snapshot bytes changed')
        execution.hold(working,envelope['snapshot']['sha256'])
        try:
            self.helper.verify()
            child=OwnedProcess([str(self.helper.path),str(working)],directory/'native',cancel,deadline,events=True,execution=execution)
            child.send(b'start\n');ready=child.event('ready')['observation'];verify_native(ready)
            inventory=[{'owner':o['owner'],'revision':o['linksRevision']} for o in ready['validation']['owners']]
            if inventory!=envelope['owners']:raise ExportRuntimeError('The immutable snapshot owner inventory differs from its queued intent')
            media=directory/'media';media.mkdir()
            params={'expectedProjectPath':str(working),'expectedRevision':ready['revision'],'expectedLinksRevisions':inventory,
                'expectedSnapshotSHA256':envelope['snapshot']['sha256'],'expectedOwnerMetadataSHA256':ready['ownerMetadataSHA256'],
                'expectedRecipeSHA256':envelope['recipe_sha256'],'outputs':[{'composition':mode,'sequence':ready['validation']['owners'][0]['sequences'][mode],'path':str(media/(mode+'.mp4'))} for mode in envelope['compositions']]}
            write_once(directory/'capture-intent.json',params);child.send(canonical(params)+b'\n')
            capture=child.event('captured')['capture'];verify_native(capture)
            if capture['snapshotSHA256']!=envelope['snapshot']['sha256'] or capture['recipeSHA256']!=envelope['recipe_sha256'] or capture['ownerMetadataSHA256']!=ready['ownerMetadataSHA256']:raise ExportRuntimeError('Native capture differs from its pinned observation')
            write_once(directory/'native-capture.json',capture);child.send(b'render\n');child.process.stdin.close()
            native=child.event('result')['result'];code=child.wait()
            if code!=0 or native['capture']!=capture:raise ExportRuntimeError('Native terminal response differs from immutable capture')
            child.close()
            if not child.closed:raise ExportRuntimeError('Owned native scope remains unknown; verification tools will not launch')
            child=None
            if native['state']=='failed':state='failed'
            elif native['state']!='completed':raise ExportRuntimeError('Unknown native terminal state')
            else:
                if [o['composition'] for o in native['outputs']]!=envelope['compositions'] or len(native['outputs'])!=len(capture['outputs']):raise ExportRuntimeError('Native terminal output set is incomplete or changed')
                for output in native['outputs']:
                    mode=output['composition'];path=Path(output['path'])
                    if path!=media/(mode+'.mp4'):raise ExportRuntimeError('Native output escaped its owned namespace')
                    captured_output=next(o for o in capture['outputs'] if o['composition']==mode)
                    terminal=output['terminal']
                    if (output['facts']!=captured_output['facts'] or output['sequence']!=captured_output['sequence']
                        or terminal.get('finished') is not True or terminal.get('offlineMedia')!=[]
                        or terminal.get('result',{}).get('error') is not None
                        or terminal.get('result',{}).get('frames')!=output['facts']['frames']):raise ExportRuntimeError('Native job terminal does not attest the captured output')
                    before=hash_file(path)
                    probe_out,probe_err=self._tool(self.ffprobe,['-v','error','-count_frames','-show_streams','-show_format','-of','json',str(path)],directory/(mode+'-probe'),cancel,deadline,execution)
                    probe=finite_json(probe_out.read_text(encoding='utf8'));verify_probe(probe,output['facts'])
                    _,decode_err=self._tool(self.ffmpeg,['-v','error','-threads','2','-i',str(path),'-f','null','-'],directory/(mode+'-decode'),cancel,deadline,execution)
                    if probe_err.stat().st_size or decode_err.stat().st_size or hash_file(path)!=before:raise ExportRuntimeError('Export changed or failed full decode')
                    decode={'schema':'clipline.native-export.decode/1','artifact_sha256':before,'exit_code':0,'stderr_empty':True}
                    write_once(directory/(mode+'-decode.json'),decode)
                    outputs.append({'composition':mode,'sequence_id':output['sequence'],'path':str(path),'sha256':before,'bytes':path.stat().st_size,'facts':output['facts'],'native_job':output['job'],'terminal':output['terminal'],
                        'probe':{'path':str(probe_out),'sha256':hash_file(probe_out)},'decode':{'path':str(directory/(mode+'-decode.json')),'sha256':hash_file(directory/(mode+'-decode.json'))}})
                verify_native(capture);self.helper.verify()
                if hash_file(working)!=envelope['snapshot']['sha256'] or hash_file(snapshot)!=envelope['snapshot']['sha256']:
                    raise ExportRuntimeError('Snapshot bytes changed during native export')
                state='completed'
        except ExportCancelled as exc:state='cancelled';fault=str(exc)
        except Exception as exc:fault=type(exc).__name__+': '+str(exc)[:2000]
        finally:
            cleanup=execution.stop()
        scopes=cleanup['scopes']
        ceased=cleanup['closed'] and not cleanup['cleanup_errors'] and bool(scopes) and all(s.get('disposition')=='stopped' and s.get('active_processes')==0 and s.get('streams_closed') for s in scopes)
        if not ceased:state='unknown'
        receipt={'schema':1,'state':state,'fault':fault,'process_disposition':'stopped' if ceased else 'unknown','scopes':scopes,'snapshot_sha256':envelope['snapshot']['sha256'],'native':native,'outputs':outputs,
            'engine':{'version':self.engine_version,'helper_sha256':self.helper.sha256,'source_pin_sha256':self.source_pin_sha256},
            'tools':{'ffprobe':self.ffprobe.sha256,'ffmpeg':self.ffmpeg.sha256,'python':self.python.sha256,'gate':self.gate_sha256},
            'capture_sha256':hash_file(directory/'native-capture.json') if (directory/'native-capture.json').is_file() else None,
            'quarantined':state!='completed','execution_owner':{'key':execution.key,'closed':execution.closed,'guards_retained':bool(execution.guards)}}
        write_once(directory/'runtime.json',receipt)
        if state!='completed':write_once(directory/'QUARANTINE.json',{'state':state,'retained':True,'export_not_deliverable':True})
        return ExportOutcome(receipt,execution)

NATIVE_TICKS_PER_SECOND=254016000000
MP4_MOVIE_TICK=Fraction(1,1000)

def _timing_int(value,*,positive=False):
    if type(value) is not int or not -(2**63)<=value<2**63 or positive and value<=0:
        raise ExportRuntimeError('Output timing requires bounded native integer facts')
    return value

def _presented_duration(stream):
    base=stream.get('time_base')
    if type(base) is not str or re.fullmatch(r'[1-9][0-9]{0,18}/[1-9][0-9]{0,18}',base) is None:
        raise ExportRuntimeError('Output time base is missing or unsupported')
    numerator,denominator=map(int,base.split('/'))
    _timing_int(numerator,positive=True);_timing_int(denominator,positive=True)
    time_base=Fraction(numerator,denominator)
    if time_base>1 or _timing_int(stream.get('start_pts'))!=0:
        raise ExportRuntimeError('Output presentation must start at zero')
    return _timing_int(stream.get('duration_ts'),positive=True)*time_base

def _verify_durations(video,audio,facts):
    # Pinned native verification constants, not a Python edit or range model.
    # time/src/lib.rs TICKS_PER_SECOND; export/audio_out.rs floors range samples;
    # isobmff/mux.rs defaults movie timescale to 1000 and floors edit durations.
    try:
        frames=_timing_int(facts['frames'],positive=True)
        num=_timing_int(facts['frameRate']['num'],positive=True)
        den=_timing_int(facts['frameRate']['den'],positive=True)
        native_range=facts['range'];native_audio=facts['audio']
        ticks=_timing_int(native_range['durationTicks'],positive=True)
        if (_timing_int(native_range['startTicks'])!=0 or ticks>(2**63-1)//4
            or type(native_audio) is not dict or set(native_audio)!={'sampleRate','channels'}
            or _timing_int(native_audio['sampleRate'],positive=True)!=48000
            or _timing_int(native_audio['channels'],positive=True)!=2
            or NATIVE_TICKS_PER_SECOND*den%num):
            raise ExportRuntimeError('Unsupported captured native range or audio profile')
        frame=Fraction(den,num);expected_video=frames*frame
        native_duration=Fraction(ticks,NATIVE_TICKS_PER_SECOND)
        if not expected_video-frame<native_duration<=expected_video:
            raise ExportRuntimeError('Captured native range differs from its frame span')
        presented_video=_presented_duration(video);presented_audio=_presented_duration(audio)
        # Native MP4 edit lists can remove less than one 1ms movie tick.
        if not 0<=expected_video-presented_video<MP4_MOVIE_TICK:
            raise ExportRuntimeError('Video duration differs from the captured frame span')
        audio_samples=ticks*48000//NATIVE_TICKS_PER_SECOND
        expected_audio=Fraction(audio_samples,48000)
        # Native AAC zero-pads its final 1024-sample access unit; priming is
        # removed by its edit list. Packet capacity is not presented duration.
        if (presented_audio*48000).denominator!=1 or not expected_audio-MP4_MOVIE_TICK<presented_audio<=expected_audio+Fraction(1023,48000):
            raise ExportRuntimeError('Audio duration differs from native input and bounded AAC padding')
    except (KeyError,TypeError,ZeroDivisionError) as exc:
        raise ExportRuntimeError('Native output timing facts are missing or malformed') from exc

def verify_probe(probe,facts):
    video=[s for s in probe.get('streams',[]) if s.get('codec_type')=='video'];audio=[s for s in probe.get('streams',[]) if s.get('codec_type')=='audio']
    if len(video)!=1 or len(audio)!=1:raise ExportRuntimeError('Native video/stereo audio streams missing or ambiguous')
    v=video[0];a=audio[0];rate=f'{facts["frameRate"]["num"]}/{facts["frameRate"]["den"]}'
    if (v.get('codec_name')!='h264' or (v.get('width'),v.get('height'))!=(facts['width'],facts['height'])
        or v.get('sample_aspect_ratio')!='1:1' or v.get('r_frame_rate')!=rate or v.get('avg_frame_rate')!=rate
        or v.get('nb_read_frames')!=str(facts['frames']) or v.get('nb_frames')!=str(facts['frames'])
        or a.get('codec_name')!='aac' or a.get('sample_rate')!='48000' or a.get('channels')!=2):
        raise ExportRuntimeError('Decoded export metadata differs from native captured settings/frame clock')
    _verify_durations(v,a,facts)
