"""Environment-specific values of the Pacer: detected automatically, overridable by the user.

Order (later wins): defaults < detection < pacer-config.json (user overrides, only the keys you want to change).
Detection costs a few WSL calls, so the result is cached in pacer-detected.json (re-detected after 6 h, when
pacer-config.json changed, or with `python pacer_config.py --detect`). The only JavaScript left runs inside WSL
(watchdog-inner.cjs) and gets its values through wsl_env().

Overridable keys of pacer-config.json (all optional):
  url, claudeExe, wslDistro, instance, db {host,port,user,password,database},
  serverControl ("systemd" | "task" | "none"), serverTask, repairTask, systemdUnit
Resolved keys: see detect(). wslDistro is None when no WSL distro with Paperclip was found; WSL features
(governor credential sync, watchdog, update, run-log tokens, copilot monitor) must then stay inactive.
"""
import json, os, shlex, shutil, subprocess, sys, time

import fsutil

FROZEN = getattr(sys, "frozen", False)  # running as the packaged Pacer.exe
CODE_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def _data_dir():
    """Where settings, state, logs and caches live: PACER_HOME, else an existing ~/.paperclip of a former install
    (the Pacer started as a Paperclip add-on there), else %LOCALAPPDATA%\\Pacer."""
    if os.environ.get("PACER_HOME"):
        return os.environ["PACER_HOME"]
    home = os.path.expanduser("~")
    legacy = os.path.join(home, ".paperclip")
    if any(os.path.exists(os.path.join(legacy, f)) for f in ("claude-governor-settings.json", "claude-governor-state.json", "pacer-config.json")):
        return legacy
    return os.path.join(os.environ.get("LOCALAPPDATA") or home, "Pacer")


DIR = _data_dir()
os.makedirs(DIR, exist_ok=True)
OVERRIDES = os.path.join(DIR, "pacer-config.json")
DETECTED = os.path.join(DIR, "pacer-detected.json")
CACHE_S = 6 * 3600
NO_WINDOW = 0x08000000

DEFAULTS = {
    "instance": "default",
    "serverTask": "Paperclip Server (WSL)",
    "repairTask": "Paperclip WSL Repair",
    "systemdUnit": "paperclip.service",
}
DEFAULT_DB = {"host": "127.0.0.1", "user": "paperclip", "password": "paperclip", "database": "paperclip"}
DEFAULT_PORT = 3100
DEFAULT_PG_PORT = 54329

# runs inside WSL (bash -lc): home, paperclipai package dir (nvm first, then npm's global root), unit, instance config
_PROBE = r'''
echo "home=$HOME"
P=$(ls -d ~/.nvm/versions/node/*/lib/node_modules/paperclipai 2>/dev/null | tail -1)
[ -z "$P" ] && P="$(npm root -g 2>/dev/null)/paperclipai" && [ ! -d "$P" ] && P=
echo "pkg=$P"
systemctl --user cat __UNIT__ >/dev/null 2>&1 && echo "systemd=1"
C="$HOME/.paperclip/instances/__INSTANCE__/config.json"
[ -f "$C" ] && echo "config=$(tr -d '\n\r' < "$C")"
'''


