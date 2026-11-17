"""OS-specific locking, private state files and packaged native backend discovery."""
from __future__ import annotations

import json
import os
import platform
import tempfile
from functools import lru_cache
from pathlib import Path

IS_WINDOWS = os.name == "nt"


def platform_tag():
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    system = "windows" if IS_WINDOWS else "macos" if platform.system() == "Darwin" else "linux"
    return f"{system}-{arch}"


def native_binary(root: Path):
    name = "ai-rdp-native.exe" if IS_WINDOWS else "ai-rdp-native"
    candidates = [root / "bin" / platform_tag() / name,
                  root / "build" / "native" / name,
                  root / "build" / "native" / "Release" / name]
    return next((p for p in candidates if p.is_file()), candidates[1])


def lock_state(path: Path):
    file = path.open("a+b")
    try:
        if IS_WINDOWS:
            import msvcrt
            if file.seek(0, 2) == 0:
                file.write(b"\0"); file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return file  # Caller holds this handle for the service lifetime.
    except OSError:
        file.close()
        raise BlockingIOError("Another service is using this state directory") from None


@lru_cache(maxsize=1)
def _winapi():
    import ctypes as c
    from ctypes import wintypes as w
    a = c.WinDLL("advapi32", use_last_error=True)
    k = c.WinDLL("kernel32", use_last_error=True)
    ptr = c.c_void_p
    def bind(lib, name, args, result=w.BOOL):
        f = getattr(lib, name); f.argtypes = args; f.restype = result
    bind(k, "GetCurrentProcess", [], w.HANDLE)
    bind(k, "CloseHandle", [w.HANDLE])
    bind(k, "LocalFree", [ptr], ptr)
    bind(a, "OpenProcessToken", [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)])
    bind(a, "GetTokenInformation", [w.HANDLE, c.c_int, ptr, w.DWORD, c.POINTER(w.DWORD)])
    bind(a, "GetLengthSid", [ptr], w.DWORD)
    bind(a, "ConvertSidToStringSidW", [ptr, c.POINTER(ptr)])
    bind(a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, c.POINTER(ptr), c.POINTER(w.DWORD)])
    bind(a, "SetFileSecurityW", [w.LPCWSTR, w.DWORD, ptr])
    bind(a, "GetFileSecurityW", [w.LPCWSTR, w.DWORD, ptr, w.DWORD, c.POINTER(w.DWORD)])
    bind(a, "GetSecurityDescriptorOwner", [ptr, c.POINTER(ptr), c.POINTER(w.BOOL)])
    bind(a, "GetSecurityDescriptorDacl", [ptr, c.POINTER(w.BOOL), c.POINTER(ptr), c.POINTER(w.BOOL)])
    bind(a, "EqualSid", [ptr, ptr])
    bind(a, "GetAce", [ptr, w.DWORD, c.POINTER(ptr)])
    return c, w, a, k


def _check(ok):
    if not ok:
        import ctypes
        raise ctypes.WinError(ctypes.get_last_error())


@lru_cache(maxsize=1)
def _user_sid():
    c, w, a, k = _winapi()
    token = w.HANDLE()
    _check(a.OpenProcessToken(k.GetCurrentProcess(), 8, c.byref(token)))
    try:
        needed = w.DWORD()
        a.GetTokenInformation(token, 1, None, 0, c.byref(needed))
        buffer = c.create_string_buffer(needed.value)
        _check(a.GetTokenInformation(token, 1, buffer, len(buffer), c.byref(needed)))
        sid = c.cast(buffer, c.POINTER(c.c_void_p))[0]
        raw = c.create_string_buffer(c.string_at(sid, a.GetLengthSid(sid)))
        text = c.c_void_p()
        _check(a.ConvertSidToStringSidW(sid, c.byref(text)))
        try: return raw, c.wstring_at(text)
        finally: k.LocalFree(text)
    finally:
        k.CloseHandle(token)


def restrict_private(path: Path, directory=False):
    if not IS_WINDOWS:
        path.chmod(0o700 if directory else 0o600)
        return
    c, w, a, k = _winapi()
    _, sid = _user_sid()
    inheritance = "OICI" if directory else ""
    # Set an explicit owner and protected DACL. A chmod-only check is not valid on Windows.
    sddl = f"O:{sid}D:P(A;{inheritance};FA;;;{sid})"
    descriptor = c.c_void_p()
    _check(a.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, c.byref(descriptor), None))
    try: _check(a.SetFileSecurityW(str(path.resolve()), 0x80000005, descriptor))
    finally: k.LocalFree(descriptor)


def check_private(path: Path):
    if not IS_WINDOWS:
        stat = path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise RuntimeError("runtime.json must be owned by this user and have mode 0600")
        return
    c, w, a, _ = _winapi()
    current, _ = _user_sid()
    needed = w.DWORD()
    a.GetFileSecurityW(str(path.resolve()), 5, None, 0, c.byref(needed))
    descriptor = c.create_string_buffer(needed.value)
    _check(a.GetFileSecurityW(str(path.resolve()), 5, descriptor, len(descriptor), c.byref(needed)))
    owner, dacl = c.c_void_p(), c.c_void_p()
    defaulted, present = w.BOOL(), w.BOOL()
    _check(a.GetSecurityDescriptorOwner(descriptor, c.byref(owner), c.byref(defaulted)))
    _check(a.GetSecurityDescriptorDacl(descriptor, c.byref(present), c.byref(dacl), c.byref(defaulted)))
    if not owner or not a.EqualSid(owner, current) or not present or not dacl:
        raise RuntimeError("runtime.json must have a private Windows user ACL")
    # ACL header: revision(1), reserved(1), size(2), ACE count(2), reserved(2).
    count = c.c_uint16.from_address(dacl.value + 4).value
    if not count: raise RuntimeError("runtime.json has no usable Windows user ACL")
    for index in range(count):
        ace = c.c_void_p()
        _check(a.GetAce(dacl, index, c.byref(ace)))
        # Accept only ordinary allow ACEs for this user; reject null/world/foreign ACLs.
        if c.c_ubyte.from_address(ace.value).value != 0 or not a.EqualSid(ace.value + 8, current):
            raise RuntimeError("runtime.json grants access outside this Windows user")


def prepare_state(path: Path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    restrict_private(path, directory=True)


def write_private_json(path: Path, data):
    fd, temporary = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    temporary = Path(temporary)
    try:
        restrict_private(temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            fd = -1
            json.dump(data, out)
        os.replace(temporary, path)
    finally:
        if fd >= 0: os.close(fd)
        temporary.unlink(missing_ok=True)
