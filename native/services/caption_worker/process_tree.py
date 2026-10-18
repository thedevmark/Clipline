"""Own only the launched worker tree. The worker waits for stdin until attachment completes."""
import os
import signal

class ProcessTree:
    def __init__(self, process):
        self.process = process
        self.job = None
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
        k=c.WinDLL("kernel32",use_last_error=True)
        k.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];k.CreateJobObjectW.restype=w.HANDLE
        k.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD];k.SetInformationJobObject.restype=w.BOOL
        k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
        k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE];k.AssignProcessToJobObject.restype=w.BOOL
        k.TerminateJobObject.argtypes=[w.HANDLE,w.UINT];k.TerminateJobObject.restype=w.BOOL
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
        self.kernel,self.job=k,job

    def stop(self):
        if self.job:
            self.kernel.TerminateJobObject(self.job,1)
        elif self.process.poll() is None:
            os.killpg(self.process.pid,signal.SIGKILL)

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job)
            self.job=None
