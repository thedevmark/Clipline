import hashlib,json,os,shutil,socket,subprocess,sys,tempfile,threading,time,unittest,uuid
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).parent
from native.services import filmcraft_session as n
FIXTURE=ROOT/"fixtures/filmcraft_session_peer.py"
from native.core.project_store import ProjectStore
from native.services.native_checkpoint import checkout,publish_checkpoint
EVIDENCE=Path(os.environ["CLIPLINE_SESSION_TEST_ROOT"]).resolve() if os.environ.get("CLIPLINE_SESSION_TEST_ROOT") else None
if EVIDENCE:EVIDENCE.mkdir(parents=True,exist_ok=False)

@unittest.skipUnless(os.name=="nt","Windows Job Object qualification")
class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix="clipline-session-");self.root=Path(self.temp.name)
        self.store=ProjectStore(self.root/"moment")
        self.seed=self.store.publish(b"opaque original native snapshot",expected_generation=None,revision=1,engine="filmcraft",engine_version="fixture-v1",suffix=".fcproj")
        self.working=checkout(self.store);self.working.path.write_bytes(b"opaque dirty working bytes")
        self.editor=n.QualifiedEditor(Path(sys.executable),n.digest(Path(sys.executable)),"protocol-fixture-only")
        self.sessions=[]
    def tearDown(self):
        for session in self.sessions:
            session.stop()
        if EVIDENCE:shutil.copytree(self.root,EVIDENCE/self._testMethodName)
        self.temp.cleanup()
    def launch(self,mode="normal",**kw):
        def cmd(editor,port,data,working):
            return [str(editor.path),str(FIXTURE),mode,"--control",str(port),"--data-dir",str(data),"--no-recover",str(working)]
        with patch.object(n.QualifiedEditor,"command",cmd):
            session=n.NativeSession.start(self.store,self.working,self.editor,python=Path(sys.executable),runs=self.root/"runs",readiness_seconds=kw.pop("readiness_seconds",3),**kw)
        self.sessions.append(session);return session
    def assert_preserved(self):
        self.assertEqual(self.working.path.read_bytes(),b"opaque dirty working bytes")
        self.assertEqual(self.store.load().document,b"opaque original native snapshot")
        self.assertEqual(self.store.load().generation,self.seed.generation)
    def test_ready_bridge_retained_dirty_recovery_and_verified_scope_stop(self):
        s=self.launch();self.assertTrue(s.bridge.execute("project.inspect")["fixtureOnly"])
        cached=s.bridge
        s.bridge.execute("evaluation.markDirty")
        receipt=s.stop();self.assertEqual(receipt["scope"]["disposition"],"stopped")
        self.assertEqual(receipt["scope"]["active_processes"],0);self.assertTrue(receipt["streams_closed"])
        self.assertEqual((s.directory/"data/retained-recovery.fixture").read_bytes(),b"unsaved fixture data")
        self.assertEqual(json.loads((s.directory/"ready.json").read_text())["control_authentication"],"none")
        self.assert_preserved()
        with self.assertRaises(n.SessionError):_=s.bridge
        with self.assertRaises(n.SessionError):cached.execute("project.inspect")
        self.assertEqual(s.stop(),receipt)
    def test_wrong_project_never_exposes_bridge_and_scope_is_stopped(self):
        with self.assertRaisesRegex(n.SessionError,"different or invalid") as caught:self.launch("wrong")
        directory=caught.exception.directory
        self.assertFalse((directory/"ready.json").exists())
        self.assertEqual(json.loads((directory/"stop.json").read_text())["scope"]["disposition"],"stopped")
        self.assert_preserved()
    def test_readiness_deadline_cleans_real_child(self):
        start=time.monotonic()
        with self.assertRaisesRegex(n.SessionError,"deadline") as caught:self.launch("hang",readiness_seconds=.3)
        self.assertLess(time.monotonic()-start,8)
        self.assertEqual(json.loads((caught.exception.directory/"stop.json").read_text())["scope"]["active_processes"],0)
        self.assert_preserved()
    def test_cancel_during_launch_cleans_scope(self):
        cancel=threading.Event();timer=threading.Timer(.2,cancel.set);timer.start()
        try:
            with self.assertRaisesRegex(n.SessionError,"cancelled") as caught:self.launch("hang",cancel=cancel)
        finally:timer.cancel();timer.join()
        self.assertEqual(json.loads((caught.exception.directory/"stop.json").read_text())["scope"]["disposition"],"stopped")
        self.assert_preserved()
    def test_cancel_before_launch_never_starts_process(self):
        cancel=threading.Event();cancel.set()
        with self.assertRaisesRegex(n.SessionError,"before process") as caught:self.launch(cancel=cancel)
        self.assertFalse((caught.exception.directory/"child.json").exists())
        self.assertEqual(json.loads((caught.exception.directory/"stop.json").read_text())["scope"]["disposition"],"never_started")
    def test_early_process_exit_retains_failure(self):
        with self.assertRaisesRegex(n.SessionError,"exited") as caught:self.launch("exit")
        self.assertTrue((caught.exception.directory/"launch-error.json").exists());self.assert_preserved()
    def test_descendant_listener_is_not_adopted_and_all_owned_processes_stop(self):
        with self.assertRaisesRegex(n.SessionError,"another process") as caught:self.launch("descendant")
        receipt=json.loads((caught.exception.directory/"stop.json").read_text())
        self.assertEqual(receipt["scope"]["active_processes"],0);self.assert_preserved()
    def test_other_working_owner_cannot_launch_and_first_remains_usable(self):
        s=self.launch()
        with self.assertRaisesRegex(n.SessionError,"Another owner"):self.launch()
        self.assertTrue(s.bridge.execute("project.inspect")["fixtureOnly"])
    def test_wrong_executable_hash_never_creates_run(self):
        bad=n.QualifiedEditor(self.editor.path,"0"*64,"fixture")
        with self.assertRaisesRegex(ValueError,"changed"):
            n.NativeSession.start(self.store,self.working,bad,python=Path(sys.executable),runs=self.root/"runs")
        self.assertFalse((self.root/"runs").exists())
    def test_stale_publication_base_does_not_launch(self):
        self.store.publish(b"new opaque generation",expected_generation=self.seed.generation,revision=2,engine="filmcraft",engine_version="fixture",suffix=".fcproj")
        with self.assertRaisesRegex(ValueError,"base changed"):self.launch()
        self.assertFalse((self.root/"runs").exists())
    def test_read_only_lost_reply_is_retained_and_bounded(self):
        start=time.monotonic()
        with self.assertRaisesRegex(n.SessionError,"deadline") as caught:self.launch("slow",readiness_seconds=.65)
        self.assertLess(time.monotonic()-start,8)
        self.assertTrue(list(caught.exception.directory.glob("readiness-unknown-*.json")))
        self.assert_preserved()
    def test_preexisting_listener_receives_no_requests_and_survives_rejection(self):
        probe=socket.socket();probe.bind(("127.0.0.1",0));foreign_port=probe.getsockname()[1];probe.close()
        external=self.root/"external";external.mkdir()
        peer=subprocess.Popen([sys.executable,str(FIXTURE),"normal","--control",str(foreign_port),"--data-dir",str(external),"--no-recover",str(self.working.path)],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
        scope=n.ProcessTree(peer)
        try:
            deadline=time.monotonic()+3
            while n.listener_pids(foreign_port)!={peer.pid}:
                self.assertIsNone(peer.poll());self.assertLess(time.monotonic(),deadline);time.sleep(.01)
            real_socket=socket.socket
            class Reservation:
                def __init__(self,*a,**kw):self.socket=real_socket(*a,**kw)
                def setsockopt(self,*a):return self.socket.setsockopt(*a)
                def bind(self,*a):return self.socket.bind(*a)
                def getsockname(self):return ("127.0.0.1",foreign_port)
                def close(self):self.socket.close()
            # Simulate the reservation/release race selecting a now occupied port.
            with patch.object(n.socket,"socket",Reservation):
                with self.assertRaisesRegex(n.SessionError,"another process") as caught:self.launch("hang")
            self.assertIsNone(peer.poll())
            self.assertFalse((external/"requests.fixture").exists())
            self.assertEqual(json.loads((caught.exception.directory/"stop.json").read_text())["scope"]["active_processes"],0)
        finally:
            proof=scope.stop();scope.close();peer.wait(timeout=5)
            self.assertEqual(proof["active_processes"],0)

    def test_unknown_scope_quarantines_working_copy_and_refuses_second_launch(self):
        s=self.launch()
        original=s.tree.stop
        def unknown():
            proof=original();proof["disposition"]="unknown";proof["active_processes"]=None
            return proof
        with patch.object(s.tree,"stop",unknown):receipt=s.stop()
        self.assertEqual(receipt["scope"]["disposition"],"unknown")
        self.assertTrue((self.working.path.parent/"editor-owner-uncertain.json").exists())
        before=set((self.root/"runs").glob("*/child.json"))
        with self.assertRaisesRegex(n.SessionError,"prior uncertain"):self.launch()
        self.assertEqual(set((self.root/"runs").glob("*/child.json")),before)
        self.assert_preserved()
    def test_cleanup_receipt_failure_does_not_mask_original_launch_failure(self):
        original=n.write_once
        def fail_stop(path,value):
            if path.name=="stop.json":raise OSError("fixture stop receipt disk failure")
            return original(path,value)
        with patch.object(n,"write_once",fail_stop):
            with self.assertRaisesRegex(n.SessionError,"different or invalid") as caught:self.launch("wrong")
        self.assertIn("stop evidence",caught.exception.cleanup_error)
        self.assertTrue((caught.exception.directory/"launch-error.json").exists())
        self.assertTrue((caught.exception.directory/"launch-cleanup-error.json").exists())
        self.assertEqual(caught.exception.owner.stop_receipt["scope"]["disposition"],"stopped")
        self.assert_preserved()

    def test_explicit_checkpoint_uses_product_owner_and_retains_prior_generation(self):
        s=self.launch()
        saved,updated=publish_checkpoint(self.store,s.bridge,self.working,native_revision=1,links_revisions=[],engine_version="protocol-fixture-only")
        self.assertEqual(saved.revision,2)
        self.assertEqual(saved.document,b"opaque dirty working bytes retained opaque snapshot")
        self.assertEqual(self.seed.path.read_bytes(),b"opaque original native snapshot")
        self.assertEqual(updated.base_generation,saved.generation)
        self.assertEqual(self.working.path.read_bytes(),b"opaque dirty working bytes")
        self.assertEqual(len(list((self.store.directory/"captures").glob("*/capture.json"))),1)

    def test_job_attachment_failure_keeps_gate_closed(self):
        with patch.object(n,"ProcessTree",side_effect=OSError("attachment fixture failure")):
            with self.assertRaisesRegex(n.SessionError,"attachment fixture failure") as caught:self.launch()
        directory=caught.exception.directory
        self.assertFalse((directory/"child.json").exists())
        self.assertEqual(json.loads((directory/"stop.json").read_text())["scope"]["disposition"],"unknown")
        self.assert_preserved()
        self.assertTrue((self.working.path.parent/"editor-owner-uncertain.json").exists())
        with self.assertRaisesRegex(n.SessionError,"prior uncertain"):self.launch()


    def test_malformed_stop_marker_retains_quarantine_without_losing_scope_proof(self):
        for marker in ("incomplete marker","[]","null","42"):
            with self.subTest(marker=marker):
                self.working=checkout(self.store)
                self.working.path.write_bytes(b"opaque dirty working bytes")
                s=self.launch()
                (self.working.path.parent/"editor-owner-uncertain.json").write_text(marker)
                proof=s.stop()
                self.assertEqual(proof["scope"]["disposition"],"stopped")
                self.assertIn("quarantine_release_error",proof["scope"])
                self.assertIsNone(s._lock)
                with self.assertRaisesRegex(n.SessionError,"prior uncertain"):self.launch()
                self.assert_preserved()


if __name__=="__main__":
    print("Retained protocol evidence: "+str(EVIDENCE),flush=True)
    unittest.main(verbosity=2)
