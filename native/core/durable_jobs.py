"""Durable job intent and resource admission over CreatorLibrary.connection.

This store launches/stops no process. A trusted runtime verifier must establish
termination and output disposition before settlement releases resource holds.
No leases expire, running jobs retry, or orphan outputs disappear implicitly.
"""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib,json,re,sqlite3,uuid
from pathlib import Path

STATES={"queued","running","cancel-requested","completed","failed","cancelled","interrupted"}
ACTIVE={"running","cancel-requested","interrupted"}
KINDS={"caption","render","preparation","io"}
MAX_JSON=65536
class JobError(ValueError):pass
class IntentConflict(JobError):pass
class StaleTransition(JobError):pass
class VerificationRequired(JobError):pass

def _json(v):
    try:text=json.dumps(v,sort_keys=True,separators=(",",":"),allow_nan=False)
    except (ValueError,TypeError,RecursionError) as e:raise JobError("Finite JSON required") from e
    if len(text.encode())>MAX_JSON:raise JobError("Intent exceeds 64 KiB")
    return text

def _id(v):
    try:
        if not isinstance(v,str) or str(uuid.UUID(v))!=v:raise ValueError()
    except (ValueError,TypeError,AttributeError) as e:raise JobError("Canonical UUID required") from e
    return v

def _sha(v):
    if not isinstance(v,str) or not re.fullmatch("[0-9a-f]{64}",v):raise JobError("SHA256 required")
    return v

def _integer(v,low=0,high=2**63-1):
    if type(v) is not int or not low<=v<=high:raise JobError("Integer outside bounds")
    return v

def _reference(v):
    if not isinstance(v,dict) or set(v)!={"id","sha256"}:raise JobError("Only stable artifact ID and SHA256 references are allowed")
    return {"id":_id(v["id"]),"sha256":_sha(v["sha256"])}

def _now():return datetime.now(timezone.utc).isoformat()

@contextmanager
def _write(c):
    if c.in_transaction:raise JobError("Finish the caller transaction first")
    c.execute("BEGIN IMMEDIATE")
    try:
        yield
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK");raise

def install_schema(c):
    if c.execute("PRAGMA foreign_keys").fetchone()[0]!=1:raise JobError("Foreign keys must be enabled")
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='assets'").fetchone():raise JobError("Existing CreatorLibrary required")
    with _write(c):
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='durable_job_schema'").fetchone():
            row=c.execute("SELECT version FROM durable_job_schema WHERE singleton=1").fetchone()
            if row is None or row[0]!=1:raise JobError("Unsupported job schema")
            return
        statement=""
        for line in Path(__file__).with_name("durable_jobs_schema.sql").read_text(encoding="utf8").splitlines(keepends=True):
            statement+=line
            if sqlite3.complete_statement(statement):c.execute(statement);statement=""
        if statement.strip():raise JobError("Incomplete migration")

