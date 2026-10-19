"""Standard-library parent boundary. Call run from the application's background job owner."""
from __future__ import annotations
import json
import copy
import math
import os
import queue
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime,timezone
from pathlib import Path
from .evidence import MAX_EVENT, MAX_WIRE, MAX_LOG, EvidenceError, canonical, digest, validate_request, validate_hint_evidence
from .process_tree import ProcessTree

@dataclass(frozen=True)
class RunResult:
    state: str
    error_code: str | None
    directory: Path
    hypothesis_path: Path | None
    alignment_path: Path | None
    owner: object | None = None

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


class ExecutionUncertain(RuntimeError):
    """The caller must retain this existing owner before formatting the error."""
    def __init__(self, message, owner, directory):
        self.owner, self.directory = owner, directory
        super().__init__(failure_message(message))


def failure_message(value):
    """Diagnostics must not erase a capability when an exception cannot format."""
    try:
        return str(value)[:1000]
    except BaseException:
        return type(value).__name__


@dataclass
class _CapturedProcess:
    """References captured by the launching client/preparer, never from a PID."""
    directory: Path
    process: object
    tree: object
    threads: list
    closed_scope: dict | None = None
    gate_argv: tuple | None = None
    gate_released: bool = True
    thread_starts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _ExecutionIdentity:
    nonce: str
    directory: Path
    run_id: str
    request_sha256: str | None
    command_sha256: str | None


def gated_command(python):
    return [str(python), '-u', '-B', '-S', str(Path(__file__).with_name('launch_gate.py'))]


def _is_unreleased_gate(captured):
    argv = captured.gate_argv
    return (not captured.gate_released and argv is not None and len(argv) == 5
            and tuple(getattr(captured.process, 'args', ())) == argv
            and argv[1:4] == ('-u', '-B', '-S')
            and argv[4] == str(Path(__file__).with_name('launch_gate.py')))


def gate_frame(command):
    if (not isinstance(command, (list, tuple)) or not command
            or any(not isinstance(arg, str) or not arg or '\0' in arg for arg in command)):
        raise ValueError('An explicit command argv is required')
    frame = canonical(list(command)) + b'\n'
    if len(frame) > 65536:
        raise ValueError('The gated argv exceeds 64 KiB')
    return frame


def start_registered(captured, thread):
    """Retain the exact Thread before start can create an OS thread or abort."""
    captured.threads.append(thread)
    captured.thread_starts[thread] = 'starting'
    try:
        thread.start()
    except BaseException:
        if thread.ident is not None:
            captured.thread_starts[thread] = 'started'
        else:
            captured.thread_starts[thread] = 'ambiguous'
        raise
    captured.thread_starts[thread] = 'started'


