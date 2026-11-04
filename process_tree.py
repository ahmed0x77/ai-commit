"""Bound a CLI and all its children to the lifetime of one invocation."""
from contextlib import contextmanager
import os
import signal
import subprocess


@contextmanager
def launch(args, **kwargs):
    if os.name != "nt":
        process = subprocess.Popen(args, start_new_session=True, **kwargs)
        try:
            yield process
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
        return

    import ctypes as c
    from ctypes import wintypes as w

    class BasicLimit(c.Structure):
        _fields_ = [("process_time", c.c_longlong), ("job_time", c.c_longlong),
                    ("flags", w.DWORD), ("min_ws", c.c_size_t), ("max_ws", c.c_size_t),
                    ("active", w.DWORD), ("affinity", c.c_size_t),
                    ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class ExtendedLimit(c.Structure):
        _fields_ = [("basic", BasicLimit), ("io", c.c_ulonglong * 6),
                    ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                    ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    native = c.WinDLL("ntdll")
    kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    native.NtResumeProcess.argtypes = [w.HANDLE]
    native.NtResumeProcess.restype = w.LONG
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise OSError("Cannot create the CLI process group")
    process = None
    try:
        limits = ExtendedLimit()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
            raise OSError("Cannot configure CLI process cleanup")
        # Assign before resuming, so even immediate startup children join the job.
        process = subprocess.Popen(args, creationflags=0x4 | subprocess.CREATE_NO_WINDOW, **kwargs)
        if not kernel.AssignProcessToJobObject(job, int(process._handle)):
            raise OSError("Cannot isolate the CLI process group")
        if native.NtResumeProcess(int(process._handle)) < 0:
            raise OSError("Cannot start the CLI process group")
        yield process
    finally:
        kernel.CloseHandle(job)  # Terminate this invocation and its children only.
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
