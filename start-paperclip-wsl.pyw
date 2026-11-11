"""Starts the Paperclip server inside WSL, detached, with no console window.

Run by the 'Paperclip Server (WSL)' logon task via pythonw. The server runs as its own WSL
process (setsid -f), so this launcher can exit and closing nothing can stop the server.
start-paperclip-wsl.sh exits immediately when the server is already healthy.
"""
import subprocess

import pacer_config

CFG = pacer_config.get()
CREATE_NO_WINDOW = 0x08000000
subprocess.run(
    pacer_config.wsl_cmd(CFG, f"setsid -f bash {CFG['wslHome']}/.paperclip/start-paperclip-wsl.sh </dev/null >/dev/null 2>&1", login=True),
    creationflags=CREATE_NO_WINDOW, timeout=120, check=False)
