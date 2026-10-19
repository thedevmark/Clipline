"""Own one local Filmcraft process; synchronous background caller required.

The unauthenticated native socket remains a local trust boundary. The listener
PID is checked against the owned Windows Job Object before and after readiness.
That does not remove the check/connect race or authenticate later bridge calls.
No timeline, save retry, process adoption, or crash reconciliation lives here.
"""
from __future__ import annotations
from contextlib import ExitStack
from dataclasses import dataclass
import ctypes as c
from ctypes import wintypes as w
import hashlib, json, math, os
from pathlib import Path
import socket, subprocess, sys, time, uuid
from native.services.filmcraft_bridge import FilmcraftBridge, EditorUnavailable, OutcomeUnknown
from native.services.native_checkpoint import WorkingCopy, MAX_DOCUMENT
from native.services.caption_worker.process_tree import ProcessTree
from native.services.caption_audio import _source_read_guard

class SessionError(RuntimeError):
    def __init__(self, reason, directory=None, *, owner=None, cleanup_error=None):
        self.directory=directory
        self.owner=owner
        self.cleanup_error=cleanup_error
        super().__init__(reason)

class SessionUnavailable(SessionError):
    """Control is absent or the owned parent exited; this proves no scope cessation."""

@dataclass(frozen=True)
class QualifiedEditor:
    path: Path
    sha256: str
    version: str
    def verify(self):
        if not isinstance(self.path,Path) or not self.path.is_absolute() or not self.path.is_file():
            raise ValueError("An absolute qualified editor executable is required")
        if not isinstance(self.sha256,str) or len(self.sha256)!=64 or any(x not in "0123456789abcdef" for x in self.sha256):
            raise ValueError("A canonical executable SHA256 is required")
        if not isinstance(self.version,str) or not self.version.strip() or len(self.version)>128:
            raise ValueError("A bounded qualified editor version is required")
        if digest(self.path)!=self.sha256:raise ValueError("The qualified editor executable changed")
    def command(self, port, data, working):
        return [str(self.path),"--control",str(port),"--data-dir",str(data),"--no-recover",str(working)]

def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda:stream.read(1024*1024),b""):h.update(block)
    return h.hexdigest()

def write_once(path, value):
    encoded=json.dumps(value,ensure_ascii=False,allow_nan=False,indent=2).encode("utf8")
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush();os.fsync(stream.fileno())
    return encoded

def listener_pids(port):
    """Read IPv4 LISTEN owner PIDs from the Windows TCP table; no PID adoption."""
    api=c.WinDLL("iphlpapi",use_last_error=True)
    api.GetExtendedTcpTable.argtypes=[c.c_void_p,c.POINTER(w.DWORD),w.BOOL,w.ULONG,c.c_int,w.ULONG]
    api.GetExtendedTcpTable.restype=w.DWORD
    size=w.DWORD(0)
    result=api.GetExtendedTcpTable(None,c.byref(size),False,socket.AF_INET,3,0)
    if result not in (0,122):raise OSError(result,"Cannot size TCP listener table")
    for _ in range(3):
        if size.value>16*1024*1024:raise OSError("TCP listener table is too large")
        buf=c.create_string_buffer(size.value)
        result=api.GetExtendedTcpTable(buf,c.byref(size),False,socket.AF_INET,3,0)
        if result==122:continue
        if result:raise OSError(result,"Cannot inspect TCP listeners")
        count=int.from_bytes(buf.raw[:4],"little")
        if 4+count*24>len(buf):raise OSError("Malformed TCP listener table")
        found=set()
        for offset in range(4,4+count*24,24):
            row=buf.raw[offset:offset+24]
            # MIB_TCPROW_OWNER_PID: state, local address/port, remote address/port, PID.
            address=row[4:8]
            if int.from_bytes(row[:4],"little")==2 and socket.ntohs(int.from_bytes(row[8:12],"little")&65535)==port:
                if address in (b"\x7f\x00\x00\x01",b"\x00"*4):found.add(int.from_bytes(row[20:24],"little"))
        return found
    raise OSError("TCP listener table changed repeatedly")

