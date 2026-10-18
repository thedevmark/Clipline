import copy
import json
from pathlib import Path
import uuid
from unittest.mock import patch

import pytest

from native.core.creator_library import CreatorLibrary, FingerprintMismatch
from native.services.filmcraft_bridge import NativeCommandError, OutcomeUnknown
from native.services.native_checkpoint import WorkingCopy
from native.services.native_captions import (
    AppliedOutcomeUncertain, CaptionApplicationError, apply_accepted_captions, read_native_pin,
)


@pytest.fixture
def case(tmp_path):
    with CreatorLibrary(tmp_path/"library.sqlite") as library:
        source=tmp_path/"source.bin";source.write_bytes(b"registered original bytes")
        asset=library.register_asset(source)
        identity={"source":{"asset_id":asset.id,"sha256":asset.sha256,"path":asset.path,"audio_stream_index":1,"channels":[0,1],"mix":"unknown"},
            "audio":{"path":"prepared.wav","pcm_sha256":"a"*64,"wav_sha256":"b"*64,"sample_rate":16000,"sample_count":32000,"channels":1,"sample_width":2,"sample_zero_source_us":0,
                     "extraction":{"source_sha256":asset.sha256,"argv":["fixture"],"tool_version":"authored fixture"}},
            "range":{"start_us":0,"end_us":2000000}}
        tid=library.captions.create(identity)
        hypothesis={"run_id":str(uuid.uuid4()),"processing_state":"complete",**identity,"words":[{"id":str(uuid.uuid4()),"text":"original"}]}
        alignment={"run_id":str(uuid.uuid4()),"hypothesis_run_id":hypothesis["run_id"],"target_transcript_revision":None,
            "words":[{"word_id":hypothesis["words"][0]["id"],"text":"original","timing":{"start_us":200000,"end_us":900000,"status":"aligned"}}]}
        library.captions.propose_hypothesis(tid,hypothesis)
        library.captions.propose_alignment(tid,hypothesis["run_id"],alignment)
        library.captions.accept_initial(tid,hypothesis["run_id"],alignment_id=alignment["run_id"])
        working_path=tmp_path/"working.fcproj";working_path.write_bytes(b"opaque working source")
        working=WorkingCopy(working_path,str(uuid.uuid4()),3)
        pin={"transcript_id":tid,"revision":1,"payload_sha256":"c"*64}
        tracks=[{"sequence":s,"track":t,"rules":{"fixture":"native validates rules"},"defaultSettings":""} for s,t in ((5,8),(13,16),(21,24))]
        yield library,asset,tid,working,pin,tracks,tmp_path


class Native:
    """Native command contract fixture; actual projection runs in Rust qualification."""
    def __init__(self,case,*,change=None):
        library,asset,tid,working,pin,tracks,root=case
        self.pin=pin;self.calls=[];self.change=change
        self.inventory={"valid":True,"revision":12,"owners":[{"owner":5,"linksRevision":2,"units":[{"roles":[{"role":"gameplay","sourceItem":2,"resolvedMediaItem":2,"resolvedMediaPath":asset.path,"storedMediaIdentity":{"size":asset.bytes,"fingerprint":99}}]}]}]}
    def execute(self,command,params):
        self.calls.append((command,copy.deepcopy(params)))
        if command=="clipline.validate":return copy.deepcopy(self.inventory)
        if command=="metadata.get":return {"item":5,"metadata":{"clipline.acceptedCaptions.v1":json.dumps({"pin":self.pin})}}
        assert command=="clipline.acceptCaptions"
        reply={"owner":5,"accepted":dict(self.pin),"revision":13,"captions":3}
        if self.change:self.change(reply,params)
        return reply


def apply(case,bridge,**changes):
    library,asset,tid,working,pin,tracks,root=case
    kwargs={"owner":5,"transcript_id":tid,"revision":1,"tracks":tracks,"expected_pin":None,
            "receipts_directory":root/"receipts","engine_version":"0.9.0+caption-test"}
    kwargs.update(changes)
    return apply_accepted_captions(library,bridge,working,**kwargs)


def test_forwards_exact_pinned_words_and_all_owner_cas_in_one_native_edit(case):
    library,asset,tid,working,pin,tracks,root=case
    bridge=Native(case);result=apply(case,bridge)
    assert [command for command,_ in bridge.calls]==["clipline.validate","clipline.acceptCaptions"]
    params=bridge.calls[1][1]
    assert params["projectionInput"]==library.captions.projection_input(tid,revision=1)
    assert params["expectedProjectPath"]==str(working.path)
    assert params["expectedRevision"]==12 and params["expectedLinksRevisions"]==[{"owner":5,"revision":2}]
    assert params["source"]=={"sourceItem":2,"resolvedMediaItem":2,"storedMediaIdentity":{"size":asset.bytes,"fingerprint":99}}
    assert result.accepted==pin and result.native_revision==13
    receipt=json.loads((result.receipt_directory/"applied.json").read_text())
    assert receipt["checkpoint_published"] is False and receipt["source_sha256"]==asset.sha256
    assert library.captions.head(tid)==1
    assert working.path.read_bytes()==b"opaque working source"


