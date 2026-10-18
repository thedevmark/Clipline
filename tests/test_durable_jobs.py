import copy,hashlib,json,sqlite3,tempfile,threading,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from native.core.creator_library import CreatorLibrary
from native.core.project_store import ProjectStore
from native.core import durable_jobs as m
def uid():return str(uuid.uuid4())
def ref():return {"id":uid(),"sha256":"a"*64}

class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.db=self.root/"library.sqlite"
        self.lib=CreatorLibrary(self.db);self.addCleanup(lambda:self.lib.close())
        self.source=self.root/"source.bin";self.source.write_bytes(b"shared original fixture")
        self.asset=self.lib.register_asset(self.source)
        m.install_schema(self.lib.connection);self.calls=[];self.disposition="stopped";self.output_disposition="verified"
        self.store=m.DurableJobs(self.lib.connection,settlement_verifier=self.verify)
        self.intent={"request_ref":ref(),"assets":[{"asset_id":self.asset.id,"sha256":self.asset.sha256}],"profile_sha256":"b"*64,"range":{"start_us":0,"end_us":18_000_000}}
        self.owner=uid()
    def verify(self,job,evidence):
        # This is a synthetic attestation boundary, not live process verification.
        self.calls.append((copy.deepcopy(job),evidence))
        return {"job_id":job["id"],"claim_token":job["claim_token"],"intent_sha256":job["intent_sha256"],"output_set_id":job["output_set_id"],"process_disposition":self.disposition,"outputs_disposition":self.output_disposition,"result_ref":ref() if self.output_disposition!="absent" else None,"evidence_ref":evidence}
    def submit(self,kind="caption",intent=None,key=None):
        payload=copy.deepcopy(self.intent if intent is None else intent)
        payload.setdefault("request_ref",ref())
        return self.store.submit(kind,payload,idempotency_key=key or uid())
    def claim(self):return self.store.claim_next(owner_id=self.owner)
    def finish(self,job,outcome="completed"):
        return self.store.finish(job["id"],expected_revision=job["revision"],claim_token=job["claim_token"],outcome=outcome,evidence_ref=ref())
    def test_same_library_schema_and_original_preserved(self):
        self.assertEqual(self.lib.connection.execute("PRAGMA user_version").fetchone()[0],2)
        m.install_schema(self.lib.connection)
        self.submit();self.assertEqual(self.source.read_bytes(),b"shared original fixture")
        self.assertEqual(self.lib.connection.execute("SELECT count(*) FROM assets").fetchone()[0],1)
    def test_idempotency_is_exact_and_conflicts_do_not_mutate(self):
        key=uid();a=self.submit(key=key);b=self.submit(key=key)
        self.assertEqual(a,b)
        changed=copy.deepcopy(self.intent);changed["range"]["end_us"]-=1
        with self.assertRaises(m.IntentConflict):self.submit(intent=changed,key=key)
        with self.assertRaises(m.IntentConflict):self.submit(kind="preparation",key=key)
        self.assertEqual(self.store.job(a["id"]),a)
    def test_input_pins_detached_from_caller_mutation(self):
        a=self.submit();self.intent["range"]["end_us"]=123
        self.assertEqual(self.store.job(a["id"])["input"]["range"]["end_us"],18_000_000)
    def test_all_heavy_kinds_share_one_slot_and_io_is_limited(self):
        self.submit();a=self.claim();self.submit("preparation");self.submit("io",{})
        io=self.claim();self.assertEqual(io["kind"],"io");self.assertIsNone(self.claim())
        holds=self.store.resources();self.assertEqual(len(holds),3)
        self.assertEqual(sum(h["resource"]=="heavy" for h in holds),1)
        self.finish(a);next_job=self.claim();self.assertEqual(next_job["kind"],"preparation")
    def test_two_connections_cannot_claim_same_job_or_heavy_slot(self):
        one=self.submit();two=self.submit("preparation");barrier=threading.Barrier(2);results=[];errors=[]
        def worker():
            try:
                with CreatorLibrary(self.db) as lib:
                    store=m.DurableJobs(lib.connection);barrier.wait(10)
                    results.append(store.claim_next(owner_id=uid()))
            except BaseException as e:errors.append(repr(e))
        threads=[threading.Thread(target=worker) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(15);self.assertFalse(t.is_alive())
        self.assertEqual(errors,[]);self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.store.job(one["id"])["state"],"running")
        self.assertEqual(self.store.job(two["id"])["state"],"queued")
    def test_three_connection_io_admission_allows_only_two(self):
        for _ in range(3):self.submit("io",{})
        barrier=threading.Barrier(3);results=[];errors=[]
        def worker():
            try:
                with CreatorLibrary(self.db) as lib:
                    store=m.DurableJobs(lib.connection);barrier.wait(10)
                    results.append(store.claim_next(owner_id=uid()))
            except BaseException as exc:errors.append(repr(exc))
        threads=[threading.Thread(target=worker) for _ in range(3)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(15);self.assertFalse(thread.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(sum(result is not None for result in results),2)
        self.assertEqual(len(self.store.resources()),2)
        self.assertTrue(all(row["resource"]=="io" for row in self.store.resources()))
    def test_queued_cancellation_needs_no_process_claim(self):
        a=self.submit();a=self.store.request_cancel(a["id"],expected_revision=1)
        self.assertEqual(a["state"],"cancelled");self.assertIsNone(a["claim_token"])
        self.assertIsNone(self.claim());self.assertEqual(self.store.resources(),[]);self.assertEqual(self.calls,[])
    def test_claim_cancel_race_cannot_cancel_claimed_job_as_unstarted(self):
        queued=self.submit();other=CreatorLibrary(self.db);self.addCleanup(other.close)
        s2=m.DurableJobs(other.connection);running=self.claim()
        with self.assertRaises(m.StaleTransition):s2.request_cancel(queued["id"],expected_revision=queued["revision"])
        cancelled=s2.request_cancel(running["id"],expected_revision=running["revision"])
        self.assertEqual(cancelled["state"],"cancel-requested")
        self.assertEqual(len(s2.resources()),2)
    def test_running_cancel_retains_resources_until_verified_stop(self):
        self.submit();a=self.claim();old=copy.deepcopy(a)
        a=self.store.request_cancel(a["id"],expected_revision=a["revision"])
        self.assertEqual(a["state"],"cancel-requested");self.assertEqual(len(self.store.resources()),2)
        with self.assertRaises(m.StaleTransition):self.finish(old)
        with self.assertRaises(m.VerificationRequired):self.finish(a)
        self.disposition="live";self.output_disposition="quarantined"
        with self.assertRaises(m.VerificationRequired):self.finish(a,"cancelled")
        self.assertEqual(len(self.store.resources()),2)
        self.disposition="stopped";a=self.finish(a,"cancelled")
        self.assertEqual(a["state"],"cancelled");self.assertEqual(self.store.resources(),[])
    def test_completion_wins_only_if_committed_before_cancel(self):
        self.submit();a=self.claim();done=self.finish(a)
        with self.assertRaises(m.StaleTransition):self.store.request_cancel(a["id"],expected_revision=a["revision"])
        self.assertEqual(self.store.job(a["id"])["state"],"completed")
        self.assertEqual(done["input"],a["input"])
    def test_cancellation_during_verification_fences_completion(self):
        self.submit();a=self.claim();original=self.store.verifier
        def racing(job,evidence):
            self.store.request_cancel(job["id"],expected_revision=job["revision"])
            return original(job,evidence)
        self.store.verifier=racing
        with self.assertRaises(m.StaleTransition):self.finish(a)
        self.assertEqual(self.store.job(a["id"])["state"],"cancel-requested")
        self.assertEqual(len(self.store.resources()),2)
        self.assertEqual(self.lib.connection.execute("SELECT count(*) FROM durable_job_settlements").fetchone()[0],0)
    def test_wrong_or_missing_worker_token_cannot_mutate(self):
        self.submit();a=self.claim()
        for token in (None,uid()):
            with self.subTest(token=token),self.assertRaises(m.JobError):self.store.record_launch_intent(a["id"],expected_revision=a["revision"],claim_token=token,launch_ref=ref())
            with self.subTest(token=token),self.assertRaises(m.JobError):self.store.finish(a["id"],expected_revision=a["revision"],claim_token=token,outcome="completed",evidence_ref=ref())
        self.assertEqual(self.store.job(a["id"]),a)
    def test_launch_intent_is_not_process_started_or_replaceable(self):
        self.submit();a=self.claim();launch=ref()
        a=self.store.record_launch_intent(a["id"],expected_revision=a["revision"],claim_token=a["claim_token"],launch_ref=launch)
        self.assertEqual(a["launch_ref"],launch)
        with self.assertRaises(m.StaleTransition):self.store.record_launch_intent(a["id"],expected_revision=a["revision"],claim_token=a["claim_token"],launch_ref=ref())
    def test_restart_does_not_reclaim_or_retry(self):
        self.submit();running=self.claim();self.submit("preparation")
        self.lib.close();self.lib=CreatorLibrary(self.db);self.store=m.DurableJobs(self.lib.connection,settlement_verifier=self.verify)
        self.assertEqual(self.store.job(running["id"]),running);self.assertIsNone(self.claim())
        interrupted=self.store.mark_interrupted(running["id"],expected_revision=running["revision"],reason="restart")
        self.assertEqual(interrupted["state"],"interrupted");self.assertEqual(len(self.store.resources()),2)
        with self.assertRaises(m.StaleTransition):self.finish(running)
        with self.assertRaises(m.StaleTransition):self.finish(interrupted)
        self.assertIsNone(self.claim())
    def test_reconciliation_needs_stop_and_output_evidence(self):
        self.submit();a=self.claim();a=self.store.mark_interrupted(a["id"],expected_revision=a["revision"],reason="cleanup_uncertain")
        self.disposition="unknown"
        with self.assertRaises(m.VerificationRequired):self.store.reconcile(a["id"],expected_revision=a["revision"],claim_token=a["claim_token"],outcome="completed",evidence_ref=ref())
        self.disposition="stopped";self.output_disposition="quarantined"
        a=self.store.reconcile(a["id"],expected_revision=a["revision"],claim_token=a["claim_token"],outcome="failed",evidence_ref=ref())
        self.assertEqual(a["state"],"failed");self.assertEqual(self.store.resources(),[])
    def test_cancelled_interrupted_job_cannot_promote_outputs(self):
        self.submit();a=self.claim();a=self.store.mark_interrupted(a["id"],expected_revision=a["revision"],reason="owner_lost")
        a=self.store.request_cancel(a["id"],expected_revision=a["revision"])
        self.assertEqual(a["state"],"interrupted");self.assertTrue(a["cancel_requested"])
        with self.assertRaises(m.VerificationRequired):self.store.reconcile(a["id"],expected_revision=a["revision"],claim_token=a["claim_token"],outcome="completed",evidence_ref=ref())
        self.output_disposition="absent"
        a=self.store.reconcile(a["id"],expected_revision=a["revision"],claim_token=a["claim_token"],outcome="cancelled",evidence_ref=ref())
        self.assertEqual(a["state"],"cancelled")
    def test_no_runtime_verifier_cannot_release_slot(self):
        self.submit();a=self.claim();self.store.verifier=None
        with self.assertRaises(m.VerificationRequired):self.finish(a)
        self.assertEqual(len(self.store.resources()),2)
    def test_evidence_identity_and_output_disposition_must_match(self):
        self.submit();a=self.claim();original=self.store.verifier
        for key in ("job_id","claim_token","intent_sha256","output_set_id"):
            def wrong(job,evidence,key=key):
                result=original(job,evidence);result[key]=uid();return result
            self.store.verifier=wrong
            with self.subTest(key=key),self.assertRaises(m.VerificationRequired):self.finish(a)
        self.store.verifier=original;self.output_disposition="absent"
        with self.assertRaises(m.VerificationRequired):self.finish(a)
        self.assertEqual(len(self.store.resources()),2)
    def test_atomic_claim_rollback_includes_reservations(self):
        job=self.submit()
        self.lib.connection.execute("CREATE TEMP TRIGGER fail_claim BEFORE UPDATE ON durable_job_heads BEGIN SELECT RAISE(ABORT,'injected claim failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.claim()
        self.assertEqual(self.store.job(job["id"])["state"],"queued");self.assertEqual(self.store.resources(),[])
    def test_atomic_settlement_failure_does_not_release_resources(self):
        self.submit();a=self.claim()
        self.lib.connection.execute("CREATE TEMP TRIGGER fail_settle BEFORE UPDATE ON durable_job_heads BEGIN SELECT RAISE(ABORT,'injected settlement failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.finish(a)
        self.assertEqual(self.store.job(a["id"]),a);self.assertEqual(len(self.store.resources()),2)
        self.assertEqual(self.lib.connection.execute("SELECT count(*) FROM durable_job_settlements").fetchone()[0],0)
    def test_immutable_sql_evidence_and_retention_roots(self):
        a=self.submit();self.claim()
        for table,column in (("durable_job_intents","kind"),("durable_job_events","state"),("durable_job_assets","sha256"),("durable_job_slots","resource")):
            with self.subTest(table=table),self.assertRaises(sqlite3.IntegrityError):self.lib.connection.execute("UPDATE "+table+" SET "+column+"="+column)
        with self.assertRaises(sqlite3.IntegrityError):self.lib.connection.execute("DELETE FROM assets WHERE id=?",(self.asset.id,))
    def test_pinned_native_generation_does_not_follow_project_head(self):
        project=ProjectStore(self.root/"project");first=project.publish(b"native source edit",expected_generation=None,revision=1,engine="filmcraft",engine_version="0.9.0",suffix=".json")
        intent=copy.deepcopy(self.intent);intent["snapshot"]={"project_id":uid(),"generation":first.generation,"revision":1,"sha256":first.sha256,"engine":first.engine,"engine_version":first.engine_version};intent["recipe_sha256"]="c"*64
        a=self.submit("render",intent)
        project.publish(b"later edit",expected_generation=first.generation,revision=2,engine="filmcraft",engine_version="0.9.0",suffix=".json")
        self.assertEqual(self.store.job(a["id"])["input"]["snapshot"]["generation"],first.generation)
        self.assertEqual(first.path.read_bytes(),b"native source edit")
    def test_accepted_corrections_and_kit_pins_remain_separate(self):
        kit=self.lib.create_kit({"name":"creator"});tid=uid()
        # Seed one minimal immutable accepted revision; this store never authors words.
        c=self.lib.connection;c.execute("INSERT INTO caption_transcripts VALUES(?,?,?,?,?)",(tid,self.asset.id,self.asset.sha256,"{}","fixture"));c.execute("INSERT INTO caption_revisions VALUES(?,1,NULL,?,'fixture')",(tid,'[{"text":"human correction"}]'))
        intent=copy.deepcopy(self.intent);intent.update(kit={"kit_id":kit,"version":1},transcript={"transcript_id":tid,"revision":1})
        a=self.submit(intent=intent);before=c.execute("SELECT words_json FROM caption_revisions WHERE transcript_id=?",(tid,)).fetchone()[0]
        self.lib.revise_kit(kit,{"name":"changed"},expected_version=1);self.claim()
        self.assertEqual(self.store.job(a["id"])["input"]["kit"]["version"],1)
        self.assertEqual(c.execute("SELECT words_json FROM caption_revisions WHERE transcript_id=?",(tid,)).fetchone()[0],before)
    def test_metadata_listing_10k_records_opens_no_media_or_intent_blobs(self):
        c=self.lib.connection;c.execute("BEGIN IMMEDIATE")
        for _ in range(10000):
            jid=uid();c.execute("INSERT INTO durable_job_intents(id,idempotency,kind,intent_json,intent_sha256,output_set_id,created_utc) VALUES(?,?,'io',?,?,?,'fixture')",(jid,uid(),'{}','a'*64,uid()));c.execute("INSERT INTO durable_job_heads(job_id,state,revision,cancel_requested) VALUES(?,'queued',1,0)",(jid,))
        c.execute("COMMIT");queries=[];c.set_trace_callback(queries.append);ids=[];cursor=0
        with patch.object(Path,"open",side_effect=AssertionError("metadata must not open media")):
            while True:
                page=self.store.list_jobs(after=cursor,limit=137);ids.extend(x["id"] for x in page["items"])
                self.assertLessEqual(len(page["items"]),137)
                if page["next_cursor"] is None:break
                cursor=page["next_cursor"]
        c.set_trace_callback(None);self.assertEqual(len(set(ids)),10000)
        self.assertTrue(all("intent_json" not in q for q in queries))
    def test_rejects_urls_credentials_unbounded_and_foreign_asset_payloads(self):
        for bad in ({"url":"https://example.invalid?token=temporary"},{**self.intent,"token":"secret"},{**self.intent,"assets":[{"asset_id":uid(),"sha256":"a"*64}]},{**self.intent,"snapshot":{"url":"bad"}}):
            with self.subTest(bad=bad),self.assertRaises(m.JobError):self.submit(intent=bad)
        self.assertEqual(self.store.list_jobs()["items"],[])
    def test_external_caller_transaction_not_committed_or_rolled_back(self):
        self.lib.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(m.JobError):self.submit()
        self.assertTrue(self.lib.connection.in_transaction);self.lib.connection.execute("ROLLBACK")
    def test_slot_schema_rejects_unplanned_capacity(self):
        for resource,slot in (("heavy",1),("io",2),("other",0)):
            with self.subTest(resource=resource,slot=slot),self.assertRaises(sqlite3.IntegrityError):self.lib.connection.execute("INSERT INTO durable_job_slots VALUES(?,?)",(resource,slot))
    def test_verified_never_started_failure_releases_claim(self):
        self.submit();a=self.claim();self.disposition="never_started";self.output_disposition="absent"
        failed=self.finish(a,"failed")
        self.assertEqual(failed["state"],"failed");self.assertEqual(self.store.resources(),[])
    def test_explicit_new_intent_waits_for_cancel_cleanup_and_uses_new_namespace(self):
        key=uid();original=self.submit(key=key);a=self.claim()
        a=self.store.request_cancel(a["id"],expected_revision=a["revision"])
        another=self.submit();self.assertIsNone(self.claim())
        self.assertNotEqual(another["output_set_id"],original["output_set_id"])
        self.disposition="stopped";self.output_disposition="quarantined";done=self.finish(a,"cancelled")
        self.assertEqual(self.submit(key=key),done)
        next_job=self.claim();self.assertEqual(next_job["id"],another["id"])
        self.assertNotEqual(next_job["claim_token"],a["claim_token"])
        with self.assertRaises(m.StaleTransition):self.store.finish(next_job["id"],expected_revision=next_job["revision"],claim_token=a["claim_token"],outcome="completed",evidence_ref=ref())
    def test_every_job_requires_complete_request_reference(self):
        for kind in ("io","caption","preparation"):
            bad={} if kind=="io" else {k:v for k,v in self.intent.items() if k!="request_ref"}
            with self.subTest(kind=kind),self.assertRaises(m.JobError):self.store.submit(kind,bad,idempotency_key=uid())
        for request in (None,{"url":"https://example.invalid?token=temporary"},{"id":uid(),"sha256":"invalid"}):
            bad={**self.intent,"request_ref":request}
            with self.subTest(request=request),self.assertRaises(m.JobError):self.store.submit("caption",bad,idempotency_key=uid())
    def test_same_key_different_execution_request_conflicts(self):
        key=uid();initial=self.submit(key=key)
        for request in ({**self.intent["request_ref"],"id":uid()},{**self.intent["request_ref"],"sha256":"d"*64}):
            changed={**self.intent,"request_ref":request}
            with self.subTest(request=request),self.assertRaises(m.IntentConflict):self.submit(intent=changed,key=key)
        self.assertEqual(self.store.job(initial["id"]),initial)
        io_key=uid();io=self.store.submit("io",{"request_ref":ref()},idempotency_key=io_key)
        with self.assertRaises(m.IntentConflict):self.store.submit("io",{"request_ref":ref()},idempotency_key=io_key)
        self.assertEqual(self.store.job(io["id"]),io)
    def test_request_reference_survives_claim_cancel_and_reconcile(self):
        job=self.submit();pinned=copy.deepcopy(job["input"]["request_ref"])
        job=self.claim();self.assertEqual(job["input"]["request_ref"],pinned)
        job=self.store.request_cancel(job["id"],expected_revision=job["revision"])
        self.assertEqual(job["input"]["request_ref"],pinned)
        job=self.store.mark_interrupted(job["id"],expected_revision=job["revision"],reason="restart")
        self.output_disposition="quarantined"
        job=self.store.reconcile(job["id"],expected_revision=job["revision"],claim_token=job["claim_token"],outcome="cancelled",evidence_ref=ref())
        self.assertEqual(job["input"]["request_ref"],pinned)
        self.assertEqual(self.calls[-1][0]["input"]["request_ref"],pinned)
    def test_listing_bounds(self):
        for after,limit in ((-1,1),(0,0),(0,201),(True,1)):
            with self.subTest(after=after,limit=limit),self.assertRaises(m.JobError):self.store.list_jobs(after=after,limit=limit)

if __name__=="__main__":unittest.main(verbosity=2)
