"""Read-only local-video admission through the existing retained tool owner."""
from pathlib import Path
import json,sys,threading,time,uuid
from native.core.creator_library import _fingerprint,LibraryError
from native.services.caption_audio import CaptionAudioPreparer,_path,_source_read_guard,MAX_SOURCE
from native.services.caption_worker.client import ExecutionUncertain,write_once
from native.services.caption_worker.evidence import hash_file
from native.services.tools import TOOLS
from native.services.twitch_intake_client import classify_probe


class LocalVideoProbe(CaptionAudioPreparer):
    def __init__(self):
        # The video-only inspection uses no FFmpeg extraction or model runtime.
        super().__init__(None,TOOLS.ffprobe,python=sys.executable)

    def inspect_video(self,asset,output_root,*,cancel=None):
        if not self._busy.acquire(False):
            raise ExecutionUncertain('The original source inspection is still running',self,self._directory)
        try:
            if self.has_unsettled:
                raise ExecutionUncertain('Check or stop the original source inspection first',self,self._directory)
            cancel=cancel or threading.Event();started=time.monotonic()
            def checkpoint():
                if cancel.is_set():raise LibraryError('Source inspection cancelled.')
                if time.monotonic()-started>30:raise LibraryError('Source inspection exceeded its 30-second limit.')
            source=_path(asset.path)
            if not Path(self.ffprobe).is_absolute() or not Path(self.ffprobe).is_file():
                raise LibraryError('FFprobe is unavailable. Install the local FFmpeg tools, then restart Clipline.')
            probe=_path(self.ffprobe)
            if source.stat().st_size>MAX_SOURCE:raise LibraryError('This local profile supports files up to 2 GiB.')
            directory=Path(output_root)/str(uuid.uuid4());directory.mkdir(parents=True,exist_ok=False)
            self._directory=directory;self._last_observation=None
            self._source_guard=_source_read_guard(source)
            if _fingerprint(source)[1:]!=(asset.sha256,asset.bytes):raise LibraryError('The registered local source changed.')
            probe_hash=hash_file(probe);commands=[]
            raw=self._tool([str(probe),'-v','error','-protocol_whitelist','file','-format_whitelist','mov',
                '-show_streams','-show_format','-of','json',str(source)],'local-video',directory=directory,commands=commands,checkpoint=checkpoint)
            def invalid(_):raise LibraryError('The source probe returned a non-finite value.')
            facts=classify_probe(json.loads(raw,parse_constant=invalid))
            checkpoint()
            if _fingerprint(source)[1:]!=(asset.sha256,asset.bytes) or hash_file(probe)!=probe_hash:
                raise LibraryError('The local source or probe changed during inspection.')
            self._diagnostic_pending=True
            write_once(directory/'inspection.json',{'schema':1,'asset_id':asset.id,'source_sha256':asset.sha256,
                'source_facts':facts,'ffprobe_sha256':probe_hash,'commands':commands,'native_compatibility':False})
            self._release_source();self._diagnostic_pending=False
            return facts
        except BaseException as exc:
            if self._captured is None and not self._diagnostic_pending:self._release_source()
            if self.has_unsettled and not isinstance(exc,ExecutionUncertain):
                raise ExecutionUncertain(exc,self,self._directory) from exc
            if isinstance(exc,ValueError) and not isinstance(exc,LibraryError):
                raise LibraryError('Choose an H.264 MP4 up to 180 seconds, with square pixels and optional mono/stereo AAC audio.') from exc
            raise
        finally:self._busy.release()
