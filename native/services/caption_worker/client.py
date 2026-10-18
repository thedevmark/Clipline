"""Standard-library parent boundary. Call run from the application's background job owner."""
from __future__ import annotations
import json
import math
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from .evidence import MAX_EVENT, MAX_WIRE, MAX_LOG, EvidenceError, canonical, digest, validate_request
from .process_tree import ProcessTree

@dataclass(frozen=True)
class RunResult:
    state: str
    error_code: str | None
    directory: Path
    hypothesis_path: Path | None
    alignment_path: Path | None

    def load(self, kind):
        if kind not in {"hypothesis","alignment"}:
            raise ValueError("Unknown machine evidence kind")
        path=self.hypothesis_path if kind=="hypothesis" else self.alignment_path
        if path is None:
            return None
        receipt=json.loads((self.directory/"receipt.json").read_text(encoding="utf-8"))
        value=json.loads(path.read_text(encoding="utf-8"))
        if digest(value)!=receipt["hashes"][kind]:
            raise EvidenceError("EVIDENCE_CHANGED","Immutable machine artifact changed")
        return value

def write_once(path, value):
    data=canonical(value)
    if path.exists():
        raise FileExistsError(path)
    temporary=path.with_name(path.name+".part")
    with temporary.open("xb") as stream:
        stream.write(data)
        stream.flush();os.fsync(stream.fileno())
    os.rename(temporary,path)
    return digest(value)

