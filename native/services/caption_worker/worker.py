"""JSONL stdout only. Backend diagnostics go to stderr. Exactly one request per process."""
from __future__ import annotations
import contextlib
import json
import math
import sys
import traceback
from pathlib import Path
sys.dont_write_bytecode=True
if __package__ in {None,""}:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from caption_worker.evidence import MAX_REQUEST,MAX_EVENT,EvidenceError,canonical,validate_request,make_hypothesis,make_alignment,hash_file,digest

def machine_json(value):
    if isinstance(value,dict): return {k:machine_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [machine_json(v) for v in value]
    if isinstance(value,float): return value if math.isfinite(value) else None
    if value is None or isinstance(value,(str,int,bool)): return value
    if hasattr(value,"item"): return machine_json(value.item())
    raise EvidenceError("INVALID_BACKEND_RESULT",f"Unsupported machine result type {type(value).__name__}")

def emit(kind,run_id,data):
    payload=canonical({"event":kind,"run_id":run_id,"data":data})+b"\n"
    if len(payload)>MAX_EVENT:
        raise EvidenceError("RESULT_TOO_LARGE","Worker event exceeds protocol limit")
    sys.stdout.buffer.write(payload);sys.stdout.buffer.flush()

def main():
    request=None;has_hypothesis=False
    try:
        line=sys.stdin.buffer.readline(MAX_REQUEST+1)
        if len(line)>MAX_REQUEST or not line.endswith(b"\n"):
            raise EvidenceError("INVALID_REQUEST","Missing or oversized request frame")
        try: request=validate_request(json.loads(line))
        except (ValueError,UnicodeError,RecursionError) as e: raise EvidenceError("INVALID_REQUEST",str(e)) from e
        from caption_worker import backend
        with contextlib.redirect_stdout(sys.stderr):
            print("phase=preflight",file=sys.stderr,flush=True)
            profile=backend.preflight(request)
            print("phase=optional_runtime_import",file=sys.stderr,flush=True)
            segments,align,runtime=backend.load(request,profile)
        files={p.name:hash_file(p) for p in Path(__file__).parent.glob("*.py")}
        runtime["worker_build"]={"sha256":digest(files),"files":files}
        runtime["nonfinite_machine_values"]="null; not fabricated zero/confidence"
        hypothesis=make_hypothesis(request,machine_json(segments),runtime)
        emit("hypothesis",request["run_id"],hypothesis);has_hypothesis=True
        with contextlib.redirect_stdout(sys.stderr):
            raw=align()
        alignment=make_alignment(request,hypothesis,machine_json(raw),runtime)
        emit("alignment",request["run_id"],alignment)
        emit("terminal",request["run_id"],{"state":"complete","error_code":None})
        return 0
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        code=e.code if isinstance(e,EvidenceError) else "BACKEND_FAILURE"
        try:
            emit("terminal",request.get("run_id") if isinstance(request,dict) else None,
                 {"state":"partial" if has_hypothesis else "failed","error_code":code,"message":str(e)[:1000]})
        except Exception:
            return 2
        return 1

if __name__=="__main__":
    raise SystemExit(main())
