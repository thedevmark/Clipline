import copy,hashlib,json,os,subprocess,sys,tempfile,threading,time,unittest,uuid,wave
from pathlib import Path
from unittest.mock import patch
R=Path(__file__).resolve().parents[1]
from native.services.caption_worker.client import CaptionClient
from native.services.caption_worker.evidence import EvidenceError,MAX_LOG,bounds,canonical,hash_file,make_hypothesis,make_alignment,validate_request
from native.services.caption_worker.backend import verify_source_audio

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix="clipline-captions-");self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        source=self.root/"source.bin";source.write_bytes(b"original source fixture")
        audio=self.root/"audio.wav";pcm=b"\0\0"*160
        with wave.open(str(audio),"wb") as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(pcm)
        profile=self.root/"profile.json";profile.write_text("{}",encoding="utf-8")
        self.r={"schema_version":1,"run_id":str(uuid.uuid4()),"task":"transcribe","language":"en","profile_path":str(profile),"profile_sha256":hash_file(profile),
            "source":{"asset_id":str(uuid.uuid4()),"path":str(source),"sha256":hash_file(source),"audio_stream_index":1,"channels":[0],"mix":"unknown"},
            "audio":{"path":str(audio),"wav_sha256":hash_file(audio),"pcm_sha256":hashlib.sha256(pcm).hexdigest(),"sample_rate":16000,"sample_width":2,"sample_count":160,"channels":1,
                "sample_zero_source_us":10_000_000,"extraction":{"source_sha256":hash_file(source),"argv":["fixture"]}},
            "range":{"start_us":10_000_000,"end_us":10_010_000}}
    def client(self,mode):
        return CaptionClient(sys.executable,R/"native/services/caption_worker/worker.py",command=[sys.executable,"-u","-B","-S",str(R/"tests/fixtures/caption_fake_worker.py"),mode])
    def run_mode(self,mode,**kwargs):return self.client(mode).run(self.r,self.root/"runs",**kwargs)
    def test_empty_hypothesis_is_complete_and_artifacts_are_immutable(self):
        result=self.run_mode("success",timeout=10)
        self.assertEqual((result.state,result.error_code),("complete",None))
        h=json.loads(result.hypothesis_path.read_text(encoding="utf-8"));self.assertEqual(h["words"],[])
        receipt=json.loads((result.directory/"receipt.json").read_text());self.assertEqual(receipt["hashes"]["hypothesis"],hashlib.sha256(result.hypothesis_path.read_bytes()).hexdigest())
        if os.name=="nt":
            self.assertEqual(receipt["process_scope"]["disposition"],"stopped")
            self.assertEqual(receipt["process_scope"]["active_processes"],0)
            self.assertTrue(receipt["streams_closed"])
        with self.assertRaises(FileExistsError):self.run_mode("success")
    def test_malformed_stdout_is_rejected(self):self.assertEqual(self.run_mode("malformed",timeout=10).error_code,"MALFORMED_PROTOCOL")
    def test_truncated_stdout_is_rejected(self):self.assertEqual(self.run_mode("truncated",timeout=10).error_code,"TRUNCATED_PROTOCOL")
    def test_stdout_flood_is_bounded_and_stopped(self):
        r=self.run_mode("flood",timeout=10);self.assertEqual(r.error_code,"STDOUT_LIMIT");self.assertLessEqual((r.directory/"stdout.jsonl").stat().st_size,3*1_048_576)
    def test_stderr_flood_is_bounded_and_stopped(self):
        r=self.run_mode("stderr",timeout=10);self.assertEqual(r.error_code,"STDERR_LIMIT");self.assertEqual((r.directory/"stderr.log").stat().st_size,MAX_LOG)
    def test_timeout_stops_worker(self):self.assertEqual(self.run_mode("sleep",timeout=0.4).error_code,"TIMEOUT")
    def test_timeout_does_not_block_on_unread_stdin(self):
        self.r["test_padding"]="x"*50_000
        r=self.run_mode("unread",timeout=0.4);self.assertEqual(r.error_code,"TIMEOUT")
    def test_worker_crash_is_not_complete(self):
        r=self.run_mode("crash",timeout=10);self.assertEqual((r.state,r.error_code),("failed","WORKER_CRASH"))
    def test_attachment_failure_closes_actual_pipes_without_claiming_stopped_scope(self):
        opened=[];popen=subprocess.Popen
        def launch(*args,**kwargs):
            process=popen(*args,**kwargs);opened.append(process);return process
        with patch("native.services.caption_worker.client.subprocess.Popen",side_effect=launch), patch("native.services.caption_worker.client.ProcessTree",side_effect=OSError("injected attachment failure")):
            result=self.run_mode("success",timeout=10)
        self.assertEqual(result.state,"failed")
        self.assertEqual(len(opened),1)
        self.assertIsNotNone(opened[0].poll())
        self.assertTrue(all(pipe.closed for pipe in (opened[0].stdin,opened[0].stdout,opened[0].stderr)))
        receipt=json.loads((result.directory/"receipt.json").read_text())
        self.assertEqual(receipt["process_scope"]["disposition"],"never_started")
        self.assertEqual(receipt["process_scope"]["scope"],"unreleased_gate_parent")
        self.assertFalse(receipt["process_scope"]["requested_work_started"])
        self.assertNotIn("active_processes",receipt["process_scope"])
        self.assertTrue(receipt["streams_closed"])
    def test_crash_after_hypothesis_preserves_partial_evidence(self):
        r=self.run_mode("partial",timeout=10);self.assertEqual((r.state,r.error_code),("partial","WORKER_CRASH"));self.assertTrue(r.hypothesis_path.exists());self.assertIsNone(r.alignment_path)
    def test_missing_terminal_is_not_complete(self):self.assertEqual(self.run_mode("missing_terminal",timeout=10).error_code,"MISSING_TERMINAL")
    def test_false_complete_is_not_accepted(self):self.assertEqual(self.run_mode("false_complete",timeout=10).error_code,"INCOMPLETE_RESULT")
    def test_duplicate_terminal_is_rejected(self):self.assertEqual(self.run_mode("duplicate",timeout=10).error_code,"INVALID_SEQUENCE")
    def test_cancel_before_launch_is_cancelled(self):
        cancel=threading.Event();cancel.set();r=self.run_mode("success",cancel=cancel);self.assertEqual(r.state,"cancelled");self.assertIsNone(r.hypothesis_path)
    def test_cancel_after_hypothesis_preserves_it_without_acceptance(self):
        cancel=threading.Event();holder=[]
        t=threading.Thread(target=lambda:holder.append(self.run_mode("cancel_partial",cancel=cancel,timeout=30)));t.start()
        expected=self.root/"runs"/self.r["run_id"]/"hypothesis.json"
        try:
            deadline=time.monotonic()+15
            while not expected.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(expected.exists())
        finally:
            cancel.set();t.join(10)
        self.assertFalse(t.is_alive());self.assertEqual(holder[0].state,"cancelled");self.assertTrue(expected.exists())
    def test_cancel_kills_worker_descendant(self):
        cancel=threading.Event();holder=[];pid_path=self.root/"child.pid";self.r["test_child_pid_path"]=str(pid_path)
        t=threading.Thread(target=lambda:holder.append(self.run_mode("child",cancel=cancel,timeout=30)));t.start()
        try:
            deadline=time.monotonic()+15
            while not pid_path.exists() and time.monotonic()<deadline:time.sleep(.02)
            self.assertTrue(pid_path.exists());pid=int(pid_path.read_text())
        finally:
            cancel.set();t.join(10)
        self.assertFalse(t.is_alive());self.assertEqual(holder[0].state,"cancelled")
        if os.name=="nt":
            scope=json.loads((holder[0].directory/"receipt.json").read_text())["process_scope"]
            self.assertEqual((scope["disposition"],scope["active_processes"]),("stopped",0))
        if os.name=="nt":
            import ctypes
            k=ctypes.WinDLL("kernel32",use_last_error=True);k.OpenProcess.restype=ctypes.c_void_p;k.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32]
            h=k.OpenProcess(0x00100000,False,pid)
            if h:
                k.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32];self.assertEqual(k.WaitForSingleObject(h,2000),0);k.CloseHandle.argtypes=[ctypes.c_void_p];k.CloseHandle(h)
    def test_invalid_run_id_cannot_escape_output_directory(self):
        self.r["run_id"]="../../escape";r=self.run_mode("success");self.assertEqual(r.error_code,"INVALID_REQUEST");self.assertTrue(r.directory.is_relative_to(self.root/"runs"))
    def test_source_replacement_is_rejected(self):
        Path(self.r["source"]["path"]).write_bytes(b"foreign replacement")
        with self.assertRaises(EvidenceError) as caught:verify_source_audio(self.r)
        self.assertEqual(caught.exception.code,"SOURCE_MISMATCH")
    def test_pcm_hash_and_clock_mismatch_are_rejected(self):
        self.r["audio"]["sample_count"]+=1
        with self.assertRaises(EvidenceError) as caught:verify_source_audio(self.r)
        self.assertEqual(caught.exception.code,"AUDIO_MISMATCH")
    def test_inexact_range_samples_are_rejected(self):
        self.r["range"]["start_us"]+=1
        with self.assertRaises(EvidenceError):validate_request(self.r)
    def test_missing_boundary_and_score_remain_null(self):
        h=make_hypothesis(self.r,[{"text":"word","start":0,"end":.008}],{})
        a=make_alignment(self.r,h,{"word_segments":[{"word":"word"}]},{})
        self.assertEqual(a["words"][0]["timing"]["status"],"missing");self.assertIsNone(a["words"][0]["timing"]["start_us"]);self.assertIsNone(a["words"][0]["alignment_score"])
    def test_nonzero_source_clock_and_bad_bounds(self):
        self.assertEqual(bounds(.001,.005,self.r)["start_us"],10_001_000)
        self.assertEqual(bounds(.01,.01,self.r)["status"],"unresolved")
        self.assertIsNone(bounds(-1,.1,self.r)["start_us"])
    def test_contraction_keeps_piece_probabilities_without_invented_average(self):
        seg={"text":" Y'all","start":0,"end":.009,"words":[{"word":" Y","start":0,"end":.003,"probability":.8},{"word":"'all","start":.003,"end":.009,"probability":.4}]}
        h=make_hypothesis(self.r,[seg],{});self.assertEqual(len(h["words"]),1);self.assertEqual(len(h["words"][0]["asr_piece_ids"]),2);self.assertIsNone(h["words"][0]["asr_probability"])
        self.assertEqual([p["probability"] for p in h["asr_pieces"]],[.8,.4])
    def test_alignment_text_mismatch_does_not_reassign_word_ids(self):
        h=make_hypothesis(self.r,[{"text":"one two","start":0,"end":.009}],{})
        a=make_alignment(self.r,h,{"word_segments":[{"word":"two","start":.001,"end":.004}]},{})
        self.assertTrue(all(w["timing"]["status"]=="unresolved" for w in a["words"]));self.assertEqual([x["word_id"] for x in a["words"]],[x["id"] for x in h["words"]])
    def test_unavailable_profile_fails_before_optional_imports(self):
        c=CaptionClient(sys.executable,R/"native/services/caption_worker/worker.py");r=c.run(self.r,self.root/"runs",timeout=5)
        self.assertEqual(r.error_code,"PROFILE_UNAVAILABLE")
    def test_parent_import_does_not_load_optional_models(self):
        p=subprocess.run([sys.executable,"-S","-c","import sys;sys.path.insert(0,"+repr(str(R/"native/services"))+");import caption_worker.client;assert not {'torch','whisperx','numpy','faster_whisper'}&set(sys.modules)"],capture_output=True)
        self.assertEqual(p.returncode,0,p.stderr)
    def test_client_refuses_overlapping_heavy_jobs(self):
        c=self.client("sleep");cancel=threading.Event();holder=[]
        t=threading.Thread(target=lambda:holder.append(c.run(self.r,self.root/"runs",timeout=30,cancel=cancel)));t.start()
        try:
            deadline=time.monotonic()+15
            while not (self.root/"runs"/self.r["run_id"]/"lifecycle.jsonl").exists() and time.monotonic()<deadline:time.sleep(.01)
            with self.assertRaises(RuntimeError):c.run(self.r,self.root/"other")
        finally:
            cancel.set();t.join(10)
        self.assertFalse(t.is_alive());self.assertEqual(holder[0].state,"cancelled")
    def test_malformed_worker_input_has_stable_failure(self):
        p=subprocess.run([sys.executable,"-B","-S",str(R/"native/services/caption_worker/worker.py")],input=b"{bad json}\n",capture_output=True,timeout=5)
        self.assertEqual(p.returncode,1);event=json.loads(p.stdout);self.assertEqual(event["data"]["error_code"],"INVALID_REQUEST")
    def test_rerun_creates_new_machine_word_ids(self):
        first=make_hypothesis(self.r,[{"text":"same words","start":0,"end":.009}],{})
        changed=copy.deepcopy(self.r);changed["run_id"]=str(uuid.uuid4())
        second=make_hypothesis(changed,[{"text":"same words","start":0,"end":.009}],{})
        self.assertTrue(set(w["id"] for w in first["words"]).isdisjoint(w["id"] for w in second["words"]))
    def test_nonfinite_machine_values_become_null_not_zero(self):
        from native.services.caption_worker.worker import machine_json
        self.assertEqual(machine_json({"score":float("nan"),"end":float("inf")}),{"score":None,"end":None})
    def test_none_and_empty_requests_retain_failure_receipts(self):
        for bad in [None,{}]:
            r=self.client("success").run(bad,self.root/str(uuid.uuid4()),timeout=3)
            self.assertEqual(r.error_code,"INVALID_REQUEST");self.assertTrue((r.directory/"receipt.json").exists())
    def test_nonstring_audio_path_is_an_invalid_request(self):
        self.r["audio"]["path"]=42
        r=self.run_mode("success",timeout=3);self.assertEqual(r.error_code,"INVALID_REQUEST")
    def test_deadline_must_be_finite_and_positive(self):
        for timeout in [float("nan"),float("inf"),0,-1,None,True]:
            request=copy.deepcopy(self.r);request["run_id"]=str(uuid.uuid4())
            r=self.client("success").run(request,self.root/"deadlines",timeout=timeout)
            self.assertEqual(r.error_code,"INVALID_TIMEOUT")
    def test_short_stderr_phase_is_visible_before_worker_exit(self):
        cancel=threading.Event();holder=[]
        t=threading.Thread(target=lambda:holder.append(self.run_mode("phase",timeout=30,cancel=cancel)));t.start()
        log=self.root/"runs"/self.r["run_id"]/"stderr.log";deadline=time.monotonic()+15
        visible=False
        try:
            while time.monotonic()<deadline:
                if log.exists() and b"phase=waiting" in log.read_bytes():visible=True;break
                time.sleep(.02)
        finally:
            cancel.set();t.join(10)
        self.assertFalse(t.is_alive());self.assertTrue(visible,"short stderr marker remained buffered")
    def test_alignment_cannot_introduce_foreign_word_id(self):
        r=self.run_mode("bad_alignment",timeout=10);self.assertEqual(r.error_code,"INVALID_EVIDENCE");self.assertIsNone(r.alignment_path)
    def test_machine_artifact_tampering_is_detected_on_read(self):
        r=self.run_mode("success",timeout=10);self.assertEqual(r.load("hypothesis")["words"],[])
        value=json.loads(r.hypothesis_path.read_text());value["tampered"]=True;r.hypothesis_path.write_bytes(canonical(value))
        with self.assertRaises(EvidenceError) as caught:r.load("hypothesis")
        self.assertEqual(caught.exception.code,"EVIDENCE_CHANGED")
    def test_parent_rejects_reordered_canonical_alignment_ids(self):
        r=self.run_mode("reordered",timeout=10);self.assertEqual((r.state,r.error_code),("partial","INVALID_EVIDENCE"));self.assertIsNone(r.alignment_path)
    def test_parent_rejects_overlapping_resolved_alignment_intervals(self):
        r=self.run_mode("overlap",timeout=10);self.assertEqual((r.state,r.error_code),("partial","INVALID_EVIDENCE"));self.assertIsNone(r.alignment_path)
    def test_parent_preserves_missing_gap_between_ordered_resolved_words(self):
        r=self.run_mode("missing_gap",timeout=10);self.assertEqual(r.state,"complete")
        words=r.load("alignment")["words"];self.assertEqual(words[1]["timing"]["status"],"missing");self.assertIsNone(words[1]["timing"]["start_us"])

if __name__=="__main__":unittest.main(verbosity=2)
