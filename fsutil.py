"""File helpers shared by the Pacer processes (tray, dashboard, governor / watchdog / updater / Copilot subprocesses).

- write_atomic / write_json_atomic: readers never see a half-written file. Windows refuses to replace a file another
  process has open, so the replace is retried for a short while.
- file_lock: a cross-process lock for read-modify-write cycles. The OS drops it when the holder dies.
- pid_lock_*: single-instance locks that record the holder's PID and start time. A lock is stale at once when its holder
  is gone (or the PID was reused by another process); the age limit only covers a holder that is alive but hung.
"""
import ctypes, json, msvcrt, os, threading, time
from contextlib import contextmanager
from ctypes import wintypes

REPLACE_TRIES, REPLACE_WAIT_S = 40, 0.05  # about 2 s of retries while a reader holds the target

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
_k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
_k32.CloseHandle.argtypes = (wintypes.HANDLE,)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE, ERROR_ACCESS_DENIED = 259, 5


def write_atomic(path, data):
    """Replace `path` with `data` in one step: str is written as UTF-8 text, bytes as is. The temp file name is unique
    per thread and process, so two writers never share (and corrupt) one temp file."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        if isinstance(data, bytes):
            with open(tmp, "wb") as f:
                f.write(data)
        else:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_TRIES - 1:
                    raise
                time.sleep(REPLACE_WAIT_S)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path, obj, indent=None):
    write_atomic(path, json.dumps(obj, indent=indent))


@contextmanager
def file_lock(lock_path, timeout=10.0):
    """Exclusive cross-process lock held while the block runs. `lock_path` is a file used only for locking: the data
    file itself cannot serve, since write_atomic must be able to replace it. Raises TimeoutError when still busy."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock busy: {lock_path}") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _creation_time(handle):
    """Process start as the FILETIME integer Windows keeps it in (unique per process incarnation), or None."""
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        return None
    created = times[0]
    return (created.dwHighDateTime << 32) | created.dwLowDateTime


def pid_alive(pid, started=None):
    """True when process `pid` runs. With `started` (its creation time) a reused PID counts as dead."""
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:  # ACCESS_DENIED: the process exists but belongs to someone else
        return ctypes.get_last_error() == ERROR_ACCESS_DENIED
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return False
        return started is None or _creation_time(handle) == started
    finally:
        _k32.CloseHandle(handle)


def _read_holder(path):
    """{"pid", "start"} of a lock file, or None when it is missing or unreadable. Older Pacer versions wrote the bare PID."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
        if text.startswith("{"):
            data = json.loads(text)
            return {"pid": int(data["pid"]), "start": data.get("start")}
        return {"pid": int(text), "start": None}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pid_lock_write(path):
    """Take the lock for this process (overwrites whatever is there; the caller decided that)."""
    handle = _k32.GetCurrentProcess()
    write_json_atomic(path, {"pid": os.getpid(), "start": _creation_time(handle)})


def pid_lock_active(path, max_age_s):
    """True while the lock's holder is alive and has refreshed the lock within max_age_s (the holder touches the file
    to show it still works, e.g. the updater every 30 s)."""
    try:
        age = time.time() - os.stat(path).st_mtime
    except OSError:
        return False
    holder = _read_holder(path)
    return age < max_age_s and holder is not None and pid_alive(holder["pid"], holder["start"])


def pid_lock_take(path, max_age_s):
    """Take the lock unless another live holder has it. The check and the write run under a file lock, so two
    processes that find a stale lock at the same moment cannot both win. False when the lock is held."""
    with file_lock(path + ".guard", timeout=10):
        if pid_lock_active(path, max_age_s):
            return False
        pid_lock_write(path)
        return True


def pid_lock_release(path):
    """Remove the lock if this process holds it (a lock taken over by someone else is left alone)."""
    holder = _read_holder(path)
    if holder and holder["pid"] == os.getpid():
        try:
            os.unlink(path)
        except OSError:
            pass
