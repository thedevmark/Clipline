"""Qualified local transfer/probe client using the shared owned-tool boundary."""
from dataclasses import dataclass, replace
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import time
import sys
from types import MappingProxyType

from native.services.native_export_client import OwnedProcess, QualifiedProgram, finite_json
from native.services.durable_native_exports import ExportOwnershipError
from native.services.caption_worker.evidence import canonical, hash_file, digest
from native.services.twitch_publication import intake_environment

PROFILE = {'id': 'clipline.twitch-own-source/1', 'max_bytes': 2*1024**3, 'max_duration_us': 180_000_000,
           'max_seconds': 300, 'container': 'mp4', 'video': 'h264', 'audio': 'optional-aac',
           'max_width': 3840, 'max_height': 2160, 'max_fps': 120, 'audio_channels': [1, 2],
           'audio_rates': [44100, 48000], 'pixel_formats': ['yuv420p','yuvj420p'],
           'native_compatibility': 'not-established-by-probe'}


class IntakeError(ValueError):
    pass


def classify_probe(value):
    """Source-admission facts only; never constructs native editing clocks."""
    if not isinstance(value, dict) or not isinstance(value.get('streams'), list) or not 1 <= len(value['streams']) <= 2:
        raise IntakeError('UNSUPPORTED_SOURCE')
    streams=value['streams']; videos=[s for s in streams if isinstance(s,dict) and s.get('codec_type')=='video']
    audios=[s for s in streams if isinstance(s,dict) and s.get('codec_type')=='audio']
    if len(videos)!=1 or len(audios)>1 or len(videos)+len(audios)!=len(streams):
        raise IntakeError('UNSUPPORTED_SOURCE')
    info=value.get('format'); video=videos[0]
    if not isinstance(info,dict) or 'mp4' not in str(info.get('format_name','')).split(',') or video.get('codec_name')!='h264':
        raise IntakeError('UNSUPPORTED_SOURCE')
    try:
        def rational(value,positive=False):
            if not isinstance(value,str) or not 1 <= len(value) <= 64: raise ValueError()
            parsed=Fraction(value)
            if positive and parsed<=0: raise ValueError()
            return parsed
        indexes=[s.get('index') for s in streams]
        if any(type(index) is not int or not 0 <= index <= 1024 for index in indexes) or len(set(indexes))!=len(indexes):
            raise ValueError()
        duration=rational(info['duration'],True); fps=rational(video['avg_frame_rate'],True)
        start=rational(video['start_time']); rational(video['time_base'],True)
        width=video['width']; height=video['height']
        if (not 0 < duration <= 180 or not 0 < fps <= 120 or start != 0
                or type(width) is not int or type(height) is not int
                or not 0 < width <= 3840 or not 0 < height <= 2160
                or video.get('sample_aspect_ratio')!='1:1'
                or video.get('pix_fmt') not in PROFILE['pixel_formats']
                or video.get('disposition',{}).get('attached_pic',0)!=0):
            raise ValueError()
        audio=None
        if audios:
            a=audios[0]
            if (a.get('codec_name')!='aac' or type(a.get('channels')) is not int or a['channels'] not in (1,2)
                    or a.get('sample_rate') not in ('44100','48000') or rational(a['start_time'])!=0
                    or a.get('channel_layout')!=('mono' if a['channels']==1 else 'stereo')):
                raise ValueError()
            rational(a['time_base'],True)
            audio={'codec':'aac','channels':a['channels'],'sample_rate':int(a['sample_rate']),
                   'start_time':a['start_time'],'time_base':a['time_base'],'index':a['index'],'channel_layout':a['channel_layout']}
        return {'container':'mp4','duration':str(duration),'video':{'codec':'h264','width':width,'height':height,
                'avg_frame_rate':str(fps),'start_time':video['start_time'],'sample_aspect_ratio':'1:1',
                'time_base':video['time_base'],'index':video['index'],'pix_fmt':video['pix_fmt']},'audio':audio,'native_compatibility':False}
    except (ValueError,TypeError,KeyError,ZeroDivisionError,OverflowError):
        raise IntakeError('UNSUPPORTED_SOURCE') from None


@dataclass(frozen=True)
class IntakeTools:
    python: QualifiedProgram
    worker: QualifiedProgram
    gate: QualifiedProgram
    ffprobe: QualifiedProgram
    code_root: Path
    code: dict
    client_id: str


