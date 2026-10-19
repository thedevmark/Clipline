"""Bounded prepared-audio owner. Invoke from the existing background job owner.

No models, media uploads, database, GUI, or timeline. Current profile supports
continuous zero-origin audio streams in local clips at most 180 seconds long.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib,json,math,os,re
from pathlib import Path
import subprocess,sys,threading,time,uuid,wave
from native.services.caption_worker.evidence import canonical,digest,validate_request,EvidenceError
from native.services.caption_worker.client import (
    write_once, ExecutionUncertain, _CapturedProcess, _sample_process,
    _release_process, _wait_bound, gated_command, gate_frame, start_registered,
)
from native.services.caption_worker.process_tree import ProcessTree

RATE=16000
MAX_SOURCE=2*1024**3
MAX_STDOUT=4*1024**2
MAX_STDERR=131072
MAX_CLOCK_LINE=4096
MAX_SOURCE_RATE=384000
FORMATS="mov,matroska,webm,wav,mp3,flac,ogg,mpegts"

class PreparationError(ValueError):
    def __init__(self,code,message):super().__init__(message);self.code=code

@dataclass(frozen=True)
class PreparedAudio:
    state:str
    error_code:str|None
    directory:Path
    request_path:Path|None
    owner:object|None=None
    def load_request(self):
        if self.request_path is None:return None
        value=json.loads(self.request_path.read_text(encoding="utf8"))
        receipt=json.loads((self.directory/"receipt.json").read_text(encoding="utf8"))
        if digest(value)!=receipt["request_sha256"]:raise PreparationError("REQUEST_CHANGED","Prepared request changed")
        validate_request(value)
        return value

def _integer(value,name,low=0,high=2**63-1):
    if type(value) is not int or not low<=value<=high:raise PreparationError("INVALID_INPUT",name+" is outside its integer bounds")
    return value

def _sha(value):
    if not isinstance(value,str) or len(value)!=64 or any(c not in "0123456789abcdef" for c in value):raise PreparationError("INVALID_INPUT","Expected SHA256 is required")
    return value

def _path(value):
    if not isinstance(value,(str,Path)) or str(value).startswith(("//","\\\\")) or not Path(value).is_absolute():raise PreparationError("INVALID_INPUT","Absolute local paths are required")
    path=Path(value).resolve(strict=True)
    if not path.is_file():raise PreparationError("INVALID_INPUT","A regular local file is required")
    return path

def _microseconds(value):
    try:
        n=Decimal(str(value))*1_000_000
        if not n.is_finite():raise ValueError()
        return int(n.quantize(Decimal(1),rounding=ROUND_HALF_UP))
    except (InvalidOperation,ValueError,OverflowError):raise PreparationError("UNSUPPORTED_CLOCK","Finite media timestamps are required")

def _source_read_guard(path):
    """On Windows, prevent concurrent writes/replacement while native tools read."""
    if os.name!="nt":return None
    import ctypes as c
    from ctypes import wintypes as w
    k=c.WinDLL("kernel32",use_last_error=True)
    k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
    k.CreateFileW.restype=w.HANDLE;k.CloseHandle.argtypes=[w.HANDLE]
    handle=k.CreateFileW(str(path),0x80000000,1,None,3,0,None) # READ, share READ only
    if not handle or handle==c.c_void_p(-1).value:raise PreparationError("SOURCE_BUSY","Source cannot be protected against concurrent modification")
    return k,handle

class DecodedClock:
    """Validate frame clocks in constant space; retain bounded samples and hash."""
    _record=re.compile(r"\bn:(\d+)\s+pts:(-?\d+|NOPTS)\s+pts_time:(\S+).*?\brate:(\d+)\s+nb_samples:(\d+)")
    def __init__(self,rate,duration_us):
        self.rate=rate;self.source_samples=math.ceil(duration_us*rate/1_000_000)
        self.max_frames=self.source_samples+1 # every audio frame has at least one sample
        self.frames={"origin":0,"clock":0};self.samples={"origin":0,"clock":0}
        self.max_frame_samples=0;self.hash=hashlib.sha256();self.first=[];self.last={}
    def observe(self,line):
        if b"ashowinfo" not in line or b" n:" not in line:return False
        if len(line)>MAX_CLOCK_LINE:raise PreparationError("OUTPUT_LIMIT","Decoded clock line exceeds its finite bound")
        text=line.decode("ascii",errors="replace")
        stage="origin" if "clipline_origin" in text else "clock" if "clipline_clock" in text else None
        match=self._record.search(text)
        if stage is None or match is None:raise PreparationError("UNSUPPORTED_CLOCK","Malformed decoded-frame clock")
        n,pts,seconds,rate,count=match.groups();n=int(n);rate=int(rate);count=int(count)
        if pts=="NOPTS" or n!=self.frames[stage] or n>=self.max_frames or rate!=self.rate or count<=0:
            raise PreparationError("UNSUPPORTED_CLOCK","Missing or changing decoded sample clock")
        pts=int(pts)
        if stage=="origin" and n==0:
            try:zero=Decimal(seconds)
            except InvalidOperation:raise PreparationError("UNSUPPORTED_CLOCK","Unknown decoded origin")
            if pts!=0 or not zero.is_finite() or zero!=0:raise PreparationError("UNSUPPORTED_CLOCK","First decoded sample is not source time zero")
        if stage=="clock" and pts!=self.samples[stage]:raise PreparationError("UNSUPPORTED_CLOCK","Decoded samples are discontinuous or shifted")
        self.max_frame_samples=max(self.max_frame_samples,count)
        self.samples[stage]+=count;self.frames[stage]+=1
        if self.samples[stage]>self.source_samples+self.max_frame_samples:raise PreparationError("UNSUPPORTED_CLOCK","Decoded duration contradicts bounded source metadata")
        self.hash.update(line)
        if len(self.first)<8:self.first.append(text.rstrip())
        self.last[stage]=text.rstrip()
        return True
    def finish(self,required_samples):
        if not self.frames["origin"] or self.frames["origin"]!=self.frames["clock"] or self.samples["origin"]!=self.samples["clock"] or self.samples["clock"]<required_samples:
            raise PreparationError("UNSUPPORTED_CLOCK","Decoded evidence does not cover the requested source clock")
        return {"verified":True,"source_zero_us":0,"sample_rate":self.rate,"sample_time_base":"1/"+str(self.rate),"frame_counts":self.frames,"decoded_samples":self.samples["clock"],"raw_clock_sha256":self.hash.hexdigest(),"first_record_lines":self.first,"last_record_lines":self.last,"max_frames_per_stage":self.max_frames,"max_record_bytes":MAX_CLOCK_LINE,"retention":"first eight and last record per stage; streaming hash and validation for every record"}

class CaptionAudioPreparer:
    def __init__(self,ffmpeg,ffprobe,*,python,gate=None):
        self.ffmpeg=ffmpeg;self.ffprobe=ffprobe;self.python=python
        self.gate=Path(gate) if gate else Path(__file__).with_name("caption_audio_gate.py")
        self._busy=threading.Lock()
        self._captured = None
        self._source_guard = None
        self._directory = None
        self._last_observation = None
        self._diagnostic_pending = False

    @property
    def has_unsettled(self):
        return (self._captured is not None or self._source_guard is not None
                or self._diagnostic_pending)

    def _release_source(self):
        if self._source_guard is not None:
            kernel, handle = self._source_guard
            if not kernel.CloseHandle(handle):
                raise OSError('The original source read guard could not be closed')
            self._source_guard = None

    def observe_execution(self, *, wait_seconds=0):
        return self._observe_execution(wait_seconds, stop=False)

    def cleanup_execution(self, *, wait_seconds=5):
        return self._observe_execution(wait_seconds, stop=True)

    def _observe_execution(self, wait_seconds, *, stop):
        _wait_bound(wait_seconds)
        if not self._busy.acquire(False):
            raise ExecutionUncertain('The original preparation operation is still running', self, self._directory)
        try:
            if not self.has_unsettled:
                return json.loads(canonical(self._last_observation or {
                    'ceased': True, 'resources_released': True,
                    'owner_unsettled': False, 'never_launched': True}))
            captured = self._captured
            proof = (_sample_process(captured, wait_seconds=wait_seconds, stop=stop)
                     if captured is not None else self._last_observation or {
                         'scope': {'disposition': 'never_started', 'active_processes': 0},
                         'parent_reaped': True, 'readers_stopped': True, 'streams_closed': True,
                         'ceased': True, 'resources_released': False, 'faults': []})
            self._diagnostic_pending = True
            write_once(self._directory / ('live-observation-' + str(uuid.uuid4()) + '.json'), proof)
            if proof['ceased']:
                if captured is not None:
                    _release_process(captured, proof)
                    self._last_observation = proof
                    self._captured = None
                self._release_source()
                write_once(self._directory / ('live-release-' + str(uuid.uuid4()) + '.json'),
                           {**proof, 'source_guard_released': True})
                self._diagnostic_pending = False
                self._last_observation = proof
            return json.loads(canonical({**proof, 'owner_unsettled': self.has_unsettled}))
        except BaseException as exc:
            raise ExecutionUncertain(exc, self, self._directory) from exc
        finally:
            self._busy.release()

    def prepare(self,selection,output_root,*,timeout=90,cancel=None):
        if not self._busy.acquire(False):
            raise ExecutionUncertain("An audio preparation job is already running", self, self._directory)
        try:
            if self.has_unsettled:
                raise ExecutionUncertain('Settle the original preparation before another launch', self, self._directory)
            return self._prepare(selection,Path(output_root),timeout,cancel or threading.Event())
        except BaseException as exc:
            if self.has_unsettled and not isinstance(exc, ExecutionUncertain):
                raise ExecutionUncertain(exc, self, self._directory) from exc
            raise
        finally:self._busy.release()

    def _tool(self, argv, label, *, directory, commands, checkpoint, clock=None):
        checkpoint();record={"label":label,"argv":argv,"process_scope":{"disposition":"never_started"}};commands.append(record)
        stdout=directory/(label+".stdout");stderr=directory/(label+".stderr")
        command=[str(self.python),"-u","-B","-S",str(self.gate)]
        process=tree=None;threads=[];errors=[];buffers=[bytearray(),bytearray()]
        try:
            env=os.environ.copy();env.update(TEMP=str(directory),TMP=str(directory),PYTHONDONTWRITEBYTECODE="1")
            prefix = gate_frame(command)
            process=subprocess.Popen(gated_command(self.python),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,start_new_session=os.name!="nt",creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
            self._captured = _CapturedProcess(directory, process, None, threads,
                                             gate_argv=tuple(process.args), gate_released=False)
            record["process_scope"]={"disposition":"unknown"}
            tree=ProcessTree(process)
            self._captured.tree = tree
            def drain(pipe,path,limit,index):
                try:
                    with path.open("xb") as log:
                        while True:
                            data=pipe.readline(MAX_CLOCK_LINE+1) if clock is not None and index==1 else pipe.read1(8192)
                            if not data:break
                            if clock is not None and index==1:
                                if len(data)>MAX_CLOCK_LINE:errors.append("OUTPUT_LIMIT");break
                                try:
                                    if clock.observe(data):continue
                                except PreparationError as exc:errors.append(exc.code);break
                            remaining=limit-len(buffers[index]);log.write(data[:remaining]);log.flush();buffers[index].extend(data[:remaining])
                            if len(data)>remaining:errors.append("OUTPUT_LIMIT");break
                except OSError:errors.append("PIPE_FAILURE")
                finally:pipe.close()
            for index,(pipe,path,limit) in enumerate(((process.stdout,stdout,MAX_STDOUT),(process.stderr,stderr,MAX_STDERR))):
                thread=threading.Thread(target=drain,args=(pipe,path,limit,index),daemon=True)
                start_registered(self._captured, thread)
            def send():
                try:
                    self._captured.gate_released = True
                    process.stdin.write(prefix + canonical(argv)+b"\n");process.stdin.close()
                except OSError:errors.append("PIPE_FAILURE")
            sender=threading.Thread(target=send,daemon=True)
            start_registered(self._captured, sender)
            while process.poll() is None:
                checkpoint()
                if errors:raise PreparationError(errors[0],"Native tool output or decoded clock failed validation")
                time.sleep(.02)
            for thread in threads:thread.join(2)
            if errors:raise PreparationError(errors[0],"Native tool output or decoded clock failed validation")
            record["exit_code"]=process.returncode
            if process.returncode:raise PreparationError("TOOL_FAILED","Native tool returned a nonzero exit code")
            return bytes(buffers[0])
        finally:
            if self._captured is not None:
                proof = _sample_process(self._captured, wait_seconds=5, stop=True)
                record.update(process_scope=proof['scope'], exit_code=proof['exit_code'],
                              streams_closed=proof['streams_closed'])
                if not proof['ceased']:
                    raise PreparationError('CLEANUP_FAILED', 'The original tool scope, parent or streams remain uncertain')
                write_once(directory / ('tool-observation-' + str(uuid.uuid4()) + '.json'), proof)
                _release_process(self._captured, proof)
                write_once(directory / ('tool-release-' + str(uuid.uuid4()) + '.json'), proof)
                self._last_observation = proof
                self._captured = None

    def _prepare(self,selection,root,timeout,cancel):
        directory=root/str(uuid.uuid4());directory.mkdir(parents=True,exist_ok=False)
        self._directory = directory
        self._last_observation = None
        started=time.monotonic();commands=[];request=None;error=None;state="failed";artifact=directory/"partial.wav";source_guard=None
        receipt={"schema_version":1,"job_id":directory.name,"state":"running","original_modified":False,"commands":commands,"profile_bounds":{"source_seconds":180,"prepared_seconds":180,"caption_range_seconds":120,"source_bytes":MAX_SOURCE,"audio_origin_us":0}}
        def checkpoint():
            if cancel.is_set():raise PreparationError("CANCELLED","Cancelled by the caller")
            if time.monotonic()-started>timeout:raise PreparationError("TIMEOUT","Preparation deadline expired")
        def fingerprint(path,maximum=MAX_SOURCE):
            checkpoint();before=path.stat()
            if before.st_size>maximum:raise PreparationError("INPUT_LIMIT","File exceeds bounded profile size")
            h=hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda:stream.read(1024**2),b""):
                    checkpoint();h.update(chunk)
            after=path.stat()
            if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):raise PreparationError("SOURCE_CHANGED","File changed during verification")
            return h.hexdigest()
        def run(argv,label,clock=None):
            return self._tool(argv,label,directory=directory,commands=commands,checkpoint=checkpoint,clock=clock)
        try:
            if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=300:raise PreparationError("INVALID_TIMEOUT","Deadline must be finite and in (0,300]")
            if not isinstance(selection,dict):raise PreparationError("INVALID_INPUT","Selection object required")
            required={"asset_id","source_path","source_sha256","stream_index","channels","mix","start_us","end_us","profile_path","profile_sha256"}
            if set(selection)-required-{"context_start_us","context_end_us","mix_assertion","kit_revision","vocabulary_revision"} or not required<=set(selection):raise PreparationError("INVALID_INPUT","Unknown or missing selection fields")
            try:
                if str(uuid.UUID(selection["asset_id"]))!=selection["asset_id"]:raise ValueError()
            except (ValueError,AttributeError,TypeError):raise PreparationError("INVALID_INPUT","Registered asset UUID required")
            source=_path(selection["source_path"]);expected=_sha(selection["source_sha256"])
            start=_integer(selection["start_us"],"start");end=_integer(selection["end_us"],"end")
            zero=_integer(selection.get("context_start_us",start),"context start");finish=_integer(selection.get("context_end_us",end),"context end")
            if not zero<=start<end<=finish or end-start>120_000_000 or finish-zero>180_000_000 or any((v*RATE)%1_000_000 for v in (zero,start,end,finish)):raise PreparationError("INVALID_RANGE","Ordered range/context must fit exact 16 kHz samples and profile bounds")
            index=_integer(selection["stream_index"],"stream index",0,64)
            channels=selection["channels"]
            if not isinstance(channels,list) or not 1<=len(channels)<=32:raise PreparationError("INVALID_INPUT","Explicit selected source channels required")
            for channel in channels:_integer(channel,"channel",0,31)
            if len(set(channels))!=len(channels):raise PreparationError("INVALID_INPUT","Duplicate selected channel")
            mix=selection["mix"]
            if mix not in ("unknown","mixed","isolated_microphone"):raise PreparationError("INVALID_INPUT","Explicit audio ownership selection required")
            if mix=="isolated_microphone" and (not isinstance(selection.get("mix_assertion"),str) or not selection["mix_assertion"].strip()):raise PreparationError("INVALID_INPUT","Isolation requires an explicit user assertion; it is never inferred")
            profile_path=_path(selection["profile_path"]);profile_hash=_sha(selection["profile_sha256"])
            if fingerprint(profile_path,65536)!=profile_hash:raise PreparationError("PROFILE_CHANGED","Profile hash changed")
            profile=json.loads(profile_path.read_text(encoding="utf8"))
            if not isinstance(profile,dict) or not isinstance(profile.get("assets"),list) or any(not isinstance(a,dict) for a in profile["assets"]):raise PreparationError("INVALID_PROFILE","Cached profile object and asset records required")
            ffmpeg=_path(self.ffmpeg);ffprobe=_path(self.ffprobe)
            tool_hash=fingerprint(ffmpeg,512*1024**2);probe_hash=fingerprint(ffprobe,512*1024**2)
            if profile.get("schema_version")!=1 or Path(profile["ffmpeg"]).resolve()!=ffmpeg or not any(a.get("role")=="ffmpeg" and a.get("sha256")==tool_hash for a in profile.get("assets",[])):raise PreparationError("PROFILE_TOOL_MISMATCH","Preparation FFmpeg must match the cached worker profile")
            source_guard=_source_read_guard(source)
            self._source_guard = source_guard
            receipt["source_read_policy"]="Windows share-read only; writes/replacement denied during verification and extraction" if source_guard else "pre/post fingerprint checks; concurrent mutation lock not qualified on this platform"
            if fingerprint(source)!=expected:raise PreparationError("SOURCE_CHANGED","Registered source fingerprint differs")
            receipt["source_identity"]={"asset_id":selection["asset_id"],"sha256":expected,"path":str(source),"audio_stream_index":index,"channels":channels,"mix":mix,"mix_evidence":"user_asserted_not_verified" if mix=="isolated_microphone" else "explicit_selection_not_classified"}
            receipt["tools"]={"ffmpeg":{"path":str(ffmpeg),"sha256":tool_hash},"ffprobe":{"path":str(ffprobe),"sha256":probe_hash}}
            version=run([str(ffmpeg),"-version"],"ffmpeg-version").decode("utf8",errors="replace").splitlines()[0]
            probe=json.loads(run([str(ffprobe),"-v","error","-protocol_whitelist","file","-format_whitelist",FORMATS,"-show_streams","-show_format","-of","json",str(source)],"probe"))
            streams=[s for s in probe.get("streams",[]) if s.get("index")==index and s.get("codec_type")=="audio"]
            if not streams:raise PreparationError("MISSING_AUDIO","Selected audio stream does not exist")
            stream=streams[0];rate=int(stream["sample_rate"]);count=int(stream["channels"])
            if not 0<rate<=MAX_SOURCE_RATE or not 0<count<=32 or any(c>=count for c in channels):raise PreparationError("INVALID_CHANNEL","Selected source channel is unavailable")
            if _microseconds(stream.get("start_time",0))!=0:raise PreparationError("UNSUPPORTED_CLOCK","This bounded profile requires zero-origin source audio")
            duration=_microseconds(stream.get("duration",probe.get("format",{}).get("duration")))
            if not 0<duration<=180_000_000 or finish>duration+int(1_000_000/rate):raise PreparationError("INVALID_RANGE","Source duration or chosen context exceeds this local clip profile")
            packets=json.loads(run([str(ffprobe),"-v","error","-protocol_whitelist","file","-format_whitelist",FORMATS,"-select_streams",str(index),"-show_packets","-show_entries","packet=pts_time,duration_time","-of","json",str(source)],"packet-clock"))["packets"]
            if not packets or len(packets)>32768:raise PreparationError("UNSUPPORTED_CLOCK","Bounded timestamp evidence required")
            previous=None;covers_zero=False;tolerance=max(2,math.ceil(1_000_000/rate))
            for packet in packets:
                pts=_microseconds(packet["pts_time"]);length=_microseconds(packet["duration_time"])
                if length<=0 or previous is not None and abs(pts-previous)>tolerance:raise PreparationError("UNSUPPORTED_CLOCK","Discontinuous audio timestamps require another preparation profile")
                if pts<=0<pts+length:covers_zero=True
                previous=pts+length
            if not covers_zero:raise PreparationError("UNSUPPORTED_CLOCK","Packet evidence does not establish the zero-origin source clock")
            weight=format(1/len(channels),".17g")
            pan="pan=mono|c0="+"+".join(weight+"*c"+str(c) for c in channels)
            first=zero*RATE//1_000_000;last=finish*RATE//1_000_000
            clock=DecodedClock(rate,duration)
            filters="ashowinfo@clipline_origin,asettb=expr=1/"+str(rate)+",ashowinfo@clipline_clock,"+pan+",aresample=16000,atrim=start_sample="+str(first)+":end_sample="+str(last)+",asetpts=PTS-STARTPTS"
            argv=[str(ffmpeg),"-nostdin","-v","info","-copyts","-threads","2","-protocol_whitelist","file","-format_whitelist",FORMATS,"-i",str(source),"-map","0:"+str(index),"-vn","-sn","-dn","-af",filters,"-ac","1","-ar","16000","-c:a","pcm_s16le","-f","wav",str(artifact)]
            run(argv,"extract",clock=clock)
            clock_receipt=clock.finish(math.ceil(finish*rate/1_000_000))
            clock_hash=write_once(directory/"decoded-clock.json",clock_receipt)
            if artifact.stat().st_size>RATE*180*2+1_048_576:raise PreparationError("PCM_LIMIT","Extracted PCM exceeds the bounded audio profile")
            with wave.open(str(artifact),"rb") as wav:
                if (wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes())!=(1,2,RATE,last-first) or wav.getcomptype()!="NONE":raise PreparationError("PCM_CLOCK_MISMATCH","Extracted PCM does not cover the requested exact sample clock")
                pcm=wav.readframes(wav.getnframes())
            if len(pcm)!=(last-first)*2:raise PreparationError("PCM_CLOCK_MISMATCH","Extracted PCM is truncated")
            if fingerprint(source)!=expected:raise PreparationError("SOURCE_CHANGED","Source changed during extraction")
            if fingerprint(ffmpeg,512*1024**2)!=tool_hash or fingerprint(ffprobe,512*1024**2)!=probe_hash or fingerprint(profile_path,65536)!=profile_hash:raise PreparationError("PROFILE_CHANGED","Tools or profile changed during extraction")
            prepared=directory/"prepared.wav";os.rename(artifact,prepared);artifact=prepared
            audio={"path":str(prepared.resolve()),"wav_sha256":fingerprint(prepared),"pcm_sha256":hashlib.sha256(pcm).hexdigest(),"sample_rate":RATE,"sample_count":last-first,"channels":1,"sample_width":2,"sample_zero_source_us":zero,
                   "extraction":{"argv":argv,"tool_version":version,"tool_sha256":tool_hash,"source_sha256":expected,"probe_sha256":probe_hash,"channel_policy":"arithmetic mean of explicit selected channels","clock_policy":"copyts; original decoded origin zero; continuous decoded sample clock; resample then exact sample trim","decoded_clock_sha256":clock_hash,"source_sample_rate":rate,"selected_stream":stream,"packet_clock_sha256":hashlib.sha256((directory/"packet-clock.stdout").read_bytes()).hexdigest()}}
            request={"schema_version":1,"run_id":str(uuid.uuid4()),"task":"transcribe","language":"en","source":{k:v for k,v in receipt["source_identity"].items() if k!="mix_evidence"},"audio":audio,"range":{"start_us":start,"end_us":end},"profile_path":str(profile_path),"profile_sha256":profile_hash,"kit_revision":selection.get("kit_revision"),"vocabulary_revision":selection.get("vocabulary_revision")}
            validate_request(request);receipt["request_sha256"]=write_once(directory/"request.json",request);state="complete"
        except PreparationError as exc:error=exc.code;receipt["error_message"]=str(exc)
        except EvidenceError as exc:error=exc.code;receipt["error_message"]=str(exc)
        except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError,wave.Error,OverflowError) as exc:error="PREPARATION_FAILED";receipt["error_message"]=type(exc).__name__+": "+str(exc)[:500]
        if error:
            state="cancelled" if error=="CANCELLED" else "partial" if artifact.exists() else "failed"
            if artifact.exists():
                size=artifact.stat().st_size
                receipt["partial_artifact"]={"path":str(artifact),"bytes":size,"sha256":hashlib.sha256(artifact.read_bytes()).hexdigest() if size<=8*1024**2 else None,"hash_omitted_reason":"oversize_partial" if size>8*1024**2 else None}
        receipt.update(state=state,error_code=error,elapsed_seconds_uncontrolled=time.monotonic()-started,request_sha256=receipt.get("request_sha256"))
        self._diagnostic_pending = True
        write_once(directory/"receipt.json",receipt)
        if self._captured is None:
            self._release_source()
            write_once(directory / ('source-release-' + str(uuid.uuid4()) + '.json'),
                       {'source_guard_released': True, 'last_process_observation': self._last_observation})
            self._diagnostic_pending = False
        return PreparedAudio(state,error,directory,directory/"request.json" if state=="complete" else None,
                             self if self.has_unsettled else None)

    @staticmethod
    def cover_range(start_us,end_us,duration_us):
        """Choose representable analysis bounds; never change canonical edit bounds.

        prepare still verifies actual decoded coverage and refuses truncated audio.
        """
        _integer(start_us,'Moment start');_integer(end_us,'Moment end');_integer(duration_us,'audio duration',1)
        start=start_us//125*125;end=(end_us+124)//125*125
        if not start_us<end_us or end>duration_us or end-start>120_000_000:
            raise PreparationError('INVALID_RANGE','A wider 16 kHz analysis range cannot cover this Moment within the audio and 120-second limit')
        return start,end

    def inspect_source(self, source_path, source_sha256, output_root, *, timeout=30, cancel=None):
        """Inspect real container indices through this preparer's retained tool boundary.

        Metadata is a selection aid. Only prepare verifies the decoded audio clock.
        """
        if not self._busy.acquire(False):
            raise ExecutionUncertain('An audio operation is already running',self,self._directory)
        try:
            if self.has_unsettled:
                raise ExecutionUncertain('Settle the original preparation first',self,self._directory)
            if type(timeout) not in (int,float) or not math.isfinite(timeout) or not 0<timeout<=60:
                raise PreparationError('INVALID_TIMEOUT','Inspection requires a finite deadline up to 60 seconds')
            cancel=cancel or threading.Event();started=time.monotonic()
            def checkpoint():
                if cancel.is_set():raise PreparationError('CANCELLED','Audio inspection cancelled')
                if time.monotonic()-started>timeout:raise PreparationError('TIMEOUT','Audio inspection deadline expired')
            source=_path(source_path);expected=_sha(source_sha256);probe=_path(self.ffprobe)
            directory=Path(output_root)/str(uuid.uuid4());directory.mkdir(parents=True,exist_ok=False)
            self._directory=directory;self._last_observation=None
            self._source_guard=_source_read_guard(source)
            def fingerprint(path,maximum):
                checkpoint();before=path.stat()
                if before.st_size>maximum:raise PreparationError('INPUT_LIMIT','Inspection input exceeds its bound')
                h=hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda:stream.read(1024**2),b''):checkpoint();h.update(chunk)
                after=path.stat()
                if (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino):
                    raise PreparationError('SOURCE_CHANGED','Inspection input changed')
                return h.hexdigest()
            if fingerprint(source,MAX_SOURCE)!=expected:raise PreparationError('SOURCE_CHANGED','Registered source fingerprint differs')
            probe_hash=fingerprint(probe,512*1024**2);commands=[]
            raw=self._tool([str(probe),'-v','error','-protocol_whitelist','file','-format_whitelist',FORMATS,'-show_streams','-show_format','-of','json',str(source)],'inspect',directory=directory,commands=commands,checkpoint=checkpoint)
            value=json.loads(raw);streams=value.get('streams')
            if not isinstance(streams,list) or len(streams)>128:raise PreparationError('INVALID_PROBE','Bounded stream inventory required')
            audio=[]
            for item in streams:
                if item.get('codec_type')!='audio':continue
                index=_integer(item['index'],'global stream index',0,64)
                count=int(item['channels']);rate=int(item['sample_rate'])
                _integer(count,'channel count',1,32);_integer(rate,'sample rate',1,MAX_SOURCE_RATE)
                duration=_microseconds(item.get('duration',value.get('format',{}).get('duration')))
                if not 0<duration<=180_000_000:raise PreparationError('INVALID_RANGE','Audio exceeds the 180 second local profile')
                if _microseconds(item.get('start_time',0))!=0:raise PreparationError('UNSUPPORTED_CLOCK','Only zero-origin audio is supported')
                audio.append({'stream_index':index,'channels':list(range(count)),'sample_rate':rate,'duration_us':duration})
            if len({x['stream_index'] for x in audio})!=len(audio):raise PreparationError('INVALID_PROBE','Duplicate stream indices')
            if fingerprint(source,MAX_SOURCE)!=expected or fingerprint(probe,512*1024**2)!=probe_hash:raise PreparationError('SOURCE_CHANGED','Inspection inputs changed')
            result={'source_path':str(source),'source_sha256':expected,'streams':audio,'decoded_clock_verified':False}
            self._diagnostic_pending=True
            write_once(directory/'inspection.json',{'schema':1,**result,'commands':commands,'ffprobe_sha256':probe_hash})
            if self._captured is None:
                self._release_source();self._diagnostic_pending=False
            return result
        except BaseException as exc:
            # The same preparer owns every captured tool and read guard. Never drop
            # these on diagnostic failure or an unknown cleanup result.
            if self._captured is None and not self._diagnostic_pending:
                self._release_source()
            if self.has_unsettled and not isinstance(exc,ExecutionUncertain):
                raise ExecutionUncertain(exc,self,self._directory) from exc
            raise
        finally:self._busy.release()
