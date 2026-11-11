"""Pacer entry point. One program, several roles (the packaged Pacer.exe and `python pacer.py` behave the same):

  pacer [launch]          start the tray and open the dashboard (what the Start menu shortcut does)
  pacer tray              tray icon + background jobs (governor, watchdog, Copilot monitor); starts with Windows
  pacer dashboard         the window
  pacer restart           stop every running Pacer tray / dashboard, then launch again
  pacer governor [--dry-run] [--fake=5h,7d]   one governor tick
  pacer watchdog [--restart]                  one Paperclip watchdog pass / manual server restart
  pacer update <version> [--dry-run]          Paperclip update
  pacer copilot           one Copilot usage reading
  pacer openrouter        one OpenRouter key-limit reading
  pacer detect            re-detect the environment and print it
"""
import os, subprocess, sys

DETACHED = 0x08000000 | 0x00000008  # CREATE_NO_WINDOW | DETACHED_PROCESS


def launch():
    import pacer_config
    for role in ("tray", "dashboard"):  # both are single-instance: a second start only raises the window
        subprocess.Popen(pacer_config.self_cmd(role), cwd=pacer_config.DIR, creationflags=DETACHED,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def restart():
    """Runs detached from the dashboard that asked for it: kill every other Pacer tray / dashboard, then launch again."""
    import time
    me = os.getpid()
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne %d -and $_.CommandLine -match "
          "'(Pacer\\.exe|pacer\\.py)\"?\\s+(tray|dashboard)|paperclip[\\\\/](tray|dashboard)\\.pyw?' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" % me)
    subprocess.run(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps], creationflags=0x08000000, timeout=60)
    time.sleep(0.8)
    launch()


def main(argv):
    cmd, rest = (argv[0], argv[1:]) if argv else ("launch", [])
    if cmd == "launch":
        launch()
    elif cmd == "tray":
        import tray
        tray.main()
    elif cmd == "dashboard":
        import dashboard
        dashboard.main()
    elif cmd == "restart":
        restart()
    elif cmd == "governor":
        import governor
        fake = next((a[7:] for a in rest if a.startswith("--fake=")), None)
        return 0 if governor.run("--dry-run" in rest, fake) else 1
    elif cmd == "watchdog":
        import watchdog
        return 0 if watchdog.run("--restart" in rest) else 1
    elif cmd == "update":
        import updater
        version = next((a for a in rest if not a.startswith("--")), None)
        return 0 if updater.run(version, "--dry-run" in rest) else 1
    elif cmd == "copilot":
        import copilot_monitor
        copilot_monitor.main()
    elif cmd == "openrouter":
        import openrouter_monitor
        openrouter_monitor.main()
    elif cmd == "detect":
        import json, pacer_config
        print(json.dumps({k: v for k, v in pacer_config.get(force=True).items() if k != "db"}, indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