class TwitchIntakeClient:
    def __init__(self,tools):
        if type(tools) is not IntakeTools or not isinstance(tools.client_id,str) or not 1 <= len(tools.client_id) <= 512:
            raise IntakeError('INVALID_RUNTIME')
        if not tools.code_root.is_absolute(): raise IntakeError('INVALID_CODE_CLOSURE')
        root=tools.code_root.resolve(strict=True)
        if not root.is_absolute() or not isinstance(tools.code,dict) or not 1 <= len(tools.code) <= 256:
            raise IntakeError('INVALID_CODE_CLOSURE')
        for rel,sha in tools.code.items():
            path=root/rel
            if (not isinstance(rel,str) or Path(rel).is_absolute() or '..' in Path(rel).parts
                    or path.suffix not in ('.py','.sql') or not isinstance(sha,str) or len(sha)!=64):
                raise IntakeError('INVALID_CODE_CLOSURE')
        if tools.worker.path.resolve()!=root/'native/services/twitch_transfer_worker.py':
            raise IntakeError('WORKER_OUTSIDE_CLOSURE')
        self.tools=replace(tools,code=MappingProxyType(dict(tools.code)))
        self._identity={'programs':{name:getattr(tools,name).sha256 for name in ('python','worker','gate','ffprobe')},
                       'code_sha256':digest(tools.code),'client_id':tools.client_id}

    @property
    def identity(self):
        return json.loads(canonical(self._identity))

    def admit(self,execution):
        root=self.tools.code_root.resolve(strict=True)
        actual={str(p.relative_to(root)).replace('\\','/') for p in (root/'native').rglob('*')
                if p.is_file() and p.suffix in ('.py','.sql')}
        if any((root/'native').rglob('*.pyc')):
            raise IntakeError('BYTECODE_CACHE_NOT_ADMITTED')
        if actual!=set(self.tools.code):
            raise IntakeError('CODE_CLOSURE_CHANGED')
        for relative,expected in self.tools.code.items():
            path=root/relative
            if path.is_symlink() or not path.resolve(strict=True).is_relative_to(root):
                raise IntakeError('INVALID_CODE_CLOSURE')
            execution.hold(path,expected)
        # Pin the actual loaded caller dependencies as well as the worker copy.
        # The closure is explicit; this never discovers executables or secrets.
        for name,module in list(sys.modules.items()):
            if name=='native' or name.startswith('native.'):
                filename=getattr(module,'__file__',None)
                if filename and Path(filename).suffix=='.py':
                    relative=name.replace('.','/')+('/__init__.py' if Path(filename).name=='__init__.py' else '.py')
                    expected=self.tools.code.get(relative)
                    if expected is None: raise IntakeError('CALLER_CODE_CLOSURE_INCOMPLETE')
                    execution.hold(Path(filename),expected)
        for name in ('python','worker','gate','ffprobe'):
            program=getattr(self.tools,name)
            if type(program) is not QualifiedProgram:
                raise IntakeError('QUALIFIED_PROGRAM_REQUIRED')
            program.verify(); execution.hold(program.path,program.sha256)

    def _owned(self,argv,wire,directory,execution,cancel,deadline,*,event=False):
        child=OwnedProcess(argv,Path(directory),cancel,deadline,events=event,execution=execution,
                           environment=intake_environment(os.environ))
        try:
            child.send(wire); child.process.stdin.close()
            result=child.event('result') if event else None
            status=child.wait()
        finally:
            try: child.close()
            except BaseException:
                if not child.closed:
                    raise ExportOwnershipError('INTAKE_STOP_UNKNOWN',[execution]) from None
                raise
            if not child.closed:
                raise ExportOwnershipError('INTAKE_STOP_UNKNOWN',[execution])
        return status,result,Path(directory)/'stdout.log',Path(directory)/'stderr.log'

    def transfer(self,selected,target,directory,execution,cancel,deadline):
        seconds=deadline-time.monotonic()
        if not 0 < seconds <= 300:
            raise IntakeError('DEADLINE')
        request={'schema':1,'code_root':str(self.tools.code_root),'clip':selected,'target':str(target),
                 'client_id':self.tools.client_id,'max_bytes':PROFILE['max_bytes'],'seconds':seconds}
        code,value,out,err=self._owned([str(self.tools.python.path),'-I','-B',str(self.tools.worker.path)],
            canonical(request)+b'\n',directory,execution,cancel,deadline,event=True)
        if err.stat().st_size:
            raise IntakeError('WORKER_DIAGNOSTIC_FAILED')
        if (not isinstance(value,dict) or set(value) not in ({'event','state','bytes'},{'event','state','error_code'})
                or value.get('state') not in ('completed','failed')):
            raise IntakeError('INVALID_WORKER_RESULT')
        if code!=0 or value['state']!='completed':
            code=value.get('error_code')
            if code not in {'AUTH_REQUIRED','SOURCE_UNAVAILABLE','INVALID_TRANSPORT','REDIRECT_LIMIT','INVALID_LIMIT',
                            'DEADLINE','HTTP_STATUS','INVALID_LENGTH','ENCODING','BYTE_LIMIT','INVALID_BODY','TRUNCATED',
                            'TRANSFER_FAILED','INVALID_REQUEST','DURATION_LIMIT','WORKER_FAILED'}:
                code='TRANSFER_FAILED'
            raise IntakeError(code)
        if type(value.get('bytes')) is not int or not 0 < value['bytes'] <= PROFILE['max_bytes'] or target.stat().st_size!=value['bytes']:
            raise IntakeError('TRANSFER_SIZE_CHANGED')

    def probe(self,target,directory,execution,cancel,deadline):
        args=[str(self.tools.ffprobe.path),'-v','error','-protocol_whitelist','file,pipe','-format_whitelist','mov',
              '-show_streams','-show_format','-of','json',str(target)]
        status,_,out,err=self._owned([str(self.tools.python.path),'-I','-B',str(self.tools.gate.path)],
            canonical(args)+b'\n',directory,execution,cancel,deadline)
        if status!=0 or err.stat().st_size or out.stat().st_size>1024*1024:
            raise IntakeError('PROBE_FAILED')
        return classify_probe(finite_json(out.read_bytes()))
