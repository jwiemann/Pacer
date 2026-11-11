"""Keeps the Paperclip WSL distro running so the systemd user service paperclip.service stays up.

WSL stops a distro when no session is attached, which took the Paperclip server down with it.
This launcher holds one windowless wsl.exe session open (CREATE_NO_WINDOW, no console to close).
Run at logon by the 'Paperclip WSL Keepalive' task via pythonw.
"""
import subprocess

import pacer_config

CREATE_NO_WINDOW = 0x08000000
proc = subprocess.Popen(["wsl.exe", "-d", pacer_config.require_wsl(pacer_config.get()), "--", "sleep", "infinity"],
                        creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
proc.wait()