def _wait_bound(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 5:
        raise ValueError('Use a finite observation wait in [0,5] seconds')
    return value


def _sample_process(captured, *, wait_seconds=0, stop=False):
    """Observe the original objects; only an explicit stop requests termination."""
    wait_seconds = _wait_bound(wait_seconds)
    faults = []
    scope = {'disposition': 'unknown', 'active_processes': None}
    if captured.closed_scope is not None:
        scope = dict(captured.closed_scope)
    elif captured.tree is not None:
        try:
            scope = captured.tree.stop() if stop else captured.tree.observe()
        except BaseException as exc:
            faults.append('scope:' + type(exc).__name__)
    elif _is_unreleased_gate(captured):
        # Only the fixed launch_gate.py ran. Its argv frame was not sent.
        # Reaping this gate is not a JobObject-zero assertion.
        try:
            if stop and captured.process.poll() is None:
                captured.process.kill()
            if captured.process.poll() is not None:
                scope = {'disposition': 'never_started', 'scope': 'unreleased_gate_parent',
                         'gate_released': False, 'requested_work_started': False}
        except BaseException as exc:
            faults.append('unattached_parent:' + type(exc).__name__)
    deadline = time.monotonic() + wait_seconds
    code = None
    try:
        code = captured.process.poll()
        if code is None and time.monotonic() < deadline:
            try:
                code = captured.process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
    except BaseException as exc:
        faults.append('parent:' + type(exc).__name__)
    if (captured.tree is None and _is_unreleased_gate(captured)
            and code is not None and not faults):
        scope = {'disposition': 'never_started', 'scope': 'unreleased_gate_parent',
                 'gate_released': False, 'requested_work_started': False}
    readers_stopped = True
    for thread in captured.threads:
        try:
            start = captured.thread_starts.get(thread)
            if start is not None:
                # An entered start can expose ident before public start completes.
                # Only a successful public join can settle that exact reader.
                thread.join(max(0, deadline - time.monotonic()))
            elif thread.is_alive() and time.monotonic() < deadline:
                thread.join(max(0, deadline - time.monotonic()))
            if thread.is_alive():
                readers_stopped = False
        except BaseException as exc:
            faults.append('reader:' + type(exc).__name__)
            readers_stopped = False
    zero = (isinstance(scope, dict) and (
        scope.get('disposition') == 'stopped'
        and type(scope.get('active_processes')) is int and scope['active_processes'] == 0
        or captured.tree is None and _is_unreleased_gate(captured)
        and scope.get('disposition') == 'never_started' and code is not None))
    streams_closed = False
    if zero and code is not None and readers_stopped and not faults:
        try:
            for pipe in (captured.process.stdin, captured.process.stdout, captured.process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()
            streams_closed = all(p is None or p.closed for p in (
                captured.process.stdin, captured.process.stdout, captured.process.stderr))
        except BaseException as exc:
            faults.append('streams:' + type(exc).__name__)
    return {'scope': scope, 'parent_reaped': code is not None, 'exit_code': code,
            'readers_stopped': readers_stopped, 'streams_closed': streams_closed,
            'ceased': zero and code is not None and readers_stopped and streams_closed and not faults,
            'faults': faults, 'resources_released': captured.closed_scope is not None}


def _release_process(captured, proof):
    if proof['ceased'] is not True:
        raise RuntimeError('The original scope, parent and streams are not settled')
    if captured.closed_scope is None and captured.tree is not None:
        if captured.tree.close() is not True:
            raise OSError('The original process scope handle could not be closed')
        captured.closed_scope = dict(proof['scope'])
    proof['resources_released'] = True

class CaptionClient:
    """One subprocess per run; one heavy run per client. No implicit profile provisioning."""
    def __init__(self, python, worker_script, *, command=None):
        self.python = str(python)
        self.command=command or [str(python),"-u","-B","-S",str(worker_script)]
        self._busy=threading.Lock()
        self._captured = None
        self._last_observation = None
        self._execution = None
        self._execution_capture = None

    @property
    def current_execution(self):
        """Opaque same-process identity; a copied/decoded identity is not accepted."""
        return self._execution

    @property
    def has_unsettled(self):
        return self._captured is not None

    def observe_execution(self, *, wait_seconds=0, expected_execution=None):
        return self._observe_execution(wait_seconds, stop=False, expected_execution=expected_execution)

    def cleanup_execution(self, *, wait_seconds=5, expected_execution=None):
        return self._observe_execution(wait_seconds, stop=True, expected_execution=expected_execution)

    def _observe_execution(self, wait_seconds, *, stop, expected_execution=None):
        _wait_bound(wait_seconds)
        if not self._busy.acquire(False):
            raise ExecutionUncertain('The original caption operation is still running', self,
                                     self._captured.directory if self._captured else None)
        try:
            return self._observe_locked(wait_seconds, stop, expected_execution)
        except BaseException as exc:
            if isinstance(exc, ExecutionUncertain):
                raise
            raise ExecutionUncertain(exc, self,
                                     self._captured.directory if self._captured else None) from exc
        finally:
            self._busy.release()

    def _observe_locked(self, wait_seconds, stop, expected_execution):
        if expected_execution is not None and self._execution is not expected_execution:
            raise ExecutionUncertain('The original caption execution identity changed', self, None)
        captured = self._captured
        if captured is None:
            return json.loads(canonical({**(self._last_observation or {
                'ceased': True, 'resources_released': True, 'never_launched': True}),
                'owner_unsettled': False}))
        proof = _sample_process(captured, wait_seconds=wait_seconds, stop=stop)
        write_once(captured.directory / ('live-observation-' + str(uuid.uuid4()) + '.json'), proof)
        if proof['ceased']:
            _release_process(captured, proof)
            write_once(captured.directory / ('live-release-' + str(uuid.uuid4()) + '.json'), proof)
            self._last_observation = proof
            self._captured = None
        return json.loads(canonical({**proof, 'owner_unsettled': self.has_unsettled}))

    @contextmanager
    def settlement_observation(self, expected_execution):
        """Keep this owner's existing operation lock through a live DB settlement."""
        if expected_execution is None or not self._busy.acquire(False):
            raise ExecutionUncertain('The original execution is unavailable or busy', self, None)
        try:
            proof = self._observe_locked(0, False, expected_execution)
            if not proof.get('ceased') or proof.get('owner_unsettled') or not proof.get('resources_released'):
                raise ExecutionUncertain('The original execution is not settled', self,
                                         expected_execution.directory)
            yield proof
        except BaseException as exc:
            if isinstance(exc, ExecutionUncertain):
                raise
            raise ExecutionUncertain(exc, self, expected_execution.directory) from exc
        finally:
            self._busy.release()

    def run(self, request, output_root, *, timeout=180, cancel=None):
        if not self._busy.acquire(blocking=False):
            raise ExecutionUncertain("A caption job is already running", self,
                                     self._captured.directory if self._captured else None)
        try:
            if self.has_unsettled:
                raise ExecutionUncertain('Settle the original caption execution before another launch', self,
                                         self._captured.directory)
            return self._run(request,Path(output_root),timeout,cancel or threading.Event())
        except BaseException as exc:
            if self.has_unsettled and not isinstance(exc, ExecutionUncertain):
                raise ExecutionUncertain(exc, self, self._captured.directory) from exc
            raise
        finally:
            self._busy.release()

    def _run(self, request, output_root, timeout, cancel):
        request = copy.deepcopy(request)
        execution_command = tuple(self.command) if isinstance(self.command, (list, tuple)) else self.command
        try: safe_run=str(uuid.UUID(request.get("run_id","")))
        except (ValueError,AttributeError,TypeError): safe_run=str(uuid.uuid4())
        directory=output_root/safe_run
        directory.mkdir(parents=True,exist_ok=False)
        try:
            request_hash = digest(request)
        except (ValueError, TypeError, RecursionError):
            request_hash = None
        try:
            command_hash = digest(execution_command)
        except (ValueError, TypeError, RecursionError):
            command_hash = None
        self._execution = _ExecutionIdentity(str(uuid.uuid4()), directory.resolve(), safe_run,
                                             request_hash, command_hash)
        self._execution_capture = None
        self._last_observation = {'scope': {'disposition': 'never_started', 'active_processes': 0},
            'parent_reaped': True, 'readers_stopped': True, 'streams_closed': True,
            'ceased': True, 'resources_released': True, 'faults': []}
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
        process_scope={"disposition":"never_started"}
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
                prefix = gate_frame(execution_command)
                process=subprocess.Popen(gated_command(self.python),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,
                    start_new_session=os.name!="nt",creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
                self._captured = _CapturedProcess(directory, process, None, threads,
                                                 gate_argv=tuple(process.args), gate_released=False)
                self._execution_capture = self._captured
                process_scope={"disposition":"unknown"}
                tree=ProcessTree(process)
                self._captured.tree = tree
                lifecycle("running")
                for pipe,name,limit,parse in [(process.stdout,"stdout.jsonl",MAX_WIRE,True),(process.stderr,"stderr.log",MAX_LOG,False)]:
                    t=threading.Thread(target=reader,args=(pipe,name,limit,parse),daemon=True)
                    start_registered(self._captured, t)
                def send():
                    try:
                        self._captured.gate_released = True
                        process.stdin.write(prefix + canonical(request)+b"\n")
                    except OSError: fail("STDIN_FAILURE")
                    finally:
                        try: process.stdin.close()
                        except OSError: pass
                sender=threading.Thread(target=send,daemon=True)
                start_registered(self._captured, sender)
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
                        validate_hint_evidence(request, data)
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
                        validate_hint_evidence(request, data)
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
            proof = None
            if self._captured is not None:
                proof = _sample_process(self._captured, wait_seconds=5, stop=True)
                process_scope = proof['scope']
                if not proof['ceased']:
                    error = 'PROCESS_CLEANUP_FAILED'
        streams_closed = proof['streams_closed'] if proof is not None else True
        if error and state!="cancelled": state="partial" if hypothesis else "failed"
        lifecycle(state)
        requested_id=request.get("run_id") if isinstance(request,dict) else None
        write_once(directory/"receipt.json",{"schema_version":1,"run_id":safe_run,"requested_run_id":requested_id if isinstance(requested_id,str) else None,"state":state,"error_code":error,
            "created_utc":created,"finished_utc":datetime.now(timezone.utc).isoformat(),
            "hashes":hashes,"worker_exit_code":0 if process is not None and process.returncode==0 else None,
            "launch_process_exit_code":process.returncode if process is not None else None,
            "launch_process_role":"trusted_stdin_gate",
            "process_scope":process_scope,"streams_closed":streams_closed,
            "elapsed_seconds_uncontrolled":time.monotonic()-started,"accepted_corrections_modified":False})
        if self._captured is not None and proof['ceased']:
            _release_process(self._captured, proof)
            write_once(directory / ('live-release-' + str(uuid.uuid4()) + '.json'), proof)
            self._last_observation = proof
            self._captured = None
        return RunResult(state,error,directory,hypothesis,alignment,self if self.has_unsettled else None)
