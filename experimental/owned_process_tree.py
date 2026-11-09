"""Own CLI process tree; Windows kill-on-close job assigned before stdin input.

An unnamed non-inheritable job handle belongs only to this launch. The trusted
CLI reads the supplied prompt from stdin after assignment; no PID-wide scan.
Refs: Microsoft Learn Job Objects and JOBOBJECT_EXTENDED_LIMIT_INFORMATION.
"""

import os, signal


class OwnedTree:
    def __init__(self, process):
        self.process = process
        self.handle = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [
                ("user", c.c_longlong),
                ("job_user", c.c_longlong),
                ("flags", w.DWORD),
                ("min_ws", c.c_size_t),
                ("max_ws", c.c_size_t),
                ("active", w.DWORD),
                ("affinity", c.c_size_t),
                ("priority", w.DWORD),
                ("scheduling", w.DWORD),
            ]

        class IO(c.Structure):
            _fields_ = [(n, c.c_ulonglong) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

        class Extended(c.Structure):
            _fields_ = [
                ("basic", Basic),
                ("io", IO),
                ("process_memory", c.c_size_t),
                ("job_memory", c.c_size_t),
                ("peak_process", c.c_size_t),
                ("peak_job", c.c_size_t),
            ]

        k = c.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.AssignProcessToJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            raise c.WinError(c.get_last_error())
        limits = Extended()
        limits.basic.flags = 0x2000
        try:
            if not k.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            if not k.AssignProcessToJobObject(handle, w.HANDLE(int(process._handle))):
                raise c.WinError(c.get_last_error())
        except BaseException:
            k.CloseHandle(handle)
            raise
        self.kernel = k
        self.handle = handle

    def close(self):
        if os.name == "nt":
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
        else:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
