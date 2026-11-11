"""Start the Pacer tray at Windows logon: one value in HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run (no admin needed)."""
import subprocess, winreg

import pacer_config

KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "Pacer"


def command():
    return subprocess.list2cmdline(pacer_config.self_cmd("tray"))


def is_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
            return bool(winreg.QueryValueEx(k, NAME)[0])  # the installer writes the same entry in its own quoting
    except OSError:
        return False


def set_enabled(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(k, NAME)
            except FileNotFoundError:
                pass