def in_job(tree, pid):
    api=c.WinDLL("kernel32",use_last_error=True)
    api.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];api.OpenProcess.restype=w.HANDLE
    api.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)];api.IsProcessInJob.restype=w.BOOL
    api.CloseHandle.argtypes=[w.HANDLE];api.CloseHandle.restype=w.BOOL
    handle=api.OpenProcess(0x1000,False,pid)
    if not handle:return False
    try:
        result=w.BOOL()
        if not api.IsProcessInJob(handle,tree.job,c.byref(result)):raise OSError(c.get_last_error(),"Cannot inspect listener process scope")
        return bool(result.value)
    finally:api.CloseHandle(handle)


def _project_identity(value):
    if (not isinstance(value,dict) or not isinstance(value.get("path"),str)
            or len(value["path"])>32768 or type(value.get("revision")) is not int
            or not 0<=value["revision"]<2**64 or type(value.get("dirty")) is not bool):
        raise SessionError("The native project identity is invalid")
    return {key:value[key] for key in ("path","revision","dirty")}

class SessionBridge(FilmcraftBridge):
    def __init__(self, owner):
        super().__init__(owner.port, timeout=10)
        self.owner=owner
    def call(self, method, params=None):
        self.owner._assert_usable()
        return super().call(method, params)