class CaptionClient:
    """One subprocess per run; one heavy run per client. No implicit profile provisioning."""
    def __init__(self, python, worker_script, *, command=None):
        self.command=command or [str(python),"-u","-B","-S",str(worker_script)]
        self._busy=threading.Lock()

    def run(self, request, output_root, *, timeout=180, cancel=None):
        if not self._busy.acquire(blocking=False):
            raise RuntimeError("A caption job is already running")
        try:
            return self._run(request,Path(output_root),timeout,cancel or threading.Event())
        finally:
            self._busy.release()

    def _run(self, request, output_root, timeout, cancel):
        try: safe_run=str(uuid.UUID(request.get("run_id","")))
        except (ValueError,AttributeError,TypeError): safe_run=str(uuid.uuid4())
        directory=output_root/safe_run
        directory.mkdir(parents=True,exist_ok=False)
        created=datetime.now(timezone.utc).isoformat()
        def lifecycle(value):
            with (directory/"lifecycle.jsonl").open("ab") as stream:
                stream.write(canonical({"state":value,"utc":datetime.now(timezone.utc).isoformat()})+b"\n")
        lifecycle("queued")
        started=time.monotonic()
        state,error,hypothesis,alignment,terminal="failed",None,None,None,None
        hypothesis_words={}
        hashes={}
        process=tree=None
        threads=[]
        events=queue.Queue(maxsize=8)
        violation=queue.Queue(maxsize=1)
        def fail(code):
            try: violation.put_nowait(code)
            except queue.Full: pass
        def reader(pipe, name, limit, parse):
            total=0
            try:
                with (directory/name).open("xb") as log:
                    while True:
                        chunk=pipe.readline(MAX_EVENT+1) if parse else pipe.read1(4096)
                        if not chunk: break
                        total+=len(chunk)
                        log.write(chunk[:max(0,limit-(total-len(chunk)))])
                        log.flush()
                        if total>limit or (parse and len(chunk)>MAX_EVENT):
                            fail("STDOUT_LIMIT" if parse else "STDERR_LIMIT");break
                        if parse:
                            if not chunk.endswith(b"\n"):
                                fail("TRUNCATED_PROTOCOL");break
                            try:
                                value=json.loads(chunk.decode("utf-8"),parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
                                events.put(value,timeout=0.2)
                            except (ValueError,UnicodeError,queue.Full,RecursionError):
                                fail("MALFORMED_PROTOCOL");break
            except OSError:
                fail("PIPE_FAILURE")
            finally:
                pipe.close()
        try:
            if type(timeout) not in {int,float} or not math.isfinite(timeout) or not 0<timeout<=900:
                raise EvidenceError("INVALID_TIMEOUT","Caption timeout must be finite, positive and at most 900 seconds")
            validate_request(request)
            hashes["request"]=write_once(directory/"request.json",request)
            if cancel.is_set():
                state,error="cancelled","CANCELLED"
            else:
                temporary=directory/"tmp";temporary.mkdir()
                env=os.environ.copy();env.update(TEMP=str(temporary),TMP=str(temporary),TMPDIR=str(temporary),PYTHONDONTWRITEBYTECODE="1")
                process=subprocess.Popen(self.command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,
                    start_new_session=os.name!="nt",creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
                tree=ProcessTree(process)
                lifecycle("running")
                for pipe,name,limit,parse in [(process.stdout,"stdout.jsonl",MAX_WIRE,True),(process.stderr,"stderr.log",MAX_LOG,False)]:
                    t=threading.Thread(target=reader,args=(pipe,name,limit,parse),daemon=True);t.start();threads.append(t)
                def send():
                    try: process.stdin.write(canonical(request)+b"\n")
                    except OSError: fail("STDIN_FAILURE")
                    finally:
                        try: process.stdin.close()
                        except OSError: pass
                sender=threading.Thread(target=send,daemon=True);sender.start();threads.append(sender)
                while True:
                    if cancel.is_set():
                        state,error="cancelled","CANCELLED";break
                    if time.monotonic()-started>timeout:
                        error="TIMEOUT";break
                    try:
                        error=violation.get_nowait();break
                    except queue.Empty: pass
                    try:
                        event=events.get(timeout=0.05)
                    except queue.Empty:
                        if process.poll() is not None and all(not t.is_alive() for t in threads) and events.empty(): break
                        continue
                    if not isinstance(event,dict) or event.get("run_id")!=request["run_id"] or terminal is not None:
                        raise EvidenceError("INVALID_SEQUENCE","unexpected event identity/order")
                    kind=event.get("event");data=event.get("data")
                    if kind=="hypothesis" and hypothesis is None and alignment is None:
                        if not isinstance(data,dict) or data.get("run_id")!=request["run_id"] or data.get("request_sha256")!=digest(request) or data.get("source")!=request["source"] or data.get("audio")!=request["audio"] or data.get("range")!=request["range"] or not isinstance(data.get("words"),list):
                            raise EvidenceError("INVALID_EVIDENCE","hypothesis is not bound to request")
                        for word in data["words"]:
                            if not isinstance(word,dict) or not isinstance(word.get("text"),str) or not isinstance(word.get("id"),str) or word["id"] in hypothesis_words:
                                raise EvidenceError("INVALID_EVIDENCE","invalid or repeated machine word identity")
                            try: uuid.UUID(word["id"])
                            except ValueError as e: raise EvidenceError("INVALID_EVIDENCE","invalid machine word UUID") from e
                            hypothesis_words[word["id"]]=word["text"]
                        hashes["hypothesis"]=write_once(directory/"hypothesis.json",data);hypothesis=directory/"hypothesis.json"
                    elif kind=="alignment" and hypothesis is not None and alignment is None:
                        if not isinstance(data,dict) or data.get("hypothesis_run_id")!=request["run_id"] or data.get("request_sha256")!=digest(request):
                            raise EvidenceError("INVALID_EVIDENCE","alignment is not bound to hypothesis")
                        if not isinstance(data.get("words"),list) or len(data["words"])!=len(hypothesis_words):
                            raise EvidenceError("INVALID_EVIDENCE","alignment word set changed")
                        seen=set();canonical_ids=list(hypothesis_words);previous_resolved_end=None
                        for index,word in enumerate(data["words"]):
                            if not isinstance(word,dict) or not isinstance(word.get("word_id"),str) or not isinstance(word.get("timing"),dict):
                                raise EvidenceError("INVALID_EVIDENCE","malformed alignment word")
                            if word.get("word_id")!=canonical_ids[index] or word.get("word_id") in seen or hypothesis_words.get(word.get("word_id"))!=word.get("text"):
                                raise EvidenceError("INVALID_EVIDENCE","alignment word lineage changed")
                            seen.add(word["word_id"]);timing=word["timing"];a,b=timing.get("start_us"),timing.get("end_us")
                            if timing["status"] not in {"aligned","missing","unresolved"}:
                                raise EvidenceError("INVALID_EVIDENCE","unexpected alignment timing status")
                            if timing["status"]=="aligned":
                                if type(a) is not int or type(b) is not int or not request["range"]["start_us"]<=a<b<=request["range"]["end_us"]:
                                    raise EvidenceError("INVALID_EVIDENCE","invalid aligned interval")
                                if previous_resolved_end is not None and a<previous_resolved_end:
                                    raise EvidenceError("INVALID_EVIDENCE","resolved alignment intervals overlap or move backwards")
                                previous_resolved_end=b
                            elif a is not None or b is not None:
                                raise EvidenceError("INVALID_EVIDENCE","unresolved timing cannot invent an interval")
                            score=word.get("alignment_score")
                            if score is not None and (type(score) not in {float,int} or not math.isfinite(score)):
                                raise EvidenceError("INVALID_EVIDENCE","invalid alignment score")
                        hashes["alignment"]=write_once(directory/"alignment.json",data);alignment=directory/"alignment.json"
                    elif kind=="terminal" and isinstance(data,dict) and data.get("state") in {"complete","partial","failed"}:
                        terminal=data
                    else:
                        raise EvidenceError("INVALID_SEQUENCE","unexpected protocol event")
                # EOF and a reader violation can arrive between the loop's queue check and exit.
                if error is None:
                    try: error=violation.get_nowait()
                    except queue.Empty: pass
                if error is None:
                    code=process.wait(timeout=2)
                    if terminal is None:
                        error="WORKER_CRASH" if code else "MISSING_TERMINAL"
                    elif terminal["state"]=="complete" and (code!=0 or hypothesis is None or alignment is None):
                        error="INCOMPLETE_RESULT"
                    elif terminal["state"]=="complete":
                        state="complete"
                    else:
                        state=terminal["state"];error=terminal.get("error_code") or "WORKER_FAILED"
        except EvidenceError as e:
            error=e.code
        except (OSError,subprocess.SubprocessError,KeyError,TypeError,ValueError) as e:
            error="LAUNCH_OR_IO_FAILURE"
            write_once(directory/"parent-error.json",{"type":type(e).__name__,"message":str(e)[:1000]})
        finally:
            if tree:
                tree.stop()
            elif process and process.poll() is None:
                process.kill()
            if process:
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: error="PROCESS_CLEANUP_FAILED"
            for t in threads: t.join(timeout=2)
            if tree: tree.close()
        if error and state!="cancelled": state="partial" if hypothesis else "failed"
        lifecycle(state)
        requested_id=request.get("run_id") if isinstance(request,dict) else None
        write_once(directory/"receipt.json",{"schema_version":1,"run_id":safe_run,"requested_run_id":requested_id if isinstance(requested_id,str) else None,"state":state,"error_code":error,
            "created_utc":created,"finished_utc":datetime.now(timezone.utc).isoformat(),
            "hashes":hashes,"worker_exit_code":process.returncode if process else None,
            "elapsed_seconds_uncontrolled":time.monotonic()-started,"accepted_corrections_modified":False})
        return RunResult(state,error,directory,hypothesis,alignment)
