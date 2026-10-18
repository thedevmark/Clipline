import ctypes,hashlib,json,os,shutil,struct,sys,tempfile,threading,time,unittest,uuid,wave
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
from native.services.caption_worker.evidence import validate_request
from native.services import caption_audio as m
PYTHON=Path(sys.executable)
FFMPEG=Path(shutil.which("ffmpeg")) if shutil.which("ffmpeg") else None
FFPROBE=Path(shutil.which("ffprobe")) if shutil.which("ffprobe") else None
def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def alive(pid):
    if os.name!="nt":
        try:os.kill(pid,0);return True
        except ProcessLookupError:return False
    k=ctypes.WinDLL("kernel32",use_last_error=True);k.OpenProcess.restype=ctypes.c_void_p
    handle=k.OpenProcess(0x1000,False,pid)
    if not handle:return False
    code=ctypes.c_ulong();k.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.c_void_p];k.CloseHandle.argtypes=[ctypes.c_void_p]
    k.GetExitCodeProcess(handle,ctypes.byref(code));k.CloseHandle(handle);return code.value==259

class AudioTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="clipline-caption-audio-");self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)
        self.source=self.path/"stereo.wav"
        self.right=[1000+(i//16000)*100 for i in range(64000)]
        with wave.open(str(self.source),"wb") as w:
            w.setnchannels(2);w.setsampwidth(2);w.setframerate(16000)
            w.writeframes(b"".join(struct.pack("<hh",-x,x) for x in self.right))
        self.original=self.source.read_bytes()
        self.ffmpeg=self.path/"fake-ffmpeg.exe";self.ffmpeg.write_bytes(b"fake tool fixture")
        self.ffprobe=self.path/"fake-ffprobe.exe";self.ffprobe.write_bytes(b"fake probe fixture")
        self.gate=self.path/"fake_gate.py";self.gate.write_bytes((ROOT/"tests/fixtures/caption_audio_fake_gate.py").read_bytes())
        self.profile=self.path/"profile.json";self.profile_for(self.ffmpeg)
        self.selection={"asset_id":str(uuid.uuid4()),"source_path":str(self.source),"source_sha256":sha(self.source),"stream_index":0,"channels":[1],"mix":"unknown","start_us":1_500_000,"end_us":2_500_000,"context_start_us":1_000_000,"context_end_us":3_000_000,"profile_path":str(self.profile),"profile_sha256":sha(self.profile)}
        self.mode("normal")
        self.service=m.CaptionAudioPreparer(self.ffmpeg,self.ffprobe,python=PYTHON,gate=self.gate)
    def profile_for(self,tool):
        self.profile.write_text(json.dumps({"schema_version":1,"ffmpeg":str(tool),"assets":[{"role":"ffmpeg","sha256":sha(tool)}]}),encoding="utf8")
    def mode(self,mode):self.gate.with_name("mode.json").write_text(json.dumps({"mode":mode}))
    def run_job(self,**kw):return self.service.prepare(self.selection,self.path/"runs",**kw)
    def assert_failed(self,result,code):
        self.assertNotEqual(result.state,"complete");self.assertEqual(result.error_code,code);self.assertIsNone(result.load_request())
        receipt=json.loads((result.directory/"receipt.json").read_text());self.assertFalse(receipt["original_modified"])
        return receipt
    def test_interpreter_must_be_explicit(self):
        with self.assertRaises(TypeError):m.CaptionAudioPreparer(self.ffmpeg,self.ffprobe)
    def test_attachment_failure_closes_tool_pipes_without_claiming_stopped(self):
        opened=[];popen=m.subprocess.Popen
        def launch(*args,**kwargs):
            process=popen(*args,**kwargs);opened.append(process);return process
        with patch.object(m.subprocess,"Popen",side_effect=launch),patch.object(m,"ProcessTree",side_effect=OSError("injected attachment failure")):
            result=self.run_job()
        receipt=self.assert_failed(result,"PREPARATION_FAILED")
        self.assertEqual(len(opened),1)
        self.assertIsNotNone(opened[0].poll())
        self.assertTrue(all(pipe.closed for pipe in (opened[0].stdin,opened[0].stdout,opened[0].stderr)))
        self.assertEqual(receipt["commands"][0]["process_scope"]["disposition"],"unknown")
        self.assertTrue(receipt["commands"][0]["streams_closed"])
    def test_frozen_parent_uses_configured_python_not_gui_executable(self):
        original=m.subprocess.Popen
        with patch.object(m.sys,"executable",str(self.path/"clipline.exe")),patch.object(m.sys,"frozen",True,create=True),patch.object(m.subprocess,"Popen",wraps=original) as launch:
            result=self.run_job()
        self.assertEqual(result.state,"complete")
        self.assertTrue(launch.call_args_list)
        for call in launch.call_args_list:
            command=call.args[0]
            self.assertEqual(command[0],str(PYTHON))
            self.assertIn("-S",command)
            self.assertNotEqual(command[0],str(self.path/"clipline.exe"))
    def test_request_compatible_source_clock_and_selection(self):
        result=self.run_job();self.assertEqual(result.state,"complete")
        r=result.load_request();validate_request(r)
        self.assertEqual(r["audio"]["sample_zero_source_us"],1_000_000);self.assertEqual(r["audio"]["sample_count"],32000)
        self.assertEqual(r["source"]["channels"],[1]);self.assertEqual(r["source"]["mix"],"unknown")
        self.assertEqual(r["range"],{"start_us":1_500_000,"end_us":2_500_000})
        self.assertEqual(self.source.read_bytes(),self.original)
    def test_changed_source_rejected_before_process(self):
        self.source.write_bytes(b"foreign replacement")
        r=self.run_job();receipt=self.assert_failed(r,"SOURCE_CHANGED");self.assertEqual(receipt["commands"],[])
    def test_changed_source_during_extract_retains_partial_not_request(self):
        self.mode("changed");r=self.run_job();receipt=self.assert_failed(r,"TOOL_FAILED" if os.name=="nt" else "SOURCE_CHANGED")
        self.assertEqual(r.state,"partial");self.assertGreater(receipt["partial_artifact"]["bytes"],0)
        if os.name=="nt":self.assertEqual(self.source.read_bytes(),self.original)
    def test_source_guard_releases_after_success(self):
        r=self.run_job();self.assertEqual(r.state,"complete")
        self.source.write_bytes(self.original+b"caller can edit after preparation")
        self.assertTrue(self.source.read_bytes().endswith(b"preparation"))
    def test_missing_selected_stream(self):
        self.mode("missing");self.assert_failed(self.run_job(),"MISSING_AUDIO")
    def test_unavailable_selected_channel(self):
        self.selection["channels"]=[2];self.assert_failed(self.run_job(),"INVALID_CHANNEL")
    def test_discontinuous_audio_clock_rejected(self):
        self.mode("gap");self.assert_failed(self.run_job(),"UNSUPPORTED_CLOCK")
    def test_nonzero_audio_stream_origin_rejected_explicitly(self):
        self.mode("offset");self.assert_failed(self.run_job(),"UNSUPPORTED_CLOCK")
    def test_missing_start_metadata_cannot_hide_audio_offset(self):
        self.mode("unknown_offset");self.assert_failed(self.run_job(),"UNSUPPORTED_CLOCK")
    def test_negative_packets_cannot_hide_shifted_decoded_origin(self):
        self.mode("hostile_shift");self.assert_failed(self.run_job(),"UNSUPPORTED_CLOCK")
    def test_valid_priming_packets_require_zero_decoded_origin(self):
        self.mode("valid_priming");self.assertEqual(self.run_job().state,"complete")
    def test_decoded_gap_rejected_despite_continuous_packets(self):
        self.mode("decoded_gap");self.assert_failed(self.run_job(),"UNSUPPORTED_CLOCK")
    def test_missing_decoded_evidence_rejected(self):
        self.mode("missing_decoded_clock");self.assert_failed(self.run_job(),"UNSUPPORTED_CLOCK")
    def test_rescaled_zero_cannot_hide_nonzero_original_origin(self):
        self.mode("subsample_origin");self.assert_failed(self.run_job(),"UNSUPPORTED_CLOCK")
    def test_full_180_second_clock_profile_uses_bounded_retention(self):
        clock=m.DecodedClock(48000,180_000_000)
        for n in range(72000):
            for stage in ("origin","clock"):
                line=("[ashowinfo@clipline_"+stage+" @ test] n:"+str(n)+" pts:"+str(n*120)+" pts_time:"+str(n*120/48000)+" rate:48000 nb_samples:120\n").encode()
                self.assertTrue(clock.observe(line))
        receipt=clock.finish(8_640_000)
        self.assertEqual(receipt["decoded_samples"],8_640_000)
        self.assertEqual(receipt["frame_counts"],{"origin":72000,"clock":72000})
        self.assertLess(len(json.dumps(receipt)),8192)
        self.assertEqual(len(receipt["first_record_lines"]),8)
    def test_clock_record_length_is_bounded(self):
        clock=m.DecodedClock(16000,4_000_000)
        with self.assertRaises(m.PreparationError):clock.observe(b"[ashowinfo@clipline_origin] n:0 "+b"x"*4096)
    def test_short_pcm_rejected(self):
        self.mode("short");self.assert_failed(self.run_job(),"PCM_CLOCK_MISMATCH")
    def test_crashed_extract_retains_partial(self):
        self.mode("crash");r=self.run_job();receipt=self.assert_failed(r,"TOOL_FAILED")
        self.assertEqual(r.state,"partial");self.assertEqual(receipt["commands"][-1]["exit_code"],7)
    def test_malformed_probe_retains_failure_receipt(self):
        self.mode("malformed");self.assert_failed(self.run_job(),"PREPARATION_FAILED")
    def test_stdout_flood_is_bounded(self):
        self.mode("stdout_flood");r=self.run_job();self.assert_failed(r,"OUTPUT_LIMIT")
        self.assertLessEqual((r.directory/"probe.stdout").stat().st_size,m.MAX_STDOUT)
    def test_stderr_flood_is_bounded(self):
        self.mode("stderr_flood");r=self.run_job();self.assert_failed(r,"OUTPUT_LIMIT")
        self.assertLessEqual((r.directory/"probe.stderr").stat().st_size,m.MAX_STDERR)
    def test_timeout_kills_owned_gate(self):
        self.mode("blocked");self.assert_failed(self.run_job(timeout=.3),"TIMEOUT")
    def test_pre_cancel_has_retained_receipt_without_tool(self):
        event=threading.Event();event.set();r=self.run_job(cancel=event)
        self.assertEqual(r.state,"cancelled");receipt=self.assert_failed(r,"CANCELLED");self.assertEqual(receipt["commands"],[])
    def test_cancel_during_extract_kills_descendant(self):
        self.mode("descendant");event=threading.Event();box={}
        thread=threading.Thread(target=lambda:box.update(result=self.run_job(cancel=event)));thread.start()
        end=time.monotonic()+20
        while not self.gate.with_name("descendant.pid").exists() and time.monotonic()<end:time.sleep(.03)
        event.set();thread.join(10);self.assertFalse(thread.is_alive())
        self.assert_failed(box["result"],"CANCELLED")
        pid=int(self.gate.with_name("descendant.pid").read_text());self.assertFalse(alive(pid))
    def test_profile_hash_mismatch(self):
        self.profile.write_text("{}")
        self.assert_failed(self.run_job(),"PROFILE_CHANGED")
    def test_profile_tool_mismatch(self):
        self.profile.write_text(json.dumps({"schema_version":1,"ffmpeg":str(self.ffmpeg),"assets":[]}));self.selection["profile_sha256"]=sha(self.profile)
        self.assert_failed(self.run_job(),"PROFILE_TOOL_MISMATCH")
    def test_invalid_inputs_are_retained_not_raised(self):
        for key,value in (("channels",[]),("channels",[True]),("channels",[1,1]),("stream_index",True),("start_us",1_000_001),("end_us",999_999),("mix","guessed"),("asset_id",None)):
            with self.subTest(key=key,value=value):
                original=self.selection[key];self.selection[key]=value
                self.assert_failed(self.run_job(),"INVALID_RANGE" if key in ("start_us","end_us") else "INVALID_INPUT")
                self.selection[key]=original
    def test_isolated_microphone_is_never_inferred(self):
        self.selection["mix"]="isolated_microphone";self.assert_failed(self.run_job(),"INVALID_INPUT")
        self.selection["mix_assertion"]="User explicitly selects their known microphone track"
        r=self.run_job();self.assertEqual(r.state,"complete")
        receipt=json.loads((r.directory/"receipt.json").read_text());self.assertEqual(receipt["source_identity"]["mix_evidence"],"user_asserted_not_verified")
    def test_nonfinite_deadline(self):
        for value in (float("nan"),float("inf"),0,True):
            with self.subTest(value=value):self.assert_failed(self.run_job(timeout=value),"INVALID_TIMEOUT")
    def test_prepared_request_tampering_detected(self):
        r=self.run_job();v=r.load_request();v["source"]["mix"]="mixed";r.request_path.write_text(json.dumps(v))
        with self.assertRaises(m.PreparationError):r.load_request()
    @unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/ffprobe required")
    def test_real_ffmpeg_selected_channel_and_nonzero_clock(self):
        self.profile_for(FFMPEG);self.selection["profile_sha256"]=sha(self.profile)
        service=m.CaptionAudioPreparer(FFMPEG,FFPROBE,python=PYTHON)
        r=service.prepare(self.selection,self.path/"actual-runs");self.assertEqual((r.state,r.error_code),("complete",None))
        req=r.load_request();validate_request(req)
        with wave.open(req["audio"]["path"],"rb") as w:pcm=w.readframes(w.getnframes())
        expected=b"".join(struct.pack("<h",x) for x in self.right[16000:48000])
        self.assertEqual(pcm,expected);self.assertEqual(req["audio"]["pcm_sha256"],hashlib.sha256(expected).hexdigest())
        self.assertEqual(self.source.read_bytes(),self.original)
    @unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/ffprobe required")
    def test_real_ffmpeg_explicit_two_channel_mean(self):
        self.profile_for(FFMPEG);self.selection["profile_sha256"]=sha(self.profile);self.selection["channels"]=[0,1]
        r=m.CaptionAudioPreparer(FFMPEG,FFPROBE,python=PYTHON).prepare(self.selection,self.path/"actual-runs")
        self.assertEqual((r.state,r.error_code),("complete",None));req=r.load_request()
        with wave.open(req["audio"]["path"],"rb") as w:self.assertEqual(w.readframes(w.getnframes()),b"\0\0"*32000)

if __name__=="__main__":unittest.main(verbosity=2)
