import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import uuid
from unittest.mock import patch

from native.core.creator_library import CreatorLibrary
from native.core import accepted_transcript as m

def uid(): return str(uuid.uuid4())
def timing(a,b,status="aligned"): return {"start_us":a,"end_us":b,"status":status}

class AcceptedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="clipline-captions-"); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.source=self.root/"source.bin"; self.source.write_bytes(b"shared media unchanged")
        self.db=self.root/"library.sqlite"
        self.lib=CreatorLibrary(self.db); self.addCleanup(lambda:self.lib.close())
        self.asset=self.lib.register_asset(self.source)
        m.install_schema(self.lib.connection)
        self.store=m.AcceptedTranscriptStore(self.lib.connection)
        self.identity={"source":{"asset_id":self.asset.id,"sha256":self.asset.sha256,"path":str(self.source),"audio_stream_index":1,"channels":[0,1],"mix":"mixed"},
                       "audio":{"path":"prepared.wav","pcm_sha256":"b"*64,"wav_sha256":"c"*64,"sample_rate":16000,"sample_count":320000,"channels":1,"sample_width":2,"sample_zero_source_us":10_000_000,
                                "extraction":{"source_sha256":self.asset.sha256,"argv":["ffmpeg","-i",str(self.source)],"tool_version":"test-only"}},
                       "range":{"start_us":11_000_000,"end_us":29_000_000}}
        self.tid=self.store.create(self.identity)
        self.h,self.a=self.machine()

    def machine(self,texts=("one","two","three")):
        h={"run_id":uid(),"processing_state":"complete",**copy.deepcopy(self.identity),"words":[{"id":uid(),"text":t} for t in texts]}
        a={"run_id":uid(),"hypothesis_run_id":h["run_id"],"target_transcript_revision":None,
           "words":[{"word_id":w["id"],"text":w["text"],"timing":timing(11_000_000+i*1_000_000,11_500_000+i*1_000_000)} for i,w in enumerate(h["words"])]}
        return h,a

    def accept(self,alignment=True):
        self.store.propose_hypothesis(self.tid,self.h)
        if alignment:self.store.propose_alignment(self.tid,self.h["run_id"],self.a)
        return self.store.accept_initial(self.tid,self.h["run_id"],alignment_id=self.a["run_id"] if alignment else None)

    def edit(self,base,op,targets,replacements):
        return self.store.edit(self.tid,expected_revision=base,operation=op,target_word_ids=targets,replacements=replacements)

    def counts(self):
        return {t:self.lib.connection.execute("SELECT count(*) FROM "+t).fetchone()[0] for t in ("caption_revisions","caption_events","caption_heads")}

    def test_composes_existing_library_and_no_media_io(self):
        with patch.object(Path,"open",side_effect=AssertionError("must not read media")):
            other=self.store.create(self.identity)
            self.assertEqual(self.store.identity(other),self.identity)
        self.assertEqual(self.lib.connection.execute("PRAGMA user_version").fetchone()[0],3)
        self.assertEqual(self.lib.connection.execute("SELECT count(*) FROM assets").fetchone()[0],1)
        m.install_schema(self.lib.connection)
        self.assertEqual(self.source.read_bytes(),b"shared media unchanged")

    def test_proposals_do_not_accept(self):
        self.store.propose_hypothesis(self.tid,self.h)
        self.store.propose_alignment(self.tid,self.h["run_id"],self.a)
        self.assertEqual(self.store.head(self.tid),0)
        self.assertEqual(self.store.list_accepted()["items"],[])

    def test_initial_accept_uses_distinct_ids_and_exact_source_clock(self):
        r=self.accept(); self.assertEqual(r["revision"],1); self.assertIsNone(r["parent_revision"])
        self.assertTrue(set(w["id"] for w in r["words"]).isdisjoint(w["id"] for w in self.h["words"]))
        for w,hw,aw in zip(r["words"],self.h["words"],self.a["words"]):
            self.assertEqual(w["text"],hw["text"]); self.assertEqual(w["timing"],aw["timing"])
            self.assertEqual(w["machine_refs"],[{"hypothesis_id":self.h["run_id"],"word_id":hw["id"]}])
        e=self.store.event(self.tid,1)
        self.assertEqual((e["expected_revision"],e["resulting_revision"],e["operation"]),(0,1,"initial_accept"))

    def test_explicit_accept_only_once(self):
        self.accept()
        with self.assertRaises(m.StaleRevision): self.store.accept_initial(self.tid,self.h["run_id"])
        with self.assertRaises(m.TranscriptError): self.store.accept_initial(self.tid,self.h["run_id"],origin="model")
        self.assertEqual(self.counts(),{"caption_revisions":1,"caption_events":1,"caption_heads":1})

    def test_no_alignment_accept_keeps_missing_reviewable(self):
        r=self.accept(False)
        self.assertTrue(all(w["timing"]==timing(None,None,"unresolved") for w in r["words"]))
        with self.assertRaises(m.NeedsTimingReview): self.store.projection_input(self.tid,revision=1)

    def test_replace_creates_identity_and_preserves_other_words(self):
        old=self.accept(); target=old["words"][1]
        new=self.edit(1,"replace",[target["id"]],[{"text":"Twitch"}])
        word=new["words"][1]
        self.assertNotEqual(word["id"],target["id"])
        self.assertEqual(word["parent_word_ids"],[target["id"]])
        self.assertEqual(word["machine_refs"],target["machine_refs"])
        self.assertEqual(word["timing"],{**target["timing"],"status":"inherited"})
        self.assertEqual(word["timing_provenance"],{"kind":"inherited","mode":"same_interval","from_revision":1,"from_word_ids":[target["id"]]})
        self.assertEqual(new["words"][0],old["words"][0]); self.assertEqual(new["words"][2],old["words"][2])
        self.assertEqual(self.store.revision(self.tid,1),old)
        e=self.store.event(self.tid,2)
        self.assertEqual((e["before_text"],e["after_text"]),("two","Twitch"))

    def test_split_merge_delete_lineage(self):
        r=self.accept(); first=r["words"][0]
        r=self.edit(1,"split",[first["id"]],[{"text":"ice","timing":timing(11_000_000,11_200_000,"manually_timed")},{"text":"cream","timing":timing(11_200_000,11_500_000,"manually_timed")}])
        split=r["words"][:2]
        self.assertTrue(all(w["parent_word_ids"]==[first["id"]] for w in split))
        r=self.edit(2,"merge",[w["id"] for w in split],[{"text":"icecream","timing":timing(11_000_000,11_500_000,"manually_timed")}])
        merged=r["words"][0]; self.assertEqual(merged["parent_word_ids"],[w["id"] for w in split])
        self.assertEqual(merged["machine_refs"],first["machine_refs"])
        r=self.edit(3,"delete",[merged["id"]],[])
        self.assertEqual([w["text"] for w in r["words"]],["two","three"])
        self.assertEqual(self.store.event(self.tid,4)["before_words"],[merged])
        self.assertEqual(self.store.revision(self.tid,2)["words"][:2],split)

    def test_retime_keeps_word_identity(self):
        r=self.accept(False); w=r["words"][0]
        r=self.edit(1,"retime",[w["id"]],[{"text":w["text"],"timing":timing(11_000_000,11_500_000,"manually_timed")}])
        self.assertEqual(r["words"][0]["id"],w["id"])
        self.assertEqual(r["words"][0]["text_origin"],w["text_origin"])
        self.assertIsNone(r["words"][1]["timing"]["start_us"])

    def test_manual_timing_rejects_zero_outside_overlap(self):
        r=self.accept(); ids=[w["id"] for w in r["words"]]
        for t in (timing(12_000_000,12_000_000,"manually_timed"),timing(9_000_000,10_000_000,"manually_timed"),timing(11_200_000,12_500_000,"manually_timed")):
            with self.subTest(t=t),self.assertRaises(m.TranscriptError): self.edit(1,"retime",[ids[0]],[{"text":"one","timing":t}])
        self.assertEqual(self.store.head(self.tid),1)

    def test_missing_gap_does_not_hide_resolved_overlap(self):
        self.a["words"][1]["timing"]=timing(None,None,"missing")
        self.a["words"][2]["timing"]=timing(11_200_000,11_600_000)
        self.store.propose_hypothesis(self.tid,self.h)
        with self.assertRaises(m.TranscriptError): self.store.propose_alignment(self.tid,self.h["run_id"],self.a)

    def test_null_and_interpolated_timings_block_projection(self):
        for status in ("missing","unresolved","interpolated"):
            with self.subTest(status=status):
                tid=self.store.create(self.identity); h,a=self.machine()
                a["words"][1]["timing"]=timing(12_000_000,12_500_000,status) if status=="interpolated" else timing(None,None,status)
                self.store.propose_hypothesis(tid,h); self.store.propose_alignment(tid,h["run_id"],a)
                r=self.store.accept_initial(tid,h["run_id"],alignment_id=a["run_id"])
                self.assertEqual(r["words"][1]["timing"]["status"],status)
                with self.assertRaises(m.NeedsTimingReview): self.store.projection_input(tid,revision=1)

    def test_both_formats_get_same_pinned_revision_without_layout_state(self):
        self.accept()
        landscape=self.store.projection_input(self.tid,revision=1)
        portrait=self.store.projection_input(self.tid,revision=1)
        self.assertEqual(landscape,portrait)
        self.assertEqual(set(landscape),{"accepted","identity"})
        self.edit(1,"delete",[landscape["accepted"]["words"][0]["id"]],[])
        self.assertEqual(self.store.projection_input(self.tid,revision=1),landscape)

    def test_reruns_and_new_alignments_never_change_human_corrections(self):
        r=self.accept(); r=self.edit(1,"replace",[r["words"][1]["id"]],[{"text":"Mark","timing":timing(12_000_000,12_500_000,"manually_timed")}])
        before=json.dumps(r,sort_keys=True)
        h,a=self.machine(("entirely","different","text"))
        self.store.propose_hypothesis(self.tid,h); self.store.propose_alignment(self.tid,h["run_id"],a)
        corrected={"run_id":uid(),"hypothesis_run_id":None,"target_transcript_revision":2,
                   "words":[{"word_id":w["id"],"text":w["text"],"timing":timing(None,None,"unresolved")} for w in r["words"]]}
        self.store.propose_accepted_alignment(self.tid,2,corrected)
        self.assertEqual(json.dumps(self.store.revision(self.tid),sort_keys=True),before)
        self.assertEqual(self.store.head(self.tid),2)

    def test_reopen_preserves_accepted_words_proposals_and_events(self):
        r=self.accept(); r=self.edit(1,"replace",[r["words"][0]["id"]],[{"text":"Streamer"}]); e=self.store.event(self.tid,2)
        self.lib.close(); self.lib=CreatorLibrary(self.db); self.store=m.AcceptedTranscriptStore(self.lib.connection)
        self.assertEqual(self.store.revision(self.tid),r); self.assertEqual(self.store.event(self.tid,2),e)
        self.assertEqual(self.store.proposal(self.tid,self.h["run_id"]),self.h)

    def test_second_connection_stale_edit_rejected(self):
        r=self.accept(); other=CreatorLibrary(self.db); self.addCleanup(other.close)
        s2=m.AcceptedTranscriptStore(other.connection)
        base=s2.revision(self.tid)
        self.edit(1,"delete",[r["words"][0]["id"]],[])
        with self.assertRaises(m.StaleRevision): s2.edit(self.tid,expected_revision=base["revision"],operation="delete",target_word_ids=[base["words"][1]["id"]],replacements=[])
        self.assertEqual(s2.head(self.tid),2)
        self.assertEqual(self.counts(),{"caption_revisions":2,"caption_events":2,"caption_heads":1})

    def test_transaction_failure_after_revision_and_event_rolls_everything_back(self):
        r=self.accept(); counts=self.counts()
        self.lib.connection.execute("CREATE TEMP TRIGGER test_fail_head BEFORE UPDATE ON caption_heads BEGIN SELECT RAISE(ABORT,'injected after revision and event'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.edit(1,"delete",[r["words"][0]["id"]],[])
        self.assertEqual(self.store.revision(self.tid),r); self.assertEqual(self.counts(),counts)
        self.lib.connection.execute("DROP TRIGGER test_fail_head")
        self.assertEqual(self.edit(1,"delete",[r["words"][0]["id"]],[])["revision"],2)

    def test_initial_head_insert_failure_has_no_partial_acceptance(self):
        self.store.propose_hypothesis(self.tid,self.h)
        self.lib.connection.execute("CREATE TEMP TRIGGER test_fail_initial BEFORE INSERT ON caption_heads BEGIN SELECT RAISE(ABORT,'injected initial'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.store.accept_initial(self.tid,self.h["run_id"])
        self.assertEqual(self.counts(),{"caption_revisions":0,"caption_events":0,"caption_heads":0})

    def test_immutable_sql_records(self):
        self.accept()
        for table in ("caption_transcripts","caption_proposals","caption_revisions","caption_events","caption_schema"):
            for op in ("DELETE FROM "+table,"UPDATE "+table+" SET "+{"caption_transcripts":"id=id","caption_proposals":"id=id","caption_revisions":"revision=revision","caption_events":"id=id","caption_schema":"version=version"}[table]):
                with self.subTest(op=op),self.assertRaises(sqlite3.IntegrityError): self.lib.connection.execute(op)

    def test_identity_mismatch_and_unknown_asset_rejected(self):
        for mutator in (lambda i:i["source"].update(asset_id=uid()),lambda i:i["source"].update(sha256="d"*64),lambda i:i["audio"].update(sample_count=1),lambda i:i["range"].update(end_us=31_000_000)):
            i=copy.deepcopy(self.identity); mutator(i)
            with self.assertRaises(m.TranscriptError): self.store.create(i)
        self.assertEqual(self.lib.connection.execute("SELECT count(*) FROM caption_transcripts").fetchone()[0],1)

    def test_machine_identity_order_and_proposal_collisions(self):
        h=copy.deepcopy(self.h); h["source"]["channels"]=[1]
        with self.assertRaises(m.TranscriptError): self.store.propose_hypothesis(self.tid,h)
        self.store.propose_hypothesis(self.tid,self.h)
        self.store.propose_hypothesis(self.tid,self.h) # exact idempotent replay
        h=copy.deepcopy(self.h); h["words"][0]["text"]="changed"
        with self.assertRaises(m.TranscriptError): self.store.propose_hypothesis(self.tid,h)
        a=copy.deepcopy(self.a); a["words"].reverse()
        with self.assertRaises(m.TranscriptError): self.store.propose_alignment(self.tid,self.h["run_id"],a)

    def test_cross_transcript_proposals_rejected(self):
        self.store.propose_hypothesis(self.tid,self.h)
        tid=self.store.create(self.identity)
        with self.assertRaises(m.TranscriptError): self.store.propose_alignment(tid,self.h["run_id"],self.a)
        with self.assertRaises(m.TranscriptError): self.store.accept_initial(tid,self.h["run_id"])

    def test_unknown_noncontiguous_or_machine_targets_rejected(self):
        r=self.accept(); ids=[w["id"] for w in r["words"]]
        for targets in ([ids[0],ids[2]],[ids[1],ids[0]],[self.h["words"][0]["id"]]):
            with self.subTest(targets=targets),self.assertRaises(m.TranscriptError): self.edit(1,"delete",targets,[])
        self.assertEqual(self.store.head(self.tid),1)

    def test_bad_edit_shapes_text_origin_and_timing_rejected(self):
        r=self.accept(); wid=r["words"][0]["id"]
        for op,rep in (("split",[{"text":"one"}]),("merge",[{"text":"one"}]),("delete",[{"text":"one"}]),("replace",[{"text":"two words"}]),("replace",[{"text":"word","timing":timing(11_000_000,11_500_000)}]),("retime",[{"text":"changed"}])):
            with self.subTest(op=op,rep=rep),self.assertRaises(m.TranscriptError): self.edit(1,op,[wid],rep)
        with self.assertRaises(m.TranscriptError): self.store.edit(self.tid,expected_revision=1,operation="delete",target_word_ids=[wid],replacements=[],origin="model")
        self.assertEqual(self.store.head(self.tid),1)

    def test_bounded_entry_and_pagination_validation(self):
        for after,limit in ((-1,1),(0,0),(0,201),(True,1),(0,False)):
            with self.subTest(after=after,limit=limit),self.assertRaises(m.TranscriptError): self.store.list_accepted(after=after,limit=limit)
        h=copy.deepcopy(self.h); h["words"]=[{"id":uid(),"text":"word"} for _ in range(m.MAX_WORDS+1)]
        with self.assertRaises(m.TranscriptError): self.store.propose_hypothesis(self.tid,h)
        h=copy.deepcopy(self.h); h["raw"]="x"*(m.MAX_JSON+1)
        with self.assertRaises(m.TranscriptError): self.store.propose_hypothesis(self.tid,h)

    def test_10k_metadata_listing_has_no_word_blob_or_media_load(self):
        # Real SQLite records and real pages; fixture seeding avoids 10k repeated UI actions.
        c=self.lib.connection; encoded=json.dumps(self.identity); word_blob=json.dumps([{"large":"x"*2000}])
        c.execute("BEGIN IMMEDIATE")
        for _ in range(10_000):
            tid=uid()
            c.execute("INSERT INTO caption_transcripts VALUES(?,?,?,?,?)",(tid,self.asset.id,self.asset.sha256,encoded,"fixture"))
            c.execute("INSERT INTO caption_revisions VALUES(?,1,NULL,?,'fixture')",(tid,word_blob))
            c.execute("INSERT INTO caption_heads(transcript_id,revision) VALUES(?,1)",(tid,))
        c.execute("COMMIT")
        statements=[]; c.set_trace_callback(statements.append)
        ids=[]; cursor=0
        with patch.object(Path,"open",side_effect=AssertionError("no media")):
            while True:
                page=self.store.list_accepted(after=cursor,limit=137)
                self.assertLessEqual(len(page["items"]),137); ids.extend(x["transcript_id"] for x in page["items"])
                if page["next_cursor"] is None: break
                cursor=page["next_cursor"]
        c.set_trace_callback(None)
        self.assertEqual(len(ids),10000); self.assertEqual(len(set(ids)),10000)
        self.assertTrue(all("words_json" not in q and "identity_json" not in q for q in statements))

    def test_proposal_pages_exclude_evidence_blobs(self):
        for _ in range(5):
            h,a=self.machine(); self.store.propose_hypothesis(self.tid,h)
        page=self.store.list_proposals(self.tid,limit=2)
        self.assertEqual(len(page["items"]),2); self.assertIsNotNone(page["next_cursor"])
        self.assertNotIn("body_json",page["items"][0])
        second=self.store.list_proposals(self.tid,after=page["next_cursor"],limit=2)
        self.assertTrue(set(x["id"] for x in page["items"]).isdisjoint(x["id"] for x in second["items"]))

    def test_caller_transaction_is_not_committed_or_rolled_back(self):
        self.lib.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(m.TranscriptError): self.store.create(self.identity)
        self.assertTrue(self.lib.connection.in_transaction)
        self.lib.connection.execute("ROLLBACK")

    def test_malformed_identity_retains_no_partial_record(self):
        for bad in (None,{}, {"source":None,"audio":{},"range":{}}, {**self.identity,"audio":{"sample_count":True}}):
            with self.subTest(bad=bad),self.assertRaises(m.TranscriptError): self.store.create(bad)

    def test_verified_relocation_keeps_stable_identity_for_new_proposals(self):
        self.accept()
        moved=self.root/"moved.bin"; moved.write_bytes(self.source.read_bytes())
        self.lib.relocate_asset(self.asset.id,moved,expected_path=self.asset.path)
        h,a=self.machine(); h["source"]["path"]=str(moved)
        h["audio"]["path"]="another-prepared-location.wav"
        h["audio"]["extraction"]["argv"]=["ffmpeg","-i",str(moved)]
        h["audio"]["wav_sha256"]="d"*64 # Header bytes may differ; verified PCM identity remains stable.
        self.store.propose_hypothesis(self.tid,h)
        self.store.propose_alignment(self.tid,h["run_id"],a)
        self.assertEqual(self.store.head(self.tid),1)
        self.assertEqual(self.lib.asset(self.asset.id).path,str(moved.resolve()))
        self.assertEqual(self.store.identity(self.tid)["source"]["path"],str(self.source))

    def test_machine_alignment_cannot_claim_human_timing(self):
        self.store.propose_hypothesis(self.tid,self.h)
        self.a["words"][0]["timing"]["status"]="manually_timed"
        with self.assertRaises(m.TranscriptError): self.store.propose_alignment(self.tid,self.h["run_id"],self.a)

    def test_spelling_correction_inherits_usable_source_interval_and_projection(self):
        initial=self.accept(); original=initial["words"][1]
        corrected=self.edit(1,"replace",[original["id"]],[{"text":"Twitch"}])
        word=corrected["words"][1]
        self.assertEqual((word["timing"]["start_us"],word["timing"]["end_us"]),(original["timing"]["start_us"],original["timing"]["end_us"]))
        self.assertEqual(word["timing"]["status"],"inherited")
        self.assertEqual(word["timing_provenance"]["from_word_ids"],[original["id"]])
        self.assertEqual(word["timing_provenance"]["from_revision"],1)
        self.assertEqual(self.store.revision(self.tid,1)["words"][1],original)
        self.assertEqual(original["timing_provenance"]["proposal_id"],self.a["run_id"])
        self.assertIsNone(self.store.event(self.tid,2)["manual_timing"])
        self.assertEqual(self.store.event(self.tid,2)["timing_decisions"][0]["provenance"],word["timing_provenance"])
        landscape=self.store.projection_input(self.tid,revision=2)
        portrait=self.store.projection_input(self.tid,revision=2)
        self.assertEqual(landscape,portrait)

    def test_spelling_correction_inherits_manual_interval_without_new_manual_claim(self):
        r=self.accept(); original=r["words"][0]
        r=self.edit(1,"retime",[original["id"]],[{"text":original["text"],"timing":timing(11_100_000,11_400_000,"manually_timed")}])
        self.assertEqual(r["words"][0]["timing_provenance"],{"kind":"human_manual","at_revision":2})
        r=self.edit(2,"replace",[original["id"]],[{"text":"Jev"}])
        corrected=r["words"][0]
        self.assertEqual(corrected["timing"],timing(11_100_000,11_400_000,"inherited"))
        self.assertEqual(corrected["timing_provenance"],{"kind":"inherited","mode":"same_interval","from_revision":2,"from_word_ids":[original["id"]]})
        self.assertIsNone(self.store.event(self.tid,3)["manual_timing"])
        self.store.projection_input(self.tid,revision=3)

    def test_replacement_preserves_review_required_statuses(self):
        for status in ("missing","unresolved","interpolated"):
            with self.subTest(status=status):
                tid=self.store.create(self.identity); h,a=self.machine()
                original_timing=timing(12_000_000,12_500_000,status) if status=="interpolated" else timing(None,None,status)
                a["words"][1]["timing"]=original_timing
                self.store.propose_hypothesis(tid,h); self.store.propose_alignment(tid,h["run_id"],a)
                r=self.store.accept_initial(tid,h["run_id"],alignment_id=a["run_id"])
                old_id=r["words"][1]["id"]
                r=self.store.edit(tid,expected_revision=1,operation="replace",target_word_ids=[old_id],replacements=[{"text":"corrected"}])
                self.assertEqual(r["words"][1]["timing"],original_timing)
                self.assertEqual(r["words"][1]["timing_provenance"]["kind"],"retained_review_required")
                with self.assertRaises(m.NeedsTimingReview): self.store.projection_input(tid,revision=2)

    def test_split_does_not_invent_internal_boundaries(self):
        initial=self.accept(); original=initial["words"][0]
        split=self.edit(1,"split",[original["id"]],[{"text":"ice"},{"text":"cream"}])
        for word in split["words"][:2]:
            self.assertEqual(word["timing"],timing(None,None,"unresolved"))
            self.assertEqual(word["timing_provenance"]["reason"],"split_internal_boundaries_unknown")
        with self.assertRaises(m.NeedsTimingReview): self.store.projection_input(self.tid,revision=2)

    def test_merge_uses_known_outer_span_without_internal_boundary_claim(self):
        r=self.accept(); originals=r["words"][:2]
        ids=[w["id"] for w in originals]
        r=self.edit(1,"merge",ids,[{"text":"onetwo"}])
        merged=r["words"][0]
        self.assertEqual(merged["timing"],timing(11_000_000,12_500_000,"inherited"))
        self.assertEqual(merged["timing_provenance"],{"kind":"inherited","mode":"outer_span","from_revision":1,"from_word_ids":ids})
        self.assertEqual(merged["parent_word_ids"],ids)
        self.assertEqual(len(merged["machine_refs"]),2)
        self.assertIsNone(self.store.event(self.tid,2)["manual_timing"])
        self.store.projection_input(self.tid,revision=2)

    def test_merge_with_missing_or_interpolated_word_stays_unresolved(self):
        for status in ("missing","unresolved","interpolated"):
            with self.subTest(status=status):
                tid=self.store.create(self.identity); h,a=self.machine()
                a["words"][1]["timing"]=timing(12_000_000,12_500_000,status) if status=="interpolated" else timing(None,None,status)
                self.store.propose_hypothesis(tid,h);self.store.propose_alignment(tid,h["run_id"],a)
                r=self.store.accept_initial(tid,h["run_id"],alignment_id=a["run_id"])
                r=self.store.edit(tid,expected_revision=1,operation="merge",target_word_ids=[w["id"] for w in r["words"][:2]],replacements=[{"text":"onetwo"}])
                self.assertEqual(r["words"][0]["timing"],timing(None,None,"unresolved"))
                self.assertEqual(r["words"][0]["timing_provenance"]["reason"],"merge_contains_review_required_timing")
                with self.assertRaises(m.NeedsTimingReview):self.store.projection_input(tid,revision=2)

    def test_explicit_manual_replacement_overrides_retained_bounds(self):
        r=self.accept(); original=r["words"][0]
        r=self.edit(1,"replace",[original["id"]],[{"text":"Jev","timing":timing(11_100_000,11_400_000,"manually_timed")}])
        corrected=r["words"][0]
        self.assertEqual(corrected["timing"],timing(11_100_000,11_400_000,"manually_timed"))
        self.assertEqual(corrected["timing_provenance"],{"kind":"human_manual","at_revision":2})
        self.assertEqual(self.store.event(self.tid,2)["manual_timing"],[{"word_id":corrected["id"],"timing":corrected["timing"]}])

    def test_inherited_timing_chain_survives_proposals_and_reopen(self):
        r=self.accept(); first=r["words"][0]
        r=self.edit(1,"replace",[first["id"]],[{"text":"Twich"}]); second=r["words"][0]
        r=self.edit(2,"replace",[second["id"]],[{"text":"Twitch"}]); third=r["words"][0]
        self.assertEqual(third["timing_provenance"]["from_word_ids"],[second["id"]])
        self.assertEqual(third["timing_provenance"]["from_revision"],2)
        self.assertEqual(self.store.revision(self.tid,2)["words"][0]["timing_provenance"]["from_word_ids"],[first["id"]])
        h,a=self.machine(("different","model","proposal"))
        self.store.propose_hypothesis(self.tid,h);self.store.propose_alignment(self.tid,h["run_id"],a)
        later_alignment={"run_id":uid(),"hypothesis_run_id":None,"target_transcript_revision":3,
                         "words":[{"word_id":w["id"],"text":w["text"],"timing":timing(None,None,"missing")} for w in r["words"]]}
        self.store.propose_accepted_alignment(self.tid,3,later_alignment)
        self.assertEqual(self.store.head(self.tid),3)
        self.assertEqual(self.store.revision(self.tid,3),r)
        snapshot=self.store.projection_input(self.tid,revision=3)
        self.lib.close(); self.lib=CreatorLibrary(self.db);self.store=m.AcceptedTranscriptStore(self.lib.connection)
        self.assertEqual(self.store.revision(self.tid,3),r)
        self.assertEqual(self.store.projection_input(self.tid,revision=3),snapshot)
        self.assertEqual(snapshot["accepted"]["words"][0]["text"],"Twitch")

    def test_machine_proposal_cannot_claim_inherited_status(self):
        self.store.propose_hypothesis(self.tid,self.h)
        self.a["words"][0]["timing"]["status"]="inherited"
        with self.assertRaises(m.TranscriptError): self.store.propose_alignment(self.tid,self.h["run_id"],self.a)

    def test_wrong_accepted_alignment_target_or_order_is_rejected(self):
        r=self.accept()
        a={"run_id":uid(),"hypothesis_run_id":None,"target_transcript_revision":1,"words":[{"word_id":w["id"],"text":w["text"],"timing":w["timing"]} for w in r["words"]]}
        a["words"].reverse()
        with self.assertRaises(m.TranscriptError): self.store.propose_accepted_alignment(self.tid,1,a)
        a["words"].reverse(); a["target_transcript_revision"]=2
        with self.assertRaises(m.TranscriptError): self.store.propose_accepted_alignment(self.tid,1,a)

    def test_empty_hypothesis_accepts_empty_transcript(self):
        self.h["words"]=[]; self.a["words"]=[]
        r=self.accept(); self.assertEqual(r["words"],[])
        self.assertEqual(self.store.projection_input(self.tid,revision=1)["accepted"],r)

if __name__=="__main__": unittest.main(verbosity=2)
