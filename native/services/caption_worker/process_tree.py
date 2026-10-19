"""Own only the launched worker tree. The worker waits for stdin until attachment completes."""
import os
import signal
import time
import uuid

class ProcessTree:
    def __init__(self, process):
        self.process = process
        self.job = None
        self.scope_id = str(uuid.uuid4())
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w
        class Basic(c.Structure):
            _fields_=[("process_time",c.c_longlong),("job_time",c.c_longlong),("flags",w.DWORD),
                ("min_working",c.c_size_t),("max_working",c.c_size_t),("active",w.DWORD),
                ("affinity",c.c_size_t),("priority",w.DWORD),("scheduling",w.DWORD)]
        class IO(c.Structure):
            _fields_=[(x,c.c_ulonglong) for x in ["read_ops","write_ops","other_ops","read_bytes","write_bytes","other_bytes"]]
        class Extended(c.Structure):
            _fields_=[("basic",Basic),("io",IO),("process_memory",c.c_size_t),("job_memory",c.c_size_t),
                      ("peak_process",c.c_size_t),("peak_job",c.c_size_t)]
        class Accounting(c.Structure):
            _fields_=[("user_time",c.c_longlong),("kernel_time",c.c_longlong),
                ("period_user_time",c.c_longlong),("period_kernel_time",c.c_longlong),
                ("page_faults",w.DWORD),("total_processes",w.DWORD),
                ("active_processes",w.DWORD),("terminated_processes",w.DWORD)]
        k=c.WinDLL("kernel32",use_last_error=True)
        k.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];k.CreateJobObjectW.restype=w.HANDLE
        k.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD];k.SetInformationJobObject.restype=w.BOOL
        k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];k.AssignProcessToJobObject.restype=w.BOOL
        k.TerminateJobObject.argtypes=[w.HANDLE,w.UINT];k.TerminateJobObject.restype=w.BOOL
        k.QueryInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p]
        k.QueryInformationJobObject.restype=w.BOOL
        k.CloseHandle.argtypes=[w.HANDLE];k.CloseHandle.restype=w.BOOL
        job=k.CreateJobObjectW(None,None)
        if not job:
            raise OSError(c.get_last_error(),"CreateJobObject failed")
        limits=Extended();limits.basic.flags=0x2000 # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        handle=k.OpenProcess(0x0100|0x0001,False,process.pid)
        try:
            if not k.SetInformationJobObject(job,9,c.byref(limits),c.sizeof(limits)) or not handle or not k.AssignProcessToJobObject(job,handle):
                raise OSError(c.get_last_error(),"Cannot attach worker to owned Job Object")
        except BaseException:
            k.CloseHandle(job)
            raise
        finally:
            if handle:
                k.CloseHandle(handle)
        self.kernel,self.job,self.accounting=k,job,Accounting

    def observe(self):
        """Query this actual scope without terminating or releasing its handle."""
        evidence = {"scope_id": self.scope_id, "pid": self.process.pid,
                    "scope": "windows_job_object" if os.name == "nt" else "process_group",
                    "disposition": "unknown", "active_processes": None}
        if self.job:
            import ctypes as c
            accounting = self.accounting()
            if self.kernel.QueryInformationJobObject(self.job, 1, c.byref(accounting),
                                                     c.sizeof(accounting), None):
                evidence["active_processes"] = accounting.active_processes
                evidence["disposition"] = "stopped" if accounting.active_processes == 0 else "running"
            else:
                evidence["query_error"] = c.get_last_error()
        return evidence

    def stop(self):
        """Verify the owned Windows scope is empty while its handle is still live."""
        evidence={"scope_id":self.scope_id,"pid":self.process.pid,
                  "scope":"windows_job_object" if os.name=="nt" else "process_group",
                  "disposition":"unknown","active_processes":None}
        if self.job:
            import ctypes as c
            terminated=bool(self.kernel.TerminateJobObject(self.job,1))
            evidence["terminate_requested"]=terminated
            if not terminated:evidence["terminate_error"]=c.get_last_error()
            deadline=time.monotonic()+5
            while True:
                accounting=self.accounting()
                if not self.kernel.QueryInformationJobObject(self.job,1,c.byref(accounting),c.sizeof(accounting),None):
                    evidence["query_error"]=c.get_last_error()
                    break
                evidence["active_processes"]=accounting.active_processes
                if accounting.active_processes==0:
                    evidence["disposition"]="stopped"
                    break
                if time.monotonic()>=deadline:break
                time.sleep(.01)
        elif os.name!="nt" and self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGKILL)
        return evidence

    def close(self):
        if self.job:
            if not self.kernel.CloseHandle(self.job):
                return False
            self.job=None
        return True
