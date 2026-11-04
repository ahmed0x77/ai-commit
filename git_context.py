"""Read the invoking Git command to reject history-rewriting commit modes.

Git's hook arguments do not distinguish `commit -m AI --amend` from a normal
commit. Inspect only our own process ancestry, without executing shell text.
"""
import os
from pathlib import Path


def windows_process(pid):
    import ctypes as c
    from ctypes import wintypes as w

    class Basic(c.Structure):
        _fields_ = [("reserved", c.c_void_p), ("peb", c.c_void_p),
                    ("reserved2", c.c_void_p * 2), ("pid", c.c_size_t),
                    ("parent", c.c_size_t)]

    class Unicode(c.Structure):
        _fields_ = [("length", w.USHORT), ("maximum", w.USHORT), ("buffer", c.c_void_p)]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    native = c.WinDLL("ntdll")
    shell = c.WinDLL("shell32")
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    native.NtQueryInformationProcess.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.ULONG,
                                                c.POINTER(w.ULONG)]
    native.NtQueryInformationProcess.restype = w.LONG
    shell.CommandLineToArgvW.argtypes = [w.LPCWSTR, c.POINTER(c.c_int)]
    shell.CommandLineToArgvW.restype = c.POINTER(w.LPWSTR)
    kernel.LocalFree.argtypes = [c.c_void_p]
    kernel.LocalFree.restype = c.c_void_p
    handle = kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        raise OSError("Cannot query the invoking process")
    try:
        basic = Basic()
        if native.NtQueryInformationProcess(handle, 0, c.byref(basic), c.sizeof(basic), None) < 0:
            raise OSError("Cannot read the invoking process ancestry")
        size = w.ULONG()
        native.NtQueryInformationProcess(handle, 60, None, 0, c.byref(size))
        if not size.value or size.value > 1_000_000:
            raise OSError("Cannot read the invoking command")
        data = c.create_string_buffer(size.value)
        if native.NtQueryInformationProcess(handle, 60, data, size, None) < 0:
            raise OSError("Cannot read the invoking command")
        text = Unicode.from_buffer(data)
        command = c.wstring_at(text.buffer, text.length // 2)
        count = c.c_int()
        argv = shell.CommandLineToArgvW(command, c.byref(count))
        if not argv:
            raise OSError("Cannot parse the invoking command")
        try:
            return basic.parent, [argv[i] for i in range(count.value)]
        finally:
            kernel.LocalFree(c.cast(argv, c.c_void_p))
    finally:
        kernel.CloseHandle(handle)


def invoking_commit_args():
    pid = os.getpid()
    for _ in range(16):
        if os.name == "nt":
            parent, args = windows_process(pid)
        else:
            proc = Path("/proc") / str(pid)
            args = proc.joinpath("cmdline").read_bytes().decode().strip("\0").split("\0")
            # The process name in stat may itself contain spaces or parentheses.
            fields = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()
            parent = int(fields[1])
        if args and Path(args[0]).name.lower() in ("git", "git.exe") and "commit" in args[1:]:
            return args[args.index("commit") + 1:]
        if not parent or parent == pid:
            break
        pid = parent
    raise OSError("Cannot identify the invoking Git commit command")


def is_history_operation(args):
    for arg in args:
        if arg == "--":
            break
        if arg == "--amend" or arg.split("=", 1)[0] in ("--fixup", "--squash"):
            return True
    return False