def test_reads_native_payload_hash_without_reimplementing_it(case):
    bridge=Native(case)
    assert read_native_pin(bridge,5)==case[4]


def test_foreign_source_bytes_fail_before_any_native_call(case):
    library,asset,tid,working,pin,tracks,root=case
    Path(asset.path).write_bytes(b"different original bytes")
    bridge=Native(case)
    with pytest.raises(FingerprintMismatch):apply(case,bridge)
    assert bridge.calls==[]


def test_wrong_native_media_path_refuses_before_acceptance(case):
    bridge=Native(case);foreign=case[-1]/"foreign.bin";foreign.write_bytes(b"foreign")
    bridge.inventory["owners"][0]["units"][0]["roles"][0]["resolvedMediaPath"]=str(foreign)
    with pytest.raises(CaptionApplicationError):apply(case,bridge)
    assert [c for c,_ in bridge.calls]==["clipline.validate"]


@pytest.mark.parametrize("field,value",[("owner",99),("revision",True),("captions",-1),("accepted",None)])
def test_bad_reply_is_uncertain_with_retained_native_response(case,field,value):
    bridge=Native(case,change=lambda reply,params:reply.update({field:value}))
    with pytest.raises(AppliedOutcomeUncertain) as caught:apply(case,bridge)
    assert (caught.value.directory/"intent.json").is_file()
    assert (caught.value.directory/"response.json").is_file()
    assert not (caught.value.directory/"applied.json").exists()
    assert sum(c=="clipline.acceptCaptions" for c,_ in bridge.calls)==1


def test_lost_reply_retains_intent_and_never_retries(case):
    class Lost(Native):
        def execute(self,command,params):
            if command=="clipline.acceptCaptions":
                self.calls.append((command,params));raise OutcomeUnknown("request-uuid","engine.execute","lost reply")
            return super().execute(command,params)
    bridge=Lost(case)
    with pytest.raises(AppliedOutcomeUncertain) as caught:apply(case,bridge)
    assert (caught.value.directory/"intent.json").is_file()
    assert len(bridge.calls)==2


def test_source_change_after_native_edit_requires_reconciliation_without_undo(case):
    bridge=Native(case,change=lambda reply,params:Path(case[1].path).write_bytes(b"changed after native mutation"))
    with pytest.raises(AppliedOutcomeUncertain) as caught:apply(case,bridge)
    assert (caught.value.directory/"response.json").exists()
    assert [c for c,_ in bridge.calls]==["clipline.validate","clipline.acceptCaptions"]


def test_explicit_historical_revision_does_not_follow_newer_library_head(case):
    library,asset,tid,working,pin,tracks,root=case
    word=library.captions.revision(tid,1)["words"][0]
    library.captions.edit(tid,expected_revision=1,operation="replace",target_word_ids=[word["id"]],replacements=[{"text":"corrected"}])
    bridge=Native(case);apply(case,bridge)
    assert bridge.calls[1][1]["projectionInput"]["accepted"]["revision"]==1
    assert bridge.calls[1][1]["projectionInput"]["accepted"]["words"][0]["text"]=="original"
    assert library.captions.head(tid)==2


def test_verification_is_read_only_and_keeps_existing_registration(case):
    library,asset,*_=case
    before=library.connection.total_changes
    assert library.verify_asset(asset.id)==asset
    assert library.connection.total_changes==before


def test_final_receipt_failure_after_valid_edit_requires_reconciliation(case):
    from native.services import native_captions
    write=native_captions.write_once
    def fail_final(path,value):
        if path.name=="applied.json":raise OSError("injected final receipt failure")
        return write(path,value)
    bridge=Native(case)
    with patch.object(native_captions,"write_once",side_effect=fail_final):
        with pytest.raises(AppliedOutcomeUncertain) as caught:apply(case,bridge)
    assert (caught.value.directory/"response.json").is_file()
    assert not (caught.value.directory/"applied.json").exists()
    assert [c for c,_ in bridge.calls]==["clipline.validate","clipline.acceptCaptions"]


def test_native_error_retains_bounded_request_receipt_without_retry(case):
    class Rejected(Native):
        def execute(self,command,params):
            if command=="clipline.acceptCaptions":
                self.calls.append((command,params))
                raise NativeCommandError("native-request-id","engine.execute","rejected"+"x"*3000)
            return super().execute(command,params)
    bridge=Rejected(case)
    with pytest.raises(NativeCommandError):apply(case,bridge)
    directories=list((case[-1]/"receipts").iterdir())
    assert len(directories)==1
    receipt=json.loads((directories[0]/"native-error.json").read_text())
    assert receipt["request_id"]=="native-request-id" and len(receipt["reason"])==2000
    assert receipt["reason_truncated"] and not receipt["zero_side_effects_asserted"]
    assert [c for c,_ in bridge.calls]==["clipline.validate","clipline.acceptCaptions"]