class DurableJobs:
    def __init__(self,connection,*,settlement_verifier=None,completed_output_committer=None):
        self.c=connection;self.verifier=settlement_verifier
        if completed_output_committer is not None and not callable(completed_output_committer):
            raise JobError("A trusted output committer must be callable")
        self.completed_output_committer=completed_output_committer
        if connection.execute("PRAGMA foreign_keys").fetchone()[0]!=1:raise JobError("Foreign keys required")
        row=connection.execute("SELECT version FROM durable_job_schema WHERE singleton=1").fetchone()
        if row is None or row[0]!=1:raise JobError("Install job schema first")

    def _intent(self,kind,value):
        if not isinstance(kind,str) or kind not in KINDS or not isinstance(value,dict):raise JobError("Known job kind and structured intent required")
        allowed={"request_ref","assets","snapshot","kit","transcript","profile_sha256","recipe_sha256","range"}
        if set(value)-allowed:raise JobError("Unknown intent fields; no URLs, credentials, or arbitrary payload")
        if "request_ref" not in value:raise JobError("Every job requires a complete immutable execution request reference")
        v={k:value.get(k) for k in allowed};v["request_ref"]=_reference(v["request_ref"]);v["assets"]=value.get("assets",[])
        if not isinstance(v["assets"],list) or len(v["assets"])>64:raise JobError("At most 64 pinned assets required")
        seen=set()
        for a in v["assets"]:
            if not isinstance(a,dict) or set(a)!={"asset_id","sha256"}:raise JobError("Pinned asset ID/SHA256 only")
            _id(a["asset_id"]);_sha(a["sha256"])
            if a["asset_id"] in seen:raise JobError("Duplicate asset pin")
            seen.add(a["asset_id"])
        v["assets"]=sorted(v["assets"],key=lambda a:a["asset_id"])
        if kind!="io" and not v["assets"]:raise JobError("Heavy jobs need pinned source assets")
        for key in ("profile_sha256","recipe_sha256"):
            if v[key] is not None:_sha(v[key])
        if kind in ("caption","preparation") and v["profile_sha256"] is None:raise JobError("Caption/preparation profile must be pinned")
        snap=v["snapshot"]
        if snap is not None:
            if not isinstance(snap,dict) or set(snap)!={"project_id","generation","revision","sha256","engine","engine_version"}:raise JobError("Complete immutable ProjectStore snapshot reference required")
            _id(snap["project_id"]);_id(snap["generation"]);_integer(snap["revision"]);_sha(snap["sha256"])
            for key in ("engine","engine_version"):
                if not isinstance(snap[key],str) or not snap[key].strip() or len(snap[key])>128 or any(x in snap[key] for x in ("://","?","\n")):raise JobError("Bounded engine identity required")
        if kind=="render" and (snap is None or v["recipe_sha256"] is None):raise JobError("Render requires native snapshot and recipe pins")
        for key,id_key,number in (("kit","kit_id","version"),("transcript","transcript_id","revision")):
            ref=v[key]
            if ref is not None:
                if not isinstance(ref,dict) or set(ref)!={id_key,number}:raise JobError("Stable version reference required")
                _id(ref[id_key]);_integer(ref[number],1)
        r=v["range"]
        if r is not None:
            if not isinstance(r,dict) or set(r)!={"start_us","end_us"}:raise JobError("Source range only")
            _integer(r["start_us"]);_integer(r["end_us"])
            if r["start_us"]>=r["end_us"]:raise JobError("Ordered source interval required")
        return json.loads(_json(v))

    def submit(self,kind,intent,*,idempotency_key):
        _id(idempotency_key);v=self._intent(kind,intent);encoded=_json(v)
        digest=hashlib.sha256(_json({"kind":kind,"input":v}).encode()).hexdigest()
        with _write(self.c):
            old=self.c.execute("SELECT id,intent_sha256 FROM durable_job_intents WHERE idempotency=?",(idempotency_key,)).fetchone()
            if old:
                if old[1]!=digest:raise IntentConflict("Idempotency key already pins different input")
                return self.job(old[0])
            for a in v["assets"]:
                row=self.c.execute("SELECT sha256 FROM assets WHERE id=?",(a["asset_id"],)).fetchone()
                if row is None or row[0]!=a["sha256"]:raise JobError("Asset fingerprint differs from library")
            if v["kit"]:
                if not self.c.execute("SELECT 1 FROM kit_versions WHERE kit_id=? AND version=?",(v["kit"]["kit_id"],v["kit"]["version"])).fetchone():raise JobError("Unknown Kit revision")
            if v["transcript"]:
                if not self.c.execute("SELECT 1 FROM caption_revisions WHERE transcript_id=? AND revision=?",(v["transcript"]["transcript_id"],v["transcript"]["revision"])).fetchone():raise JobError("Unknown accepted transcript revision")
            jid=str(uuid.uuid4());output=str(uuid.uuid4())
            self.c.execute("INSERT INTO durable_job_intents(id,idempotency,kind,intent_json,intent_sha256,output_set_id,created_utc) VALUES(?,?,?,?,?,?,?)",(jid,idempotency_key,kind,encoded,digest,output,_now()))
            self.c.execute("INSERT INTO durable_job_heads(job_id,state,revision,cancel_requested) VALUES(?,'queued',1,0)",(jid,))
            self.c.executemany("INSERT INTO durable_job_assets VALUES(?,?,?)",[(jid,a["asset_id"],a["sha256"]) for a in v["assets"]])
            if v["kit"]:self.c.execute("INSERT INTO durable_job_kits VALUES(?,?,?)",(jid,v["kit"]["kit_id"],v["kit"]["version"]))
            if v["transcript"]:self.c.execute("INSERT INTO durable_job_transcripts VALUES(?,?,?)",(jid,v["transcript"]["transcript_id"],v["transcript"]["revision"]))
            self._event(jid,1,"queued",{"intent_sha256":digest})
        return self.job(jid)

    def job(self,jid):
        _id(jid)
        row=self.c.execute("SELECT i.id,i.kind,i.intent_json,i.intent_sha256,i.output_set_id,h.state,h.revision,h.claim_token,h.owner_id,h.cancel_requested,h.launch_ref FROM durable_job_intents i JOIN durable_job_heads h ON h.job_id=i.id WHERE i.id=?",(jid,)).fetchone()
        if row is None:raise JobError("Unknown job")
        keys=("id","kind","input","intent_sha256","output_set_id","state","revision","claim_token","owner_id","cancel_requested","launch_ref")
        v=dict(zip(keys,tuple(row)));v["input"]=json.loads(v["input"]);v["launch_ref"]=json.loads(v["launch_ref"]) if v["launch_ref"] else None;v["cancel_requested"]=bool(v["cancel_requested"])
        return v

    def _event(self,jid,revision,state,detail):
        encoded=_json(detail)
        if len(encoded)>4096:raise JobError("Event exceeds bounded metadata")
        self.c.execute("INSERT INTO durable_job_events(job_id,revision,state,detail_json,created_utc) VALUES(?,?,?,?,?)",(jid,revision,state,encoded,_now()))

    def _cas(self,job,*,state=None,token=None,owner=None,cancel=None,launch=None,detail=None):
        revision=job["revision"]+1;state=state or job["state"]
        changed=self.c.execute("UPDATE durable_job_heads SET state=?,revision=?,claim_token=?,owner_id=?,cancel_requested=?,launch_ref=? WHERE job_id=? AND revision=?",(state,revision,token or job["claim_token"],owner or job["owner_id"],int(job["cancel_requested"] if cancel is None else cancel),_json(launch) if launch else _json(job["launch_ref"]) if job["launch_ref"] else None,job["id"],job["revision"])).rowcount
        if changed!=1:raise StaleTransition("Job revision changed")
        self._event(job["id"],revision,state,detail or {})

    def claim_next(self,*,owner_id,kinds=None,job_id=None):
        _id(owner_id)
        if job_id is not None:_id(job_id)
        if kinds is None:kinds=sorted(KINDS)
        if not isinstance(kinds,(list,tuple)) or not kinds or len(kinds)>len(KINDS) or any(not isinstance(k,str) or k not in KINDS for k in kinds) or len(set(kinds))!=len(kinds):
            raise JobError("Explicit supported job kinds required")
        with _write(self.c):
            free={}
            for row in self.c.execute("SELECT s.resource,s.slot FROM durable_job_slots s WHERE NOT EXISTS(SELECT 1 FROM durable_job_holds h WHERE h.resource=s.resource AND h.slot=s.slot) ORDER BY s.slot"):
                free.setdefault(row[0],row[1])
            if "io" not in free:return None
            query="SELECT i.id FROM durable_job_intents i JOIN durable_job_heads h ON h.job_id=i.id WHERE h.state='queued' AND i.kind IN ("+",".join("?" for _ in kinds)+")"
            parameters=list(kinds)
            if job_id is not None:query+=" AND i.id=?";parameters.append(job_id)
            if "heavy" not in free:query+=" AND i.kind='io'"
            row=self.c.execute(query+" ORDER BY i.sequence LIMIT 1",parameters).fetchone()
            if row is None:return None
            job=self.job(row[0]);token=str(uuid.uuid4());resources=["io"] if job["kind"]=="io" else ["heavy","io"]
            for resource in resources:self.c.execute("INSERT INTO durable_job_holds VALUES(?,?,?,?)",(resource,free[resource],job["id"],token))
            self._cas(job,state="running",token=token,owner=owner_id,detail={"claim_token":token,"owner_id":owner_id,"resources":resources})
            jid=job["id"]
        return self.job(jid)

    def _expected(self,jid,revision,token=None):
        _integer(revision,1);job=self.job(jid)
        if job["revision"]!=revision or token is not None and job["claim_token"]!=_id(token):raise StaleTransition("Stale job revision or worker token")
        return job

    def record_launch_intent(self,jid,*,expected_revision,claim_token,launch_ref):
        _id(claim_token);ref=_reference(launch_ref)
        with _write(self.c):
            job=self._expected(jid,expected_revision,claim_token)
            if job["state"]!="running" or job["launch_ref"] is not None:raise StaleTransition("Launch intent cannot change")
            self._cas(job,launch=ref,detail={"launch_intent":ref,"process_started":False})
        return self.job(jid)

    def request_cancel(self,jid,*,expected_revision):
        with _write(self.c):
            job=self._expected(jid,expected_revision)
            if job["state"]=="queued":state="cancelled"
            elif job["state"] in ACTIVE:state="interrupted" if job["state"]=="interrupted" else "cancel-requested"
            else:raise StaleTransition("Terminal job cannot be cancelled again")
            self._cas(job,state=state,cancel=True,detail={"process_stop_claimed":False})
        return self.job(jid)

    def mark_interrupted(self,jid,*,expected_revision,reason):
        if reason not in ("restart","owner_lost","cleanup_uncertain"):raise JobError("Explicit interruption reason required")
        with _write(self.c):
            job=self._expected(jid,expected_revision)
            if job["state"] not in ("running","cancel-requested"):raise StaleTransition("Only an active execution can be interrupted")
            self._cas(job,state="interrupted",detail={"reason":reason,"process_stop_claimed":False,"resource_holds_retained":True})
        return self.job(jid)

    def finish(self,jid,*,expected_revision,claim_token,outcome,evidence_ref):
        return self._settle(jid,expected_revision,claim_token,outcome,evidence_ref,reconcile=False)
    def reconcile(self,jid,*,expected_revision,claim_token,outcome,evidence_ref):
        return self._settle(jid,expected_revision,claim_token,outcome,evidence_ref,reconcile=True)

    def _settle(self,jid,revision,token,outcome,evidence_ref,*,reconcile):
        if outcome not in ("completed","failed","cancelled"):raise JobError("Explicit terminal outcome required")
        _id(token);evidence=_reference(evidence_ref);job=self._expected(jid,revision,token)
        valid_states={"interrupted"} if reconcile else {"running","cancel-requested"}
        if job["state"] not in valid_states:raise StaleTransition("Wrong settlement/reconciliation state")
        if self.verifier is None:raise VerificationRequired("Runtime termination and output verifier is not integrated")
        # Potential process/file verification stays outside the SQLite write lock.
        # The second CAS below rejects cancellation/interruption during verification.
        attested=self.verifier(json.loads(_json(job)),evidence)
        keys={"job_id","claim_token","intent_sha256","output_set_id","process_disposition","outputs_disposition","result_ref","evidence_ref"}
        if not isinstance(attested,dict) or set(attested)!=keys:raise VerificationRequired("Complete runtime attestation required")
        if any(attested[k]!=job[j] for k,j in (("job_id","id"),("claim_token","claim_token"),("intent_sha256","intent_sha256"),("output_set_id","output_set_id"))) or attested["evidence_ref"]!=evidence:raise VerificationRequired("Evidence belongs to another execution or snapshot")
        if attested["process_disposition"] not in ("stopped","never_started"):raise VerificationRequired("A live/unknown process cannot release resources")
        if outcome=="completed":
            if job["cancel_requested"] or attested["outputs_disposition"]!="verified":raise VerificationRequired("Cancelled or unverified outputs cannot complete")
            _reference(attested["result_ref"])
        elif attested["outputs_disposition"] not in ("absent","quarantined"):raise VerificationRequired("Failed/cancelled outputs require explicit safe disposition")
        elif attested["result_ref"] is not None:_reference(attested["result_ref"])
        if outcome=="cancelled" and not job["cancel_requested"]:raise VerificationRequired("Cancellation must be requested first")
        with _write(self.c):
            current=self._expected(jid,revision,token)
            if current["state"] not in valid_states:raise StaleTransition("Job state changed during verification")
            if outcome=="completed" and self.completed_output_committer is not None:
                result=self.completed_output_committer(self.c,json.loads(_json(current)),json.loads(_json(attested)))
                if result is not None:raise VerificationRequired("Output committer must return None")
            self.c.execute("INSERT INTO durable_job_settlements VALUES(?,?,?)",(jid,token,_json(attested)))
            self._cas(current,state=outcome,detail={"evidence_ref":evidence,"outputs_disposition":attested["outputs_disposition"]})
            self.c.execute("DELETE FROM durable_job_holds WHERE job_id=? AND claim_token=?",(jid,token))
        return self.job(jid)

    def list_jobs(self,*,after=0,limit=50,inflight_only=False):
        _integer(after);_integer(limit,1,200)
        if type(inflight_only) is not bool:raise JobError("Boolean filter required")
        query="SELECT i.sequence,i.id,i.kind,h.state,h.revision,h.claim_token,h.owner_id,h.cancel_requested,i.intent_sha256,i.output_set_id FROM durable_job_intents i JOIN durable_job_heads h ON h.job_id=i.id WHERE i.sequence>?"
        if inflight_only:query+=" AND h.state IN ('running','cancel-requested','interrupted')"
        rows=self.c.execute(query+" ORDER BY i.sequence LIMIT ?",(after,limit+1)).fetchall()
        keys=("cursor","id","kind","state","revision","claim_token","owner_id","cancel_requested","intent_sha256","output_set_id")
        return {"items":[dict(zip(keys,tuple(r))) for r in rows[:limit]],"next_cursor":rows[limit-1][0] if len(rows)>limit else None}

    def resources(self):
        return [dict(zip(("resource","slot","job_id","claim_token"),tuple(r))) for r in self.c.execute("SELECT resource,slot,job_id,claim_token FROM durable_job_holds ORDER BY resource,slot")]

    def settlement(self,jid):
        _id(jid)
        row=self.c.execute("SELECT attestation_json FROM durable_job_settlements WHERE job_id=?",(jid,)).fetchone()
        return json.loads(row[0]) if row is not None else None
