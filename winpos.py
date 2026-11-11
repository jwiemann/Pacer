"""Where the dashboard window goes: the monitor it was on, its saved offset there, and that monitor's usable desktop.

Win32 only (ctypes) and no Tk: save_position() reads the geometry of the root it is given and nothing else.
"""
import ctypes, ctypes.wintypes, json, os

import pacer_config

_u32 = ctypes.windll.user32


# window position: saved on close as an offset on the monitor it was on, keyed by that monitor's hardware id
# (DeviceID, e.g. MONITOR\DEL40F4\{guid}\0001); restored only if that monitor is still connected
POS_FILE = os.path.join(pacer_config.DIR, "dashboard-position.json")


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT), ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong),
                ("szDevice", ctypes.c_wchar * 32)]


class _DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("DeviceName", ctypes.c_wchar * 32), ("DeviceString", ctypes.c_wchar * 128),
                ("StateFlags", ctypes.c_ulong), ("DeviceID", ctypes.c_wchar * 128), ("DeviceKey", ctypes.c_wchar * 128)]


def _monitor_id(device):
    """Hardware id of the monitor on display adapter `device` (e.g. DISPLAY1), or ""."""
    dd = _DISPLAY_DEVICEW()
    dd.cb = ctypes.sizeof(dd)
    if ctypes.windll.user32.EnumDisplayDevicesW(device, 0, ctypes.byref(dd), 0):
        return dd.DeviceID
    return ""


def _monitors():
    found = []
    proc_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_RECT), ctypes.c_void_p)

    def visit(hmon, _dc, _rect, _data):
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            found.append(info)
        return 1
    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc_t(visit), 0)
    return found


def save_position(root, size=None):
    """Stores the window position and size; `size` (w, h) replaces the current size (tabs-only mode keeps the size of the
    normal window)."""
    x, y = root.winfo_x(), root.winfo_y()
    hmon = ctypes.windll.user32.MonitorFromPoint(_POINT(x + root.winfo_width() // 2, y + root.winfo_height() // 2), 2)  # DEFAULTTONEAREST
    info = _MONITORINFOEXW()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info))
    data = {"monitor": _monitor_id(info.szDevice), "x": x - info.rcMonitor.left, "y": y - info.rcMonitor.top,
            "w": size[0] if size else root.winfo_width(), "h": size[1] if size else root.winfo_height()}
    with open(POS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def saved_size():
    """Saved window (width, height) or None."""
    try:
        with open(POS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        return int(saved["w"]), int(saved["h"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def saved_position(width, height):
    """Saved top-left (x, y) if its monitor is still connected, else None (caller centers the window)."""
    try:
        with open(POS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    for info in _monitors():
        if saved.get("monitor") and _monitor_id(info.szDevice) == saved["monitor"]:
            work = info.rcWork
            x = min(max(info.rcMonitor.left + saved["x"], work.left), work.right - width)
            y = min(max(info.rcMonitor.top + saved["y"], work.top), work.bottom - height)
            return x, y
    return None


def work_area(x, y):
    """(left, top, right, bottom) of the usable desktop (without taskbar) on the monitor containing the point."""
    class RECT(ctypes.Structure):
        _fields_ = [(n, ctypes.c_long) for n in ("left", "top", "right", "bottom")]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]
    mi = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    mon = _u32.MonitorFromPoint(ctypes.wintypes.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
    _u32.GetMonitorInfoW(mon, ctypes.byref(mi))
    r = mi.rcWork
    return r.left, r.top, r.right, r.bottom


