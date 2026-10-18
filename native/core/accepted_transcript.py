"""Accepted source-clock words over a caller-owned CreatorLibrary connection.

Machine proposals never advance accepted heads. No media, editing timeline,
format layout, ASR runtime, or second database is owned here.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

MAX_WORDS = 5000
MAX_JSON = 1_048_576
MAX_TIME = 2**63-1

class TranscriptError(ValueError):
    pass

class StaleRevision(TranscriptError):
    pass

class NeedsTimingReview(TranscriptError):
    pass

def _json(value, maximum=MAX_JSON):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise TranscriptError("Finite JSON evidence is required") from exc
    if len(encoded.encode("utf8")) > maximum:
        raise TranscriptError("Evidence exceeds the bounded entry size")
    return encoded

def _object(value):
    # A detached JSON copy prevents caller mutation after validation.
    if not isinstance(value, dict):
        raise TranscriptError("An evidence object is required")
    return json.loads(_json(value))

def _id(value):
    if not isinstance(value, str):
        raise TranscriptError("UUID text is required")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise TranscriptError("Canonical UUID text is required") from exc
    return value

def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise TranscriptError("A lowercase SHA256 is required")
    return value

def _integer(value, low=0, high=MAX_TIME):
    if type(value) is not int or not low <= value <= high:
        raise TranscriptError("An integer inside the supported range is required")
    return value

def _token(value):
    if not isinstance(value, str) or not value or len(value) > 512 or any(c.isspace() or ord(c)<32 for c in value):
        raise TranscriptError("A word requires 1..512 non-whitespace characters")
    return value

def _identity_key(identity):
    """Location/command strings are provenance, not stable audio identity."""
    s,a=identity["source"],identity["audio"]
    return {"source":{k:s[k] for k in ("asset_id","sha256","audio_stream_index","channels","mix")},
            "audio":{k:a[k] for k in ("pcm_sha256","sample_rate","sample_count","channels","sample_width","sample_zero_source_us")},
            "range":identity["range"]}

def _now():
    return datetime.now(timezone.utc).isoformat()

def _page(after, limit):
    _integer(after)
    _integer(limit, 1, 200)

@contextmanager
def _transaction(connection):
    if connection.in_transaction:
        raise TranscriptError("Finish the caller transaction before this atomic operation")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise

def install_schema(connection: sqlite3.Connection):
    """Apply additive DDL atomically; never change CreatorLibrary user_version."""
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise TranscriptError("The library connection must enable foreign keys")
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'").fetchone():
        raise TranscriptError("An existing CreatorLibrary assets table is required")
    with _transaction(connection):
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='caption_schema'").fetchone()
        if exists:
            row = connection.execute("SELECT version FROM caption_schema WHERE singleton=1").fetchone()
            if row is None or row[0] != 1:
                raise TranscriptError("Unsupported caption schema")
            return
        statement = ""
        for line in Path(__file__).with_name("caption_schema.sql").read_text(encoding="utf8").splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                connection.execute(statement)
                statement = ""
        if statement.strip():
            raise TranscriptError("Incomplete migration DDL")

class AcceptedTranscriptStore:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise TranscriptError("The library connection must enable foreign keys")
        row = connection.execute("SELECT version FROM caption_schema WHERE singleton=1").fetchone()
        if row is None or row[0] != 1:
            raise TranscriptError("Install the supported caption migration first")

    def create(self, identity: dict) -> str:
        """Pin the extraction identity, without opening media or accepting words."""
        identity = _object(identity)
        if set(identity) != {"source", "audio", "range"}:
            raise TranscriptError("Identity requires source, audio, and range only")
        s, a, r = identity["source"], identity["audio"], identity["range"]
        try:
            _id(s["asset_id"]); _hash(s["sha256"])
            _integer(s["audio_stream_index"], 0, 64)
            if s["mix"] not in ("mixed", "unknown", "isolated_microphone"):
                raise TranscriptError("Unknown stream ownership")
            channels=s["channels"]
            if not isinstance(channels,list) or not 1<=len(channels)<=32 or len(set(channels))!=len(channels):
                raise TranscriptError("Selected source channels are required")
            for channel in channels: _integer(channel,0,31)
            _hash(a["pcm_sha256"]); _hash(a["wav_sha256"])
            rate=_integer(a["sample_rate"],1,384000)
            count=_integer(a["sample_count"],1)
            _integer(a["channels"],1,32); _integer(a["sample_width"],1,8)
            zero=_integer(a["sample_zero_source_us"])
            start=_integer(r["start_us"]); end=_integer(r["end_us"])
            if not zero<=start<end or (end-zero)*rate>count*1_000_000:
                raise TranscriptError("Source range exceeds the prepared sample clock")
            extraction=a["extraction"]
            if extraction["source_sha256"]!=s["sha256"] or not isinstance(extraction["argv"],list) or not extraction["argv"] or not all(isinstance(x,str) for x in extraction["argv"]) or not isinstance(extraction["tool_version"],str) or not extraction["tool_version"]:
                raise TranscriptError("Bound extraction command and tool version are required")
            # Paths and extraction details are provenance only; this store performs no I/O.
            _json(identity,65536)
        except (KeyError,TypeError) as exc:
            raise TranscriptError("Incomplete source/audio identity") from exc
        transcript_id=str(uuid.uuid4())
        with _transaction(self.connection):
            row=self.connection.execute("SELECT sha256 FROM assets WHERE id=?",(s["asset_id"],)).fetchone()
            if row is None or row[0]!=s["sha256"]:
                raise TranscriptError("Source identity does not match the existing library asset")
            self.connection.execute("INSERT INTO caption_transcripts VALUES (?,?,?,?,?)",(transcript_id,s["asset_id"],s["sha256"],_json(identity),_now()))
        return transcript_id

    def identity(self, transcript_id):
        row=self.connection.execute("SELECT identity_json FROM caption_transcripts WHERE id=?",(_id(transcript_id),)).fetchone()
        if row is None: raise TranscriptError("Unknown transcript")
        return json.loads(row[0])

    def _timing(self, value, identity, previous=None):
        if not isinstance(value,dict) or set(value)!={"start_us","end_us","status"}:
            raise TranscriptError("Timing requires source bounds and status only")
        start,end,status=value["start_us"],value["end_us"],value["status"]
        if status in ("missing","unresolved"):
            if start is not None or end is not None:
                raise TranscriptError("Unresolved timing must retain null bounds")
        elif status in ("aligned","manually_timed","interpolated","inherited"):
            _integer(start); _integer(end)
            r=identity["range"]
            if not r["start_us"]<=start<end<=r["end_us"] or previous is not None and start<previous:
                raise TranscriptError("Source bounds must be ordered, non-overlapping, and inside the source range")
        else: raise TranscriptError("Unknown timing status")
        return end if end is not None else previous

    def _words(self, words, identity):
        if not isinstance(words,list) or len(words)>MAX_WORDS:
            raise TranscriptError("A bounded word array is required")
        ids=set(); previous=None
        for w in words:
            _id(w["id"]); _token(w["text"])
            if w["id"] in ids: raise TranscriptError("Duplicate word ID")
            ids.add(w["id"])
            previous=self._timing(w["timing"],identity,previous)
        _json(words)

    def propose_hypothesis(self, transcript_id, hypothesis):
        h=_object(hypothesis); identity=self.identity(transcript_id)
        try:
            proposal_id=_id(h["run_id"])
            if _identity_key(h)!=_identity_key(identity):
                raise TranscriptError("Hypothesis source/audio identity differs")
            words=h["words"]
            if not isinstance(words,list) or len(words)>MAX_WORDS: raise TranscriptError("Too many hypothesis words")
            seen=set()
            for word in words:
                _id(word["id"]); _token(word["text"])
                if word["id"] in seen: raise TranscriptError("Duplicate machine word ID")
                seen.add(word["id"])
            if h["processing_state"] not in ("complete","partial"):
                raise TranscriptError("Only completed or retained partial hypotheses are proposals")
        except (KeyError,TypeError) as exc: raise TranscriptError("Malformed hypothesis") from exc
        return self._proposal(transcript_id,proposal_id,"hypothesis",None,h)

    def propose_alignment(self, transcript_id, hypothesis_id, alignment):
        a=_object(alignment); h=self.proposal(transcript_id,hypothesis_id,"hypothesis")
        try:
            proposal_id=_id(a["run_id"])
            if a["hypothesis_run_id"]!=hypothesis_id or a.get("target_transcript_revision") is not None:
                raise TranscriptError("This proposal must target its immutable hypothesis")
            if len(a["words"])!=len(h["words"]): raise TranscriptError("Alignment correspondence differs")
            identity=self.identity(transcript_id); previous=None
            for aw,hw in zip(a["words"],h["words"]):
                if aw["word_id"]!=hw["id"] or aw["text"]!=hw["text"]:
                    raise TranscriptError("Alignment must preserve canonical ordered machine words")
                timing={k:aw["timing"][k] for k in ("start_us","end_us","status")}
                if timing["status"] in ("manually_timed","inherited"): raise TranscriptError("Machine proposals cannot claim human or inherited timing")
                previous=self._timing(timing,identity,previous)
        except (KeyError,TypeError) as exc: raise TranscriptError("Malformed alignment") from exc
        return self._proposal(transcript_id,proposal_id,"alignment",hypothesis_id,a)

    def propose_accepted_alignment(self,transcript_id,revision,alignment):
        """Retain a new timing proposal for accepted IDs; do not apply it."""
        accepted=self.revision(transcript_id,revision)
        a=_object(alignment)
        try:
            proposal_id=_id(a["run_id"])
            if type(a["target_transcript_revision"]) is not int or a["target_transcript_revision"]!=revision or a.get("hypothesis_run_id") is not None:
                raise TranscriptError("Alignment must target the explicit accepted revision")
            if not isinstance(a["words"],list) or len(a["words"])!=len(accepted["words"]):
                raise TranscriptError("Alignment correspondence differs")
            previous=None; identity=self.identity(transcript_id)
            for aw,word in zip(a["words"],accepted["words"]):
                if aw["word_id"]!=word["id"] or aw["text"]!=word["text"]:
                    raise TranscriptError("Alignment must preserve ordered accepted IDs and text")
                timing={k:aw["timing"][k] for k in ("start_us","end_us","status")}
                if timing["status"] in ("manually_timed","inherited"): raise TranscriptError("Machine proposals cannot claim human or inherited timing")
                previous=self._timing(timing,identity,previous)
        except (KeyError,TypeError) as exc: raise TranscriptError("Malformed accepted alignment") from exc
        return self._proposal(transcript_id,proposal_id,"accepted_alignment",None,a)

    def _proposal(self,transcript_id,proposal_id,kind,target,body):
        encoded=_json(body); digest=hashlib.sha256(encoded.encode("utf8")).hexdigest()
        with _transaction(self.connection):
            old=self.connection.execute("SELECT transcript_id,kind,target_id,body_sha256 FROM caption_proposals WHERE id=?",(proposal_id,)).fetchone()
            if old:
                if tuple(old)!=(transcript_id,kind,target,digest): raise TranscriptError("Immutable proposal ID collision")
            else:
                self.connection.execute("INSERT INTO caption_proposals(id,transcript_id,kind,target_id,body_sha256,body_json) VALUES(?,?,?,?,?,?)",(proposal_id,transcript_id,kind,target,digest,encoded))
        return proposal_id

    def proposal(self,transcript_id,proposal_id,kind=None):
        row=self.connection.execute("SELECT kind,body_json FROM caption_proposals WHERE transcript_id=? AND id=?",(_id(transcript_id),_id(proposal_id))).fetchone()
        if row is None or kind is not None and row[0]!=kind: raise TranscriptError("Unknown proposal")
        return json.loads(row[1])

    def head(self,transcript_id):
        row=self.connection.execute("SELECT revision FROM caption_heads WHERE transcript_id=?",(_id(transcript_id),)).fetchone()
        return row[0] if row else 0

    def revision(self,transcript_id,revision=None):
        if revision is None: revision=self.head(transcript_id)
        _integer(revision,1)
        row=self.connection.execute("SELECT parent_revision,words_json,created_utc FROM caption_revisions WHERE transcript_id=? AND revision=?",(_id(transcript_id),revision)).fetchone()
        if row is None: raise TranscriptError("Unknown accepted revision")
        return {"transcript_id":transcript_id,"revision":revision,"parent_revision":row[0],"words":json.loads(row[1]),"created_utc":row[2]}

    def accept_initial(self,transcript_id,hypothesis_id,*,alignment_id=None,expected_revision=0,origin="human"):
        if expected_revision!=0 or type(expected_revision) is not int or origin!="human":
            raise TranscriptError("Explicit initial human acceptance requires base revision zero")
        with _transaction(self.connection):
            if self.head(transcript_id)!=0: raise StaleRevision("Accepted transcript already exists")
            h=self.proposal(transcript_id,hypothesis_id,"hypothesis")
            a=self.proposal(transcript_id,alignment_id,"alignment") if alignment_id else None
            if a and a["hypothesis_run_id"]!=hypothesis_id: raise TranscriptError("Alignment targets another hypothesis")
            words=[]
            for i,hw in enumerate(h["words"]):
                timing={k:a["words"][i]["timing"][k] for k in ("start_us","end_us","status")} if a else {"start_us":None,"end_us":None,"status":"unresolved"}
                provenance={"kind":"alignment_proposal","proposal_id":a["run_id"],"hypothesis_word_id":hw["id"]} if a else {"kind":"unresolved","reason":"alignment_not_selected"}
                words.append({"id":str(uuid.uuid4()),"text":hw["text"],"timing":timing,"timing_provenance":provenance,"text_origin":"machine_accepted","parent_word_ids":[],"machine_refs":[{"hypothesis_id":hypothesis_id,"word_id":hw["id"]}]})
            event={"operation":"initial_accept","origin":origin,"target_word_ids":[],"before_text":"","after_text":" ".join(w["text"] for w in words),"hypothesis_id":hypothesis_id,"alignment_id":alignment_id,"manual_timing":None,"source_span":h["range"]}
            self._commit(transcript_id,0,words,event)
        return self.revision(transcript_id,1)

    def edit(self,transcript_id,*,expected_revision,operation,target_word_ids,replacements,origin="human"):
        _integer(expected_revision,1)
        if origin!="human": raise TranscriptError("Only explicit human edits advance accepted text")
        if operation not in ("replace","split","merge","delete","retime"): raise TranscriptError("Unsupported correction operation")
        if not isinstance(target_word_ids,list) or not target_word_ids or len(target_word_ids)>MAX_WORDS: raise TranscriptError("Ordered target word IDs are required")
        for wid in target_word_ids: _id(wid)
        if len(set(target_word_ids))!=len(target_word_ids): raise TranscriptError("Duplicate target IDs")
        if not isinstance(replacements,list) or len(replacements)>MAX_WORDS: raise TranscriptError("Bounded replacements are required")
        replacements=json.loads(_json(replacements))
        n,m=len(target_word_ids),len(replacements)
        if not ((operation in ("replace","retime") and n==m==1) or (operation=="split" and n==1 and m>=2) or (operation=="merge" and n>=2 and m==1) or (operation=="delete" and m==0)):
            raise TranscriptError("Correction cardinality does not match its operation")
        with _transaction(self.connection):
            if self.head(transcript_id)!=expected_revision: raise StaleRevision("Accepted head changed; read it again")
            old=self.revision(transcript_id,expected_revision)["words"]
            ids=[w["id"] for w in old]
            try: start=ids.index(target_word_ids[0])
            except ValueError as exc: raise TranscriptError("Target word is absent from this revision") from exc
            if ids[start:start+n]!=target_word_ids: raise TranscriptError("Targets must be contiguous and preserve accepted order")
            before=old[start:start+n]; replacement=[]
            refs=[]
            for w in before:
                for ref in w["machine_refs"]:
                    if ref not in refs: refs.append(ref)
            for r in replacements:
                if not isinstance(r,dict) or set(r)-{"text","timing"} or "text" not in r: raise TranscriptError("Replacement requires authored text and optional manual timing")
                text=_token(r["text"])
                if "timing" in r:
                    timing=r["timing"]
                    if not isinstance(timing,dict) or timing.get("status") not in ("manually_timed","unresolved"):
                        raise TranscriptError("Authored timing must be manual or unresolved")
                    provenance={"kind":"human_manual" if timing["status"]=="manually_timed" else "human_unresolved","at_revision":expected_revision+1}
                else:
                    timing,provenance=self._retained_timing(operation,before,expected_revision)
                if operation=="retime" and text!=before[0]["text"]: raise TranscriptError("Retime cannot change text")
                replacement.append({"id":before[0]["id"] if operation=="retime" else str(uuid.uuid4()),"text":text,"timing":timing,"timing_provenance":provenance,"text_origin":before[0]["text_origin"] if operation=="retime" else "human","parent_word_ids":before[0]["parent_word_ids"] if operation=="retime" else target_word_ids[:],"machine_refs":refs})
            words=old[:start]+replacement+old[start+n:]
            resolved=[w["timing"] for w in before if w["timing"]["start_us"] is not None]
            span={"start_us":min(t["start_us"] for t in resolved),"end_us":max(t["end_us"] for t in resolved)} if resolved else {"start_us":None,"end_us":None}
            event={"operation":operation,"origin":origin,"target_word_ids":target_word_ids,"before_text":" ".join(w["text"] for w in before),"after_text":" ".join(w["text"] for w in replacement),"before_words":before,"after_word_ids":[w["id"] for w in replacement],"manual_timing":[{"word_id":w["id"],"timing":w["timing"]} for w in replacement if w["timing_provenance"]["kind"]=="human_manual"] or None,"timing_decisions":[{"word_id":w["id"],"timing":w["timing"],"provenance":w["timing_provenance"]} for w in replacement],"source_span":span}
            self._commit(transcript_id,expected_revision,words,event)
        return self.revision(transcript_id,expected_revision+1)

    @staticmethod
    def _retained_timing(operation,before,base):
        """Reuse accepted source evidence; never divide an interval into guesses."""
        references={"from_revision":base,"from_word_ids":[w["id"] for w in before]}
        usable=all(w["timing"]["status"] in ("aligned","manually_timed","inherited") for w in before)
        if operation=="replace":
            timing=dict(before[0]["timing"])
            if usable:
                timing["status"]="inherited"
            return timing,{"kind":"inherited" if usable else "retained_review_required","mode":"same_interval",**references}
        if operation=="merge" and usable:
            # All target words are contiguous in accepted order. Their known outer
            # bounds need no invented internal boundaries, even when silence remains.
            timing={"start_us":before[0]["timing"]["start_us"],"end_us":before[-1]["timing"]["end_us"],"status":"inherited"}
            return timing,{"kind":"inherited","mode":"outer_span",**references}
        reason={"split":"split_internal_boundaries_unknown","merge":"merge_contains_review_required_timing"}.get(operation,"no_explicit_timing")
        return {"start_us":None,"end_us":None,"status":"unresolved"},{"kind":"unresolved","reason":reason,**references}

    def _commit(self,transcript_id,base,words,event):
        self._words(words,self.identity(transcript_id)); revision=base+1
        event={**event,"id":str(uuid.uuid4()),"transcript_id":transcript_id,"expected_revision":base,"resulting_revision":revision}
        self.connection.execute("INSERT INTO caption_revisions VALUES(?,?,?,?,?)",(transcript_id,revision,base or None,_json(words),_now()))
        self.connection.execute("INSERT INTO caption_events VALUES(?,?,?,?,?)",(event["id"],transcript_id,base,revision,_json(event)))
        if base==0:
            self.connection.execute("INSERT INTO caption_heads(transcript_id,revision) VALUES(?,?)",(transcript_id,revision))
        elif self.connection.execute("UPDATE caption_heads SET revision=? WHERE transcript_id=? AND revision=?",(revision,transcript_id,base)).rowcount!=1:
            raise StaleRevision("Accepted head changed")

    def event(self,transcript_id,revision):
        _integer(revision,1)
        row=self.connection.execute("SELECT event_json FROM caption_events WHERE transcript_id=? AND resulting_revision=?",(_id(transcript_id),revision)).fetchone()
        if row is None: raise TranscriptError("Unknown correction event")
        return json.loads(row[0])

    def list_accepted(self,*,after=0,limit=50):
        _page(after,limit)
        rows=self.connection.execute("SELECT h.sequence,h.transcript_id,h.revision,t.asset_id,t.created_utc FROM caption_heads h JOIN caption_transcripts t ON t.id=h.transcript_id WHERE h.sequence>? ORDER BY h.sequence LIMIT ?",(after,limit+1)).fetchall()
        return {"items":[dict(zip(("cursor","transcript_id","revision","asset_id","created_utc"),tuple(row))) for row in rows[:limit]],"next_cursor":rows[limit-1][0] if len(rows)>limit else None}

    def list_proposals(self,transcript_id,*,after=0,limit=50):
        _page(after,limit)
        rows=self.connection.execute("SELECT sequence,id,kind,target_id,body_sha256 FROM caption_proposals WHERE transcript_id=? AND sequence>? ORDER BY sequence LIMIT ?",(_id(transcript_id),after,limit+1)).fetchall()
        return {"items":[dict(zip(("cursor","id","kind","target_id","sha256"),tuple(row))) for row in rows[:limit]],"next_cursor":rows[limit-1][0] if len(rows)>limit else None}

    def projection_input(self,transcript_id,*,revision):
        """Return one pinned accepted revision; native owners must map source words."""
        accepted=self.revision(transcript_id,revision)
        unresolved=[w["id"] for w in accepted["words"] if w["timing"]["status"] not in ("aligned","manually_timed","inherited")]
        if unresolved: raise NeedsTimingReview("Missing, unresolved, or interpolated timing requires explicit review: "+",".join(unresolved))
        return {"accepted":accepted,"identity":self.identity(transcript_id)}