def _run(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def read_overrides():
    return _read_json(OVERRIDES)


def _distros():
    r = _run(["wsl.exe", "-l", "-q"], 20)
    if not r or r.returncode != 0:
        return []
    names = [n.strip() for n in r.stdout.decode("utf-16-le", "ignore").replace("\x00", "").splitlines()]
    return [n for n in names if n and not n.startswith("docker-desktop")]


def _probe(distro, instance, unit):
    script = _PROBE.replace("__UNIT__", unit).replace("__INSTANCE__", instance)
    r = _run(["wsl.exe", "-d", distro, "-e", "bash", "-lc", script], 45)
    if not r or r.returncode != 0:
        return None
    out = {}
    for line in r.stdout.decode("utf-8", "ignore").splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def _task_exists(name):
    r = _run(["schtasks.exe", "/query", "/tn", name], 20)
    return bool(r and r.returncode == 0)


def to_wsl_path(win_path):
    """C:\\Users\\x\\.paperclip -> /mnt/c/Users/x/.paperclip"""
    p = os.path.abspath(win_path)
    return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")


def detect(overrides=None):
    ov = read_overrides() if overrides is None else overrides
    instance = ov.get("instance") or DEFAULTS["instance"]
    unit = ov.get("systemdUnit") or DEFAULTS["systemdUnit"]
    cfg = {**DEFAULTS, "dir": DIR, "dirWsl": to_wsl_path(DIR), "wslDistro": None, "wslHome": None, "wslPkg": None,
           "wslNodeBin": None, "wslInstance": None, "runLogs": None}

    # --- WSL / Paperclip install: first distro that has the paperclipai package (or the one the user named)
    distros = [ov["wslDistro"]] if ov.get("wslDistro") else _distros()
    probe = None
    for d in distros:
        p = _probe(d, instance, unit)
        if p and p.get("pkg"):
            cfg["wslDistro"], probe = d, p
            break
    server_json = {}
    if probe:
        cfg["wslHome"] = probe["home"]
        cfg["wslPkg"] = probe["pkg"]
        cfg["wslNodeBin"] = probe["pkg"].rsplit("/lib/node_modules/", 1)[0] + "/bin"
        cfg["wslInstance"] = f"{probe['home']}/.paperclip/instances/{instance}"
        cfg["runLogs"] = "\\\\wsl.localhost\\" + cfg["wslDistro"] + cfg["wslInstance"].replace("/", "\\") + "\\data\\run-logs"
        try:
            server_json = json.loads(probe.get("config", "{}"))
        except ValueError:
            pass

    # --- server URL and database
    srv = server_json.get("server", {})
    host = srv.get("host") or "127.0.0.1"
    cfg["url"] = ov.get("url") or os.environ.get("PAPERCLIP_URL") or f"http://{host}:{srv.get('port') or DEFAULT_PORT}"
    pg_port = server_json.get("database", {}).get("embeddedPostgresPort") or DEFAULT_PG_PORT
    cfg["db"] = {**DEFAULT_DB, "port": pg_port, **ov.get("db", {})}

    # --- how the server is started/stopped: systemd user unit in WSL, else a scheduled task, else not at all
    for k in ("serverTask", "repairTask"):
        cfg[k] = ov.get(k) or DEFAULTS[k]
    cfg["systemdUnit"] = unit
    if ov.get("serverControl"):
        cfg["serverControl"] = ov["serverControl"]
    elif probe and probe.get("systemd") == "1":
        cfg["serverControl"] = "systemd"
    elif _task_exists(cfg["serverTask"]):
        cfg["serverControl"] = "task"
    else:
        cfg["serverControl"] = "none"

    # --- Windows side executables
    local_claude = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")
    cfg["claudeExe"] = ov.get("claudeExe") or (local_claude if os.path.exists(local_claude) else shutil.which("claude") or "claude.exe")
    cfg["detectedAt"] = time.time()
    cfg["overridesMtime"] = _mtime(OVERRIDES)
    return cfg


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _fresh(cfg):
    return (cfg.get("detectedAt") and time.time() - cfg["detectedAt"] < CACHE_S
            and cfg.get("overridesMtime") == _mtime(OVERRIDES))


def write_detected(cfg):
    fsutil.write_json_atomic(DETECTED, cfg, indent=2)


def get(force=False):
    """Resolved config (cached)."""
    cfg = _read_json(DETECTED)
    if force or not _fresh(cfg):
        new = detect()
        if not new["wslDistro"] and cfg.get("wslDistro") and not read_overrides().get("wslDistro"):
            # WSL not answering right now (the watchdog runs exactly then): keep the last good values, retry in 10 min
            new = {**cfg, "detectedAt": time.time() - CACHE_S + 600, "overridesMtime": _mtime(OVERRIDES)}
        cfg = new
        write_detected(cfg)
    return cfg


def require_wsl(cfg):
    if not cfg.get("wslDistro"):
        raise RuntimeError("no WSL distro with Paperclip found (set wslDistro in pacer-config.json)")
    return cfg["wslDistro"]


def wsl_cmd(cfg, shell_cmd, login=False):
    """argv for running a bash command in the Paperclip distro."""
    return ["wsl.exe", "-d", require_wsl(cfg), "-e", "bash", "-lc" if login else "-c", shell_cmd]


def wsl_env(cfg):
    """`PACER_ENV='<json>' ` prefix so WSL-side node scripts get url/db/paths without any hardcoding."""
    payload = {"url": cfg["url"], "db": cfg["db"], "pkg": cfg["wslPkg"], "instance": cfg["wslInstance"]}
    return "PACER_ENV=" + shlex.quote(json.dumps(payload)) + " "


WSL_SIDE_FILES = ("watchdog-inner.cjs", "repair-wsl.ps1")  # run inside WSL / by a scheduled task


def ensure_resources():
    """The packaged app carries the WSL-side scripts inside the exe; WSL reads them from the data dir (/mnt/c/...)."""
    for name in WSL_SIDE_FILES:
        src, dst = os.path.join(CODE_DIR, name), os.path.join(DIR, name)
        try:
            if os.path.exists(src) and (not os.path.exists(dst) or not os.path.samefile(src, dst)) and (
                    not os.path.exists(dst) or open(src, "rb").read() != open(dst, "rb").read()):
                with open(src, "rb") as f:
                    fsutil.write_atomic(dst, f.read())  # WSL may be reading the script right now: never a half copy
        except OSError:
            pass


def self_cmd(*args):
    """argv that runs a Pacer subcommand in a new process: the exe itself when packaged, else pacer.py with the same Python."""
    if FROZEN:
        return [sys.executable, *args]
    exe = sys.executable
    pyw = exe[:-10] + "pythonw.exe" if exe.lower().endswith("python.exe") else exe
    return [pyw, os.path.join(CODE_DIR, "pacer.py"), *args]


def mutex_name(base):
    """Single-instance mutex; a separate PACER_HOME (tests, a second profile) gets its own so it never blocks the real one."""
    home = os.environ.get("PACER_HOME")
    return "Global\\" + base + ("" if not home else "_" + __import__("hashlib").sha1(home.encode()).hexdigest()[:8])


class WslResult:
    """Outcome of a command run in WSL: code (None when it could not run or timed out), stdout, stderr, error text."""
    def __init__(self, code=None, out="", err="", error=None):
        self.code, self.out, self.err, self.error = code, out, err, error

    @property
    def ok(self):
        return self.code == 0


def wsl_run(cfg, shell_cmd, timeout=60, input_text=None, login=False):
    """Run a bash command in the Paperclip distro without a window; never raises."""
    try:
        r = subprocess.run(wsl_cmd(cfg, shell_cmd, login), input=input_text, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, creationflags=NO_WINDOW)
        return WslResult(r.returncode, r.stdout, r.stderr)
    except subprocess.TimeoutExpired:
        return WslResult(error="timeout")
    except (OSError, RuntimeError) as e:
        return WslResult(error=f"{type(e).__name__}: {e}")


def start_server(cfg):
    """Starts the Paperclip server the way cfg['serverControl'] says: systemd user unit in WSL | scheduled task | not at all."""
    if cfg.get("serverControl") == "systemd":
        return wsl_run(cfg, f"systemctl --user start {cfg['systemdUnit']}", 60).ok
    if cfg.get("serverControl") == "task":
        r = _run(["schtasks.exe", "/run", "/tn", cfg["serverTask"]], 60)
        return bool(r and r.returncode == 0)
    return False


def stop_server_cmd(cfg):
    """bash fragment that stops the server so nothing restarts it behind our back (systemd has Restart=always)."""
    return f"systemctl --user stop {cfg['systemdUnit']}; " if cfg.get("serverControl") == "systemd" else ""


if __name__ == "__main__":
    c = get(force="--detect" in sys.argv or "--write" in sys.argv)
    if "--write" not in sys.argv:
        shown = {k: v for k, v in c.items() if k not in ("db",)}
        shown["db"] = {**c["db"], "password": "***"}
        print(json.dumps(shown, indent=2))
