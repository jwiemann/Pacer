"""Secrets of the Pacer, stored encrypted for the current Windows user (DPAPI, CryptProtectData).

The file in the data dir is useless on another account or machine, and the key never appears in the settings file, a log
or the process arguments. Only the OpenRouter management key lives here. `mask()` is what the UI may show of a key: its
first and last characters.
"""
import ctypes, os
from ctypes import wintypes

import fsutil, pacer_config

OPENROUTER_MGMT = os.path.join(pacer_config.DIR, "openrouter-mgmt.key")
ENTROPY = b"Pacer openrouter management key"  # binds the blob to this use: another program of the user cannot just unprotect it
MIN_LEN = 24
MASK_HEAD, MASK_TAIL = 12, 4


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf  # buf must stay alive with the blob


def _crypt(fn, data):
    inb, keep = _blob(data)
    ent, keep2 = _blob(ENTROPY)
    out = _Blob()
    if fn is ctypes.windll.crypt32.CryptProtectData:
        ok = fn(ctypes.byref(inb), "Pacer", ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(inb), None, ctypes.byref(ent), None, None, 0, ctypes.byref(out))
    del keep, keep2
    if not ok:
        raise OSError("DPAPI refused the data")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect(data):
    return _crypt(ctypes.windll.crypt32.CryptProtectData, data)


def unprotect(data):
    return _crypt(ctypes.windll.crypt32.CryptUnprotectData, data)


def mask(key):
    """The part of a key the UI may show: first and last characters ('sk-or-v1-a3f...9c2d')."""
    return key[:MASK_HEAD] + "..." + key[-MASK_TAIL:] if key and len(key) >= MIN_LEN else ""


def save(path, key):
    """Stores `key` encrypted. Raises ValueError for something that cannot be a key."""
    key = (key or "").strip()
    if len(key) < MIN_LEN or any(c.isspace() for c in key):
        raise ValueError("that does not look like an API key")
    fsutil.write_atomic(path, protect(key.encode("utf-8")))


def load(path):
    """The stored key, or None (no file, other user, damaged)."""
    try:
        with open(path, "rb") as f:
            return unprotect(f.read()).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
