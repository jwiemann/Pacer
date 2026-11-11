"""GitHub Copilot usage monitor: run by the tray every 5 min (also standalone: python copilot_monitor.py).

Reads the Copilot OAuth token that `opencode auth login -p github-copilot` stored in WSL (the one Paperclip's
opencode_local agents use), asks GitHub for the premium quota (the same endpoint the editors use) and writes
  copilot-status.json   latest reading + login state (read by dashboard.py)
  copilot-history.jsonl one {"t": epoch, "used": percent} line per reading (chart history, trimmed to ~45 days)
Exit code 0 even when signed out: that is a state to show, not a failure of this job.
"""
import json, os, subprocess, sys, time
from datetime import datetime, timedelta

import fsutil, http_util
import pacer_config

DIR = pacer_config.DIR
STATUS = os.path.join(DIR, "copilot-status.json")
SETTINGS = os.path.join(DIR, "claude-governor-settings.json")
HISTORY = os.path.join(DIR, "copilot-history.jsonl")
KEEP_S = 45 * 86400
AUTH_IN_WSL = "~/.local/share/opencode/auth.json"


def enabled():
    """False when the user hid Copilot in the dashboard (settings key copilot: false); then nothing is read or sent."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f).get("copilot") is not False
    except (OSError, ValueError):
        return True


def read_status():
    """Latest reading ({} if never run); `resets` normalised to an ISO string fromisoformat() accepts."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            cs = json.load(f)
    except (OSError, ValueError):
        return {}
    if cs.get("resets"):
        cs["resets"] = cs["resets"].replace("Z", "+00:00")
        if "T" not in cs["resets"]:
            cs["resets"] += "T00:00:00+00:00"
    return cs


def period(reset_iso):
    """(start, end) epochs of the monthly quota period that ends at `reset_iso`."""
    end = datetime.fromisoformat(reset_iso)
    try:
        start = end.replace(year=end.year - (end.month == 1), month=end.month - 1 or 12)
    except ValueError:  # 29th-31st do not exist in the previous month
        start = end - timedelta(days=30)
    return start.timestamp(), end.timestamp()


def editor_token():
    """OAuth token of the Copilot login that VS Code / JetBrains / Neovim store on Windows, or None."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    for path in (os.path.join(base, "github-copilot", n) for n in ("apps.json", "hosts.json")):
        try:
            with open(path, encoding="utf-8") as f:
                for v in json.load(f).values():
                    if isinstance(v, dict) and v.get("oauth_token"):
                        return v["oauth_token"]
        except (OSError, ValueError, AttributeError):
            continue
    return None


def read_token():
    """(token, None) or (None, (state, message)): 'nologin' = no Copilot credential anywhere, 'error' = WSL unreachable.
    Sources in order: opencode in WSL (what Paperclip's Copilot agents use), then an editor login on Windows."""
    problem = None
    try:
        r = subprocess.run(pacer_config.wsl_cmd(pacer_config.get(), f"cat {AUTH_IN_WSL}"), capture_output=True,
                           timeout=60, creationflags=0x08000000)
        if r.returncode == 0:
            try:
                cred = json.loads(r.stdout.decode("utf-8", "replace"))["github-copilot"]
                return cred.get("refresh") or cred["access"], None
            except (ValueError, KeyError, TypeError):
                pass
    except RuntimeError:
        pass  # no WSL distro: editor login only
    except (OSError, subprocess.TimeoutExpired) as e:
        problem = ("error", f"WSL not reachable: {type(e).__name__}")
    token = editor_token()
    if token:
        return token, None
    return None, (problem or ("nologin", "no GitHub Copilot login found (opencode in WSL, VS Code)"))


def fetch(token):
    return http_util.request_json("https://api.github.com/copilot_internal/user", headers={
        "Authorization": "token " + token, "User-Agent": "GitHubCopilotChat/0.26.7", "Accept": "application/json"}, timeout=30)


def write_status(st):
    if st["state"] == "error":  # transient failure: keep the last numbers so the dashboard still shows them
        try:
            with open(STATUS, encoding="utf-8") as f:
                old = json.load(f)
            st = {**{k: old[k] for k in ("login", "plan", "unlimited", "entitlement", "remaining", "used", "resets") if k in old}, **st}
        except (OSError, ValueError):
            pass

    st["at"] = time.time()
    fsutil.write_json_atomic(STATUS, st)  # atomic: the dashboard must never read a half-written file


def append_history(used):
    now = time.time()
    rows = []
    try:
        if os.path.getsize(HISTORY) > 400_000:  # trim only occasionally
            with open(HISTORY, encoding="utf-8") as f:
                rows = [ln for ln in f if json.loads(ln)["t"] >= now - KEEP_S]
            fsutil.write_atomic(HISTORY, "".join(rows))
    except (OSError, ValueError):
        pass
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(json.dumps({"t": now, "used": used}) + "\n")


def main():
    if not enabled():
        return
    token, problem = read_token()
    if problem:
        state, msg = problem
        write_status({"state": state, "error": msg})
        return
    try:
        d = fetch(token)
    except http_util.HttpError as e:
        if e.status in (401, 403):
            write_status({"state": "signin", "error": f"GitHub rejected the token (HTTP {e.status})"})
        elif e.status is not None:
            write_status({"state": "error", "error": f"GitHub API HTTP {e.status}"})
        else:
            write_status({"state": "error", "error": str(e)[:150]})
        return
    q = (d.get("quota_snapshots") or {}).get("premium_interactions") or {}
    ent = q.get("entitlement")
    if q.get("unlimited") or not ent:
        write_status({"state": "ok", "login": d.get("login"), "plan": d.get("copilot_plan"), "unlimited": True})
        return
    remaining = q.get("quota_remaining", q.get("remaining", ent))
    used = round(max(0.0, ent - remaining) / ent * 100, 3)
    write_status({"state": "ok", "login": d.get("login"), "plan": d.get("copilot_plan"), "unlimited": False,
                  "entitlement": ent, "remaining": remaining, "used": used,
                  "resets": d.get("quota_reset_date_utc") or d.get("quota_reset_date")})
    append_history(used)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # leave a trace for the tray job (non-zero exit = FAILED)
        print(e, file=sys.stderr)
        sys.exit(1)
