"""Paperclip update (started by the Pacer dashboard: python updater.py <version> [--dry-run]).
Paperclip runs from the WSL nvm install, where `paperclipai update` cannot work, so this does the manual procedure:
agents idle -> DB backup -> stop server -> npm install -g -> start server -> wait for the new version.
Progress goes to update-run.json (read by the dashboard), details to update-paperclip.log.
watchdog.lock is held (and touched) for the whole run so the tray's watchdog does not restart the stopped server."""
import os, re, subprocess, sys, threading, time

import fsutil, http_util, logutil
import pacer_config

DIR = pacer_config.DIR
LOGGER = logutil.get_logger("updater", "update-paperclip.log")
RUN = os.path.join(DIR, "update-run.json")
LOCK = os.path.join(DIR, "watchdog.lock")  # shared with the watchdog: while held, the watchdog does not restart the server
WATCHDOG_LOCK_MAX_AGE_S = 10 * 60
NO_WINDOW = 0x08000000


class UpdateError(Exception):
    pass


class Updater:
    def __init__(self, target, dry=False):
        self.cfg = pacer_config.get()
        self.target, self.dry = target, dry
        self.run = {"state": "running", "step": "starting", "target": target, "from": None, "error": None, "at": 0}
        self.run_lock = threading.Lock()
        # bash prelude: P = the paperclipai package dir of the detected install, its node on PATH
        self.prelude = f"P='{self.cfg['wslPkg']}'; export PATH=\"{self.cfg['wslNodeBin']}:$PATH\"; "

    def log(self, msg):
        LOGGER.info(msg)

    def status(self, **patch):
        with self.run_lock:
            self.run.update(patch, at=time.time())
            fsutil.write_json_atomic(RUN, self.run)
        if patch.get("step"):
            self.log(f"step: {patch['step']}")

    def wsl(self, cmd, timeout):
        """(exit code, stdout+stderr); code -1 when the command could not run or timed out."""
        try:
            r = subprocess.run(pacer_config.wsl_cmd(self.cfg, self.prelude + cmd), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               creationflags=NO_WINDOW)
            return r.returncode, r.stdout.strip()
        except subprocess.TimeoutExpired as e:
            return -1, ((e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")).strip() + "\n[timeout]"
        except (OSError, RuntimeError) as e:
            return -1, str(e)

    def health(self):
        try:
            return http_util.request_json(self.cfg["url"] + "/api/health", timeout=8)
        except http_util.HttpError:
            return None

    def busy_agents(self):
        def get(p):
            return http_util.request_json(self.cfg["url"] + p, timeout=15)
        return [a["name"] for c in get("/api/companies") for a in get(f"/api/companies/{c['id']}/agents") if a.get("status") == "running"]

    def unlock(self):
        fsutil.pid_lock_release(LOCK)

    def installed_version(self):
        code, out = self.wsl('grep -m1 \'"version"\' "$P/package.json"', 45)
        m = re.search(r'"version"\s*:\s*"([^"]+)"', out)
        return m.group(1) if m else None, out

    def update(self):
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.target or ""):
            raise UpdateError(f"bad version argument: {self.target}")
        self.status(step="checking installed version")
        current, out = self.installed_version()
        if not current:
            raise UpdateError(f"installed version not found: {out}")
        self.run["from"] = current
        self.log(f"installed {current}, target {self.target}{' (dry run)' if self.dry else ''}")
        if current == self.target:
            raise UpdateError(f"{self.target} is already installed")

        self.status(step="checking agents")
        if self.health():
            busy = self.busy_agents()
            if busy:
                raise UpdateError(f"agents still running: {', '.join(busy)}")
        else:
            self.log("server not reachable, skipping agent check")

        # the tray's watchdog must not restart the server while it is down: wait out a run in progress, then hold its lock
        for _ in range(24):
            if not fsutil.pid_lock_active(LOCK, WATCHDOG_LOCK_MAX_AGE_S):
                break
            self.status(step="waiting for the watchdog")
            time.sleep(5)
        if self.dry:
            code, out = self.wsl(f'd={self.cfg["wslInstance"]}/locks/directory-merge; find "$d" -maxdepth 1 -type d -name "*.lock" | wc -l; paperclipai --version', 60)
            self.log(f"dry run ok; legacy lock dirs / cli version: {out.replace(chr(10), ' / ')}")
            self.status(state="done", step="dry run ok")
            return
        fsutil.pid_lock_write(LOCK)
        stop_beat = threading.Event()

        def beat():
            while not stop_beat.wait(30):
                try:
                    os.utime(LOCK)
                    self.status()
                except OSError:
                    pass
        threading.Thread(target=beat, daemon=True).start()

        try:
            self.status(step="backing up database")
            code, out = self.wsl("paperclipai db:backup --json", 300)
            if code != 0:
                raise UpdateError(f"db:backup failed: {out[-200:]}")
            self.log(f"backup: {out[-300:]}")

            # legacy <hash>.lock/ directories (never the *.lock.sqlite files) block the new version: move them aside
            code, out = self.wsl(f'd={self.cfg["wslInstance"]}/locks/directory-merge; n=$(find "$d" -maxdepth 1 -type d -name "*.lock" | wc -l); '
                                 'if [ "$n" -gt 0 ]; then mkdir -p "$d.legacy-lock-dirs" && find "$d" -maxdepth 1 -type d -name "*.lock" '
                                 '-exec mv {} "$d.legacy-lock-dirs/" \\; ; fi; echo moved $n', 60)
            self.log(f"legacy lock dirs: {out}")

            self.status(step="stopping server")
            code, out = self.wsl(pacer_config.stop_server_cmd(self.cfg) +
                                 'pkill -TERM -f "[p]aperclipai run"; for i in $(seq 1 60); do if ! pgrep -f "[p]aperclipai run" >/dev/null && '
                                 '! pgrep -f "[p]ostgres -D" >/dev/null; then echo stopped; exit 0; fi; sleep 2; done; '
                                 'pkill -9 -f "[p]aperclipai run"; pkill -INT -f "[p]ostgres -D"; sleep 4; pkill -9 -f "[p]ostgres -D"; echo forced', 200)
            self.log(f"stop: {out}")

            self.status(step=f"installing {self.target}")
            code, out = self.wsl(f"cd /tmp && npm install -g paperclipai@{self.target} "
                                 "--allow-scripts=@embedded-postgres/linux-x64,ssh2,protobufjs,@paperclipai/server 2>&1 | tail -15", 1200)
            self.log(f"npm install: {out}")
            after, _ = self.installed_version()
            if after != self.target:
                raise UpdateError(f"npm install did not leave {self.target} installed: {out[-200:]}")
        except Exception:
            # never leave the server down: bring back whatever is installed, then report the failure
            self.status(step="restarting server after failure")
            pacer_config.start_server(self.cfg)
            raise
        finally:
            stop_beat.set()

        try:
            self.status(step="starting server")
            if not pacer_config.start_server(self.cfg):
                raise UpdateError(f"cannot start the server: serverControl={self.cfg.get('serverControl')}")
            h = None
            for _ in range(60):
                time.sleep(5)
                h = self.health()
                if h and h.get("version") == self.target:
                    break
            if not h:
                raise UpdateError("server did not come back within 5 min")
            if h.get("version") != self.target:
                raise UpdateError(f"server reports {h.get('version')}, expected {self.target}")
            self.status(state="done", step=f"updated to {self.target}")
        finally:
            self.unlock()


def run(target, dry=False):
    u = Updater(target, dry)
    try:
        u.update()
        return True
    except Exception as e:
        u.log(f"FAILED: {e}")
        u.unlock()
        u.status(state="failed", error=str(e)[:300])
        return False


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(0 if run(args[0] if args else None, "--dry-run" in sys.argv) else 1)
