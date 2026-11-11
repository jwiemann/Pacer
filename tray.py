"""Pacer tray: runs the Claude governor (3 min), the Paperclip watchdog (5 min) and the Copilot monitor with no visible
windows and shows their status as a tray icon (governor.py, watchdog.py and copilot_monitor.py run inside this process).

Icon (see tray_icon.py): ring = usage of the limiting window, white notch = where all agents get paused
(top = 100% with Full budget), blue halo = Full budget on, centre pie = cooldown to the window reset,
colour green/yellow/orange(paused)/red(fault). Double-click = open the dashboard (dashboard.py: gauges, settings, status).
"""
import ctypes, subprocess, threading, time, traceback, os
import tray_icon
import pystray
from paperclip_common import (DIR, URL, TRAY_STATE, CHECK_FLAG, evaluate, keep_awake, sleep_when_idle, paperclip_busy, paperclip_enabled,
                              read_settings, migrate_settings)
import copilot_monitor, governor, openrouter_monitor, watchdog
import i18n
import fsutil, http_util, logutil
import pacer_config

LOGGER = logutil.get_logger("tray", "tray-ui.log")

jobs = {}      # name -> {"ok": bool|None, "at": epoch, "wake": Event}
server_ok = [None]
icon = None


def run_job(name, fn, interval, delay):
    """Runs fn() every `interval` s in this thread (the governor, watchdog and Copilot monitor are plain Python modules).
    fn returns False when the job failed; an exception counts as failed too and is written to tray-ui.log."""
    job = jobs[name] = {"ok": None, "at": 0, "wake": threading.Event()}
    job["wake"].wait(delay)
    while True:
        job["wake"].clear()
        if name == "watchdog" and not paperclip_enabled():  # nothing to watch without Paperclip
            job["ok"], job["at"] = None, 0
            job["wake"].wait(interval)
            continue
        try:
            job["ok"] = fn() is not False
        except Exception:
            job["ok"] = False
            LOGGER.error(f"job {name}\n{traceback.format_exc().rstrip()}")
        job["at"] = time.time()
        update()
        job["wake"].wait(interval)


def poll_health():
    last_error = None  # a failure is written to the log once, when it starts or its message changes (not every 30 s)
    while True:
        if not paperclip_enabled():
            server_ok[0], last_error = None, None
        else:
            try:
                http_util.request_text(URL + "/api/health", timeout=10)
                server_ok[0], last_error = True, None
            except Exception as e:
                server_ok[0] = False
                error = f"health check failed: {e}"
                if error != last_error:
                    LOGGER.error(error)
                    last_error = error
        update()
        time.sleep(30)


def make_icon(model):
    return tray_icon.make_icon(model, 32)


def write_state():
    """Job results + server health for the dashboard (the only data it cannot get from the governor files)."""
    st = {"server_ok": server_ok[0], "at": time.time(),
          "jobs": {n: {"ok": j.get("ok"), "at": j.get("at")} for n, j in jobs.items()}}
    fsutil.write_json_atomic(TRAY_STATE, st)


def update():
    if icon is None:
        return
    model, tip = evaluate(jobs.get("governor", {}).get("ok"), jobs.get("watchdog", {}).get("ok"), server_ok[0])
    icon.icon = make_icon(model)
    i18n.set_lang(i18n.lang_from(read_settings()))  # the dashboard menu switches the language in the settings file
    icon.title = i18n.tr(tip)[:127]
    icon.update_menu()
    try:
        write_state()
    except OSError:
        pass


def check_now(*_):
    for name in ("governor", "copilot", "openrouter"):  # governor and the Copilot / OpenRouter monitors
        if name in jobs:
            jobs[name]["wake"].set()


def open_dashboard(*_):
    """The one UI: a separate process (Tk must own its main thread, see dashboard.py). A second launch
    just raises the already open window and exits."""
    subprocess.Popen(pacer_config.self_cmd("dashboard"), cwd=DIR)


IDLE_GRACE_S = 600  # with "sleep when idle": the PC may sleep this long after Paperclip last did something
BUSY_POLL_S = 30


def hold_awake():
    """Keep-awake checkbox: while 'keepAwake' is set in the settings file, hold a system wake lock so Windows
    never goes to sleep (Paperclip keeps running). With 'sleepWhenIdle' the lock is held only while Paperclip is
    busy (an agent runs or an update is in progress) and for IDLE_GRACE_S afterwards. ES_DISPLAY_REQUIRED is NOT
    set, so the screen may turn off and the session may lock. SetThreadExecutionState is per thread, so set and
    clear it from this one thread; Windows also drops it when the tray exits."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    held = False
    last_busy, last_poll = time.time(), 0.0
    while True:
        want = keep_awake()
        if want and sleep_when_idle():
            now = time.time()
            if now - last_poll >= BUSY_POLL_S:
                last_poll = now
                if paperclip_busy():
                    last_busy = now
            want = now - last_busy < IDLE_GRACE_S
        else:
            last_busy = time.time()  # switching to idle-sleep starts with a full grace period
        if want != held:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))
            held = want
        time.sleep(2)


def watch_check_flag():
    """The dashboard asks for an immediate governor run (after changing settings) by creating CHECK_FLAG."""
    while True:
        if os.path.exists(CHECK_FLAG):
            try:
                os.remove(CHECK_FLAG)
            except OSError:
                pass
            check_now()
        time.sleep(1)


def quit_app(i, *_):
    i.stop()


def main():
    global icon
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, pacer_config.mutex_name("PaperclipPacerTray"))
    if ctypes.windll.kernel32.GetLastError() == 183:
        return  # already running
    pacer_config.ensure_resources()
    migrate_settings()
    menu = pystray.Menu(
        pystray.MenuItem(lambda _i: i18n.tr("Dashboard"), open_dashboard, default=True),
        pystray.MenuItem(lambda _i: i18n.tr("Quit"), quit_app),
    )

    icon = pystray.Icon("pacer", make_icon({"pct": None, "threshold": 100}), "Pacer: starting", menu)

    # pystray fires the default item on single left click; make it double-click instead
    from pystray._util import win32 as _w32  # noqa: E402
    _orig_notify = icon._message_handlers[_w32.WM_NOTIFY]


    def _on_notify(wparam, lparam):
        if lparam == 0x0203:  # WM_LBUTTONDBLCLK
            open_dashboard()
        elif lparam != _w32.WM_LBUTTONUP:
            _orig_notify(wparam, lparam)


    icon._message_handlers[_w32.WM_NOTIFY] = _on_notify
    for args in (("governor", governor.run, 180, 0), ("watchdog", watchdog.run, 300, 60), ("copilot", copilot_monitor.main, 300, 5),
                 ("openrouter", openrouter_monitor.main, 300, 10)):
        threading.Thread(target=run_job, args=args, daemon=True).start()
    threading.Thread(target=poll_health, daemon=True).start()
    threading.Thread(target=hold_awake, daemon=True).start()
    threading.Thread(target=watch_check_flag, daemon=True).start()
    icon.run(setup=lambda i: (setattr(i, "visible", True), update()))


if __name__ == "__main__":
    main()
