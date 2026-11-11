"""Pacer watchdog for Paperclip (run by the tray every 5 min). Recovers: hung server (WSL netlink deadlock), hung pi runs,
stale environment leases. The checks of the runs themselves live in watchdog-inner.cjs: that script runs inside WSL next to
Paperclip's own node and database driver, so it stays JavaScript.
`python watchdog.py --restart` is the manual server restart of the dashboard (skips lock, health and hung-run checks)."""
import json, os, subprocess, sys, time

import fsutil, http_util, logutil
import pacer_config

DIR = pacer_config.DIR
LOGGER = logutil.get_logger("watchdog", "watchdog.log")
LOCK = os.path.join(DIR, "watchdog.lock")
LOCK_MAX_AGE_S = 10 * 60  # the updater refreshes its lock every 30 s; a live holder older than this is hung
REPAIR_DONE = os.path.join(DIR, "repair-wsl.done")
NO_WINDOW = 0x08000000


class Watchdog:
    def __init__(self):
        self.cfg = pacer_config.get()
        self.failed = False  # the tray shows the watchdog job as failed

    def log(self, msg):
        LOGGER.info(msg)

    def wsl(self, cmd, timeout):
        return pacer_config.wsl_run(self.cfg, cmd, timeout)

    def healthy(self):
        try:
            http_util.request_text(self.cfg["url"] + "/api/health", timeout=10)
            return True
        except http_util.HttpError:
            return False

    def elevated_repair(self):
        """Escalation when `wsl --shutdown` hangs or WSL stays dead: elevated on-demand task (see repair-wsl.ps1)."""
        try:
            os.unlink(REPAIR_DONE)
        except OSError:
            pass
        r = subprocess.run(["schtasks.exe", "/run", "/tn", self.cfg["repairTask"]], capture_output=True, text=True,
                           creationflags=NO_WINDOW)
        if r.returncode != 0:
            self.log(f"elevated repair task not runnable: {(r.stdout + r.stderr).strip()}")
            return False
        self.log("elevated WSL repair started")
        for _ in range(24):
            time.sleep(5)
            if os.path.exists(REPAIR_DONE):
                time.sleep(5)
                return True
        self.log("elevated WSL repair did not finish in 120s")
        return False

    def restart(self, reason):
        self.log(f"RESTART: {reason}")
        k = self.wsl('pkill -9 -f "[p]aperclipai run"; sleep 1; pkill -INT -f "[p]ostgres -D"; sleep 4; '
                     'pkill -9 -f "[p]ostgres -D"; echo done', 30)
        if k.error or k.code != 0:
            self.log(f"wsl unresponsive ({k.error or k.code}) -> wsl --shutdown")
            try:
                sd = subprocess.run(["wsl.exe", "--shutdown"], timeout=60, creationflags=NO_WINDOW).returncode
            except (OSError, subprocess.TimeoutExpired):
                sd = None
            time.sleep(8)
            probe = self.wsl("echo ok", 20)
            if sd != 0 or not probe.ok:
                self.log("wsl still unresponsive after shutdown -> elevated repair")
                self.elevated_repair()
        if not pacer_config.start_server(self.cfg):
            self.log(f"server not started: serverControl={self.cfg.get('serverControl')}")
        for _ in range(24):
            time.sleep(5)
            if self.healthy():
                self.log("server healthy after restart")
                return True
        self.log("server NOT healthy after restart")
        self.failed = True
        return False

    def check(self):
        ok = False
        for _ in range(3):
            ok = self.healthy()
            if ok:
                break
            time.sleep(10)
        if not ok:
            self.restart("health check failed 3x")
            return
        t = self.wsl("echo ok", 30)
        if not t.ok:
            self.restart("wsl unresponsive")
            return
        r = self.wsl(f"{pacer_config.wsl_env(self.cfg)}timeout 120 {self.cfg['wslNodeBin']}/node {self.cfg['dirWsl']}/watchdog-inner.cjs", 150)
        if not r.ok:
            self.log(f"inner failed: {r.error or r.out + r.err}")
            return
        out = json.loads(r.out.strip().split("\n")[-1])
        if out.get("hung") or out.get("leases"):
            self.log(f"inner: {json.dumps(out, separators=(',', ':'))}")
        if out.get("cancelFailed"):
            self.restart("cancel of hung run failed")


def run(restart=False):
    """One watchdog pass. Returns False only when a restart did not bring the server back (the tray shows that as FAILED)."""
    w = Watchdog()
    mine = False  # only a lock this run took may be released (a run that finds a live lock must leave it alone)
    try:
        if restart:  # manual restart from the dashboard: skip lock/health/hung checks
            fsutil.pid_lock_write(LOCK)
            mine = True
            w.restart("manual restart from dashboard")
            return not w.failed
        if not fsutil.pid_lock_take(LOCK, LOCK_MAX_AGE_S):
            return True  # previous run still active
        mine = True
        w.check()
    except Exception as e:
        w.log(f"error: {e!r}")
    finally:
        if mine:
            fsutil.pid_lock_release(LOCK)
    return not w.failed


if __name__ == "__main__":
    sys.exit(0 if run("--restart" in sys.argv) else 1)