class NativeSession:
    @classmethod
    def start(cls, store, working, editor, *, python, runs, cancel=None, readiness_seconds=30,
              managed_exit_nonce=None):
        if os.name!="nt":raise ValueError("Native session process ownership is qualified on Windows only")
        if type(working) is not WorkingCopy:raise ValueError("An explicit WorkingCopy is required")
        if type(editor) is not QualifiedEditor:raise ValueError("An explicit qualified editor is required")
        if managed_exit_nonce is not None:
            if not isinstance(managed_exit_nonce, str):
                raise ValueError("A canonical managed-exit UUID4 is required")
            nonce = uuid.UUID(managed_exit_nonce)
            if str(nonce) != managed_exit_nonce or nonce.version != 4:
                raise ValueError("A canonical managed-exit UUID4 is required")
        if type(readiness_seconds) not in (int,float) or not math.isfinite(readiness_seconds) or not 0<readiness_seconds<=60:
            raise ValueError("Readiness deadline must be finite and within 60 seconds")
        python=Path(python)
        if not python.is_absolute() or not python.is_file():raise ValueError("An explicit helper Python is required")
        editor.verify()
        path=working.path.resolve(strict=True)
        root=(store.directory/"working").resolve()
        if not path.is_file() or path.suffix!=".fcproj" or not path.is_relative_to(root) or not 0<path.stat().st_size<=MAX_DOCUMENT:
            raise ValueError("Use an owned, bounded Filmcraft working file")
        current=store.load()
        if current is None or current.engine!="filmcraft" or current.generation!=working.base_generation or current.revision!=working.base_revision:
            raise ValueError("The working copy publication base changed")
        self=cls();self.working=working;self.editor=editor;self.path=path;self.process=None;self.tree=None;self._bridge=None
        self._stopped=False;self._stop_final=False;self._files=[];self._lock=None;self.child_pid=None
        self._input_guards=ExitStack()
        self.directory=Path(runs).resolve()/str(uuid.uuid4())
        self.directory.mkdir(parents=True,exist_ok=False)
        (self.directory/"data").mkdir()
        self.initial_hash=digest(path)
        self.managed_exit = None
        self._lease(path.parent/"editor-owner.lock")
        try:
            write_once(path.parent/"editor-owner-uncertain.json",{
                "run":str(self.directory),"state":"launching_or_running_unsettled"})
            if cancel is not None and cancel.is_set():raise SessionError("Launch cancelled before process creation")
            reserve=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
            try:
                reserve.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                reserve.bind(("127.0.0.1",0));self.port=reserve.getsockname()[1]
                argv=editor.command(self.port,self.directory/"data",path)
                if managed_exit_nonce is not None:
                    manifest = self.directory / "managed-exit.json"
                    encoded = write_once(manifest, {"schema": 1, "nonce": managed_exit_nonce,
                        "workingPath": str(path), "workingSHA256": self.initial_hash,
                        "outputDirectory": str(self.directory / "managed-exit")})
                    self._guard_input(manifest)
                    if manifest.read_bytes() != encoded:
                        raise SessionError("Managed launch manifest changed before admission")
                    self.managed_exit = {"nonce": managed_exit_nonce,
                        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
                        "directory": self.directory / "managed-exit", "manifest": manifest}
                    argv.extend(("--managed-exit", str(manifest)))
                gate=Path(__file__).with_name("filmcraft_launch_gate.py")
                for launch_input in dict.fromkeys((editor.path,python,gate)):
                    self._guard_input(launch_input)
                editor.verify()
                intent=write_once(self.directory/"intent.json",{"schema":1,"working":str(path),"working_sha256":self.initial_hash,
                    "base_generation":working.base_generation,"base_revision":working.base_revision,
                    "editor":{"path":str(editor.path),"sha256":editor.sha256,"version":editor.version},
                    "helper":{"python":str(python),"python_sha256":digest(python),"gate_sha256":digest(gate)},
                    "argv":argv,"child_receipt":str(self.directory/"child.json"),
                    "managed_exit": {"nonce": managed_exit_nonce,
                        "manifest_sha256": self.managed_exit["manifest_sha256"]} if self.managed_exit else None,
                    "control_authentication":"none","port_binding_race":"unresolved"})
                self._guard_input(self.directory/"intent.json")
                if (self.directory/"intent.json").read_bytes()!=intent:
                    raise SessionError("Native launch intent changed before admission")
                out=(self.directory/"stdout.log").open("xb");self._files.append(out)
                err=(self.directory/"stderr.log").open("xb");self._files.append(err)
                env=os.environ.copy()
                for key in ("FILMCRAFT_CONTROL_PORT","FILMCRAFT_DATA_DIR"):env.pop(key,None)
                self.process=subprocess.Popen([str(python),"-I",str(gate),str(self.directory/"intent.json")],
                    stdin=subprocess.PIPE,stdout=out,stderr=err,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
                self.tree=ProcessTree(self.process)
                # The gate cannot create a native child before Job Object attachment.
            finally:reserve.close()
            self.process.stdin.write(b"start\n");self.process.stdin.flush();self.process.stdin.close()
            deadline=time.monotonic()+readiness_seconds
            while True:
                if cancel is not None and cancel.is_set():raise SessionError("Launch cancelled during readiness")
                if self.process.poll() is not None:raise SessionError("The native process exited before readiness")
                remaining=deadline-time.monotonic()
                if remaining<=0:raise SessionError("Native editor readiness deadline expired")
                child_file=self.directory/"child.json"
                if child_file.exists():
                    child=json.loads(child_file.read_text(encoding="utf8"));self.child_pid=child["pid"]
                    pids=listener_pids(self.port)
                    if pids and (pids!={self.child_pid} or not in_job(self.tree,self.child_pid)):
                        raise SessionError("The native control port belongs to another process")
                    if pids:
                        bridge=FilmcraftBridge(self.port,timeout=min(remaining,.5))
                        try:
                            ui=bridge.call("ui.inspect")
                            if not isinstance(ui,dict) or not isinstance(ui.get("window"),list) or not isinstance(ui.get("ui"),dict):
                                raise SessionError("The control peer is not a native editor UI")
                            remaining=deadline-time.monotonic()
                            if remaining<=0:raise SessionError("Native editor readiness deadline expired")
                            bridge=FilmcraftBridge(self.port,timeout=min(remaining,.5))
                            project_before=_project_identity(bridge.execute("project.inspect"))
                            remaining=deadline-time.monotonic()
                            if remaining<=0:raise SessionError("Native editor readiness deadline expired")
                            bridge=FilmcraftBridge(self.port,timeout=min(remaining,.5))
                            validation=bridge.execute("clipline.validate",{"expectedRevision":project_before["revision"]})
                            remaining=deadline-time.monotonic()
                            if remaining<=0:raise SessionError("Native editor readiness deadline expired")
                            bridge=FilmcraftBridge(self.port,timeout=min(remaining,.5))
                            project_after=_project_identity(bridge.execute("project.inspect"))
                            write_once(self.directory/("readiness-identity-"+str(uuid.uuid4())+".json"),{
                                "project_before":project_before,"project_after":project_after,
                                "validation":{"valid":validation.get("valid") if isinstance(validation,dict) else None,
                                              "revision":validation.get("revision") if isinstance(validation,dict) else None}})
                        except EditorUnavailable:pass
                        except OutcomeUnknown as exc:
                            # Read-only readiness calls may be repeated; no mutation is retried.
                            write_once(self.directory/("readiness-unknown-"+str(uuid.uuid4())+".json"),
                                {"request_id":exc.request_id,"method":exc.method,"reason":str(exc)})
                        else:
                            if (not isinstance(validation,dict) or validation.get("valid") is not True
                                    or type(validation.get("revision")) is not int
                                    or validation["revision"]!=project_before["revision"]
                                    or project_before["path"]!=str(path) or project_after!=project_before):
                                raise SessionError("The native editor opened a different or invalid project")
                            if listener_pids(self.port)!={self.child_pid} or not in_job(self.tree,self.child_pid):
                                raise SessionError("Native control listener ownership changed during readiness")
                            if digest(path)!=self.initial_hash:raise SessionError("The working file changed during startup; retained for reconciliation")
                            write_once(self.directory/"ready.json",{"schema":1,"pid":self.child_pid,"gate_pid":self.process.pid,
                                "scope_id":self.tree.scope_id,"port":self.port,"project":project_after,"validation":validation,
                                "ui":{"window":ui["window"]},"control_authentication":"none",
                                "limitations":["listener check/connect race","no later-call authentication"]})
                            self._bridge=SessionBridge(self)
                            return self
                if cancel is not None:cancel.wait(min(.025,max(0,deadline-time.monotonic())))
                else:time.sleep(min(.025,max(0,deadline-time.monotonic())))
        except BaseException as exc:
            try:write_once(self.directory/"launch-error.json",{"type":type(exc).__name__,"reason":str(exc)[:4000]})
            except OSError:pass
            cleanup_error=None
            try:self.stop()
            except Exception as cleanup:
                cleanup_error=str(cleanup)[:4000]
                try:write_once(self.directory/"launch-cleanup-error.json",{"type":type(cleanup).__name__,"reason":cleanup_error})
                except OSError:pass
            raise SessionError(str(exc),self.directory,owner=self,cleanup_error=cleanup_error) from exc

    def _lease(self,path):
        import msvcrt
        self._lock=path.open("a+b");self._lock.seek(0,2)
        if self._lock.tell()==0:self._lock.write(b"0");self._lock.flush()
        self._lock.seek(0)
        try:msvcrt.locking(self._lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self._lock.close();self._lock=None
            raise SessionError("Another owner holds this working copy",self.directory)
        if (self.path.parent/"editor-owner-uncertain.json").exists():
            msvcrt.locking(self._lock.fileno(),msvcrt.LK_UNLCK,1)
            self._lock.close();self._lock=None
            raise SessionError("A prior uncertain process scope requires reconciliation",self.directory)

    def _guard_input(self,path):
        protected=_source_read_guard(path)
        if protected is not None:
            self._input_guards.callback(protected[0].CloseHandle,protected[1])

    def _assert_usable(self):
        if self._stopped or self._bridge is None or self.process.poll() is not None:
            raise SessionUnavailable("The native editor is not usable",self.directory,owner=self)
        try:
            listeners=listener_pids(self.port)
        except OSError as error:
            raise SessionError("The native control listener ownership could not be verified",
                               self.directory,owner=self) from error
        if listeners==set():
            raise SessionUnavailable("The native control listener is absent",self.directory,owner=self)
        if listeners!={self.child_pid}:
            raise SessionError("The native control listener no longer belongs to this session",
                               self.directory,owner=self)
        try:
            belongs=in_job(self.tree,self.child_pid)
        except OSError as error:
            raise SessionError("The native control process ownership could not be verified",
                               self.directory,owner=self) from error
        if not belongs:
            raise SessionError("The native control listener no longer belongs to this session",
                               self.directory,owner=self)

    @property
    def bridge(self):
        self._assert_usable()
        return self._bridge

    def observe_exit(self):
        """Observe actual cessation; never quit, terminate, or close a live scope.

        The caller keeps this owner strongly alive on every unknown result.
        Document settlement remains separate from process-resource cleanup.
        """
        if self._stop_final:
            return self.stop_receipt
        proof = (self.tree.observe() if self.tree is not None else
                 {"disposition": "unknown", "active_processes": None})
        parent_code = self.process.poll() if self.process is not None else None
        result = {"schema": 1, "scope": proof, "exit_code": parent_code,
                  "parent_reaped": parent_code is not None, "streams_closed": False}
        if (parent_code is None or proof.get("disposition") != "stopped"
                or type(proof.get("active_processes")) is not int or proof["active_processes"] != 0):
            return result
        self._stopped = True
        self._bridge = None
        for stream in self._files:
            stream.close()
        result["streams_closed"] = all(stream.closed for stream in self._files)
        # Persist observed evidence before releasing the only queryable scope.
        write_once(self.directory / ("observed-exit-" + str(uuid.uuid4()) + ".json"), result)
        marker = self.path.parent / "editor-owner-uncertain.json"
        marker_data = json.loads(marker.read_text(encoding="utf8"))
        if not isinstance(marker_data, dict) or marker_data.get("run") != str(self.directory):
            raise SessionError("The native working lease identity changed", self.directory, owner=self)
        marker.unlink()
        if self._lock:
            import msvcrt
            self._lock.seek(0)
            msvcrt.locking(self._lock.fileno(), msvcrt.LK_UNLCK, 1)
            self._lock.close()
            self._lock = None
        self._input_guards.close()
        self.tree.close()
        self.stop_receipt = result
        self._stop_final = True
        return result

    def stop(self):
        """Terminate only the owned scope. Keep working bytes, recovery data and receipts."""
        if self._stop_final:return self.stop_receipt
        self._stopped=True;self._bridge=None
        proof={"disposition":"never_started","active_processes":0}
        try:
            if self.tree:
                try:proof=self.tree.stop()
                except Exception as exc:proof={"disposition":"unknown","reason":str(exc)[:2000]}
                if proof.get("disposition")=="stopped" and proof.get("active_processes")==0:
                    self.tree.close()
            elif self.process:
                # A gated process can create no descendants before successful attachment.
                try:self.process.kill();self.process.wait(timeout=5)
                except Exception:pass
                proof={"disposition":"unknown","pid":self.process.pid,"reason":"Job Object attachment did not complete"}
            if self.process:
                if self.process.stdin and not self.process.stdin.closed:self.process.stdin.close()
                try:self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:proof["disposition"]="unknown"
        finally:
            for stream in self._files:stream.close()
            marker=self.path.parent/"editor-owner-uncertain.json"
            self._stop_final=proof["disposition"] in ("stopped","never_started") and proof.get("active_processes")==0
            # Without successful attachment the stdin gate was never released.
            # A reaped gate has no native reader, even though scope admission
            # remains quarantined rather than claiming Job Object proof.
            gate_reaped=self.tree is None and self.process is not None and self.process.poll() is not None
            if self._stop_final or gate_reaped:
                self._input_guards.close()
            if self._stop_final:
                try:
                    marker_data=json.loads(marker.read_text(encoding="utf8"))
                    if not isinstance(marker_data,dict):raise ValueError("Quarantine marker must be an object")
                    if marker_data.get("run")==str(self.directory):marker.unlink()
                except (OSError,ValueError,TypeError) as exc:proof["quarantine_release_error"]=str(exc)[:2000]
            if self._lock:
                import msvcrt
                self._lock.seek(0);msvcrt.locking(self._lock.fileno(),msvcrt.LK_UNLCK,1);self._lock.close();self._lock=None
        try:working_hash=digest(self.path) if self.path.exists() else None
        except OSError as exc:
            working_hash=None;proof["working_hash_error"]=str(exc)[:2000]
        self.stop_receipt={"schema":1,"scope":proof,"exit_code":self.process.returncode if self.process else None,
            "working_sha256":working_hash,"data_retained":str(self.directory/"data"),
            "streams_closed":all(x.closed for x in self._files)}
        stop_path=self.directory/"stop.json"
        if stop_path.exists():stop_path=self.directory/("stop-"+str(uuid.uuid4())+".json")
        try:write_once(stop_path,self.stop_receipt)
        except OSError as exc:raise SessionError("Native stop evidence could not be retained",self.directory) from exc
        return self.stop_receipt
