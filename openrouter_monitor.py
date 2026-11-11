"""OpenRouter spend monitor: run by the tray every 5 min (also standalone: python openrouter_monitor.py).

Asks OpenRouter for the spend limit of the API key (GET /api/v1/key: limit, limit_remaining, limit_reset) and writes
  openrouter-status.json   latest reading + key state (read by governor.py and dashboard.py)
  openrouter-history.jsonl one {"t": epoch, "used": percent} line per reading (chart history, trimmed to ~45 days)
The key is the limit of ONE OpenRouter API key, set in the OpenRouter dashboard. A key without a limit is shown but never
throttled; a limit that resets (daily / weekly / monthly, midnight UTC) is a pace like Copilot's month, one that never
resets is only a percentage. Key lookup: openrouter.key in the Pacer data dir, the key omp is configured with (apiKey of the
openrouter provider in ~/.omp/agent/models.yml), env OPENROUTER_API_KEY, then the Windows user environment. Exit code 0 even
without a key: that is a state to show, not a failure of this job.
"""
import json, os, re, sys, time
from datetime import datetime, timedelta, timezone

import fsutil, http_util, pacer_config

DIR = pacer_config.DIR
STATUS = os.path.join(DIR, "openrouter-status.json")
SETTINGS = os.path.join(DIR, "claude-governor-settings.json")
HISTORY = os.path.join(DIR, "openrouter-history.jsonl")
KEY_FILE = os.path.join(DIR, "openrouter.key")
ENV_VAR = "OPENROUTER_API_KEY"
URL = "https://openrouter.ai/api/v1/key"
KEEP_S = 45 * 86400
KEPT = ("label", "limit", "remaining", "used", "spent", "resets", "span", "kind", "unlimited")


def enabled():
    """False when the user hid OpenRouter in the dashboard (settings key openrouter: false); then nothing is read or sent."""
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f).get("openrouter") is not False
    except (OSError, ValueError):
        return True


def read_status():
    """Latest reading ({} if never run)."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def omp_key():
    """The OpenRouter key omp (and so the agents) call the API with: `apiKey` of the openrouter provider in omp's
    models.yml. None if the file or the entry is missing or the value is not a literal key."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".omp", "agent", "models.yml"), encoding="utf-8") as f:
            m = re.search(r"^\s*openrouter:\s*\n(?:.*\n)*?\s*apiKey:\s*['\"]?(sk-or-[\w-]+)", f.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def read_key():
    """The API key or None. The spend limit that matters is that of the key the agents spend with, so the key file in
    the data dir (explicit override) comes first, then the key omp is configured with, then the OPENROUTER_API_KEY
    environment (a shell or tray started before it was set does not see it: the user environment in the registry is
    read as well). The env key is often a different key (e.g. one used by another tool) without a limit."""
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        key = ""
    key = key or omp_key() or os.environ.get(ENV_VAR, "").strip()
    if not key and sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = str(winreg.QueryValueEx(k, ENV_VAR)[0]).strip()
        except OSError:
            pass
    return key or None


def period(kind, now=None):
    """(start, end) UTC datetimes of the limit period `kind` (daily / weekly / monthly) that contains `now`; OpenRouter
    resets at midnight UTC, weekly on Monday, monthly on the 1st. None for a limit that never resets."""
    now = now or datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "daily":
        return day, day + timedelta(days=1)
    if kind == "weekly":
        start = day - timedelta(days=day.weekday())
        return start, start + timedelta(days=7)
    if kind == "monthly":
        start = day.replace(day=1)
        return start, (start + timedelta(days=32)).replace(day=1)
    return None


def reading(data, now=None):
    """Status fields of the `data` object of the key endpoint. A key without a limit is `unlimited`; used = share of the
    limit spent in the current period."""
    out = {"state": "ok", "label": data.get("label")}
    limit, remaining = data.get("limit"), data.get("limit_remaining")
    if not isinstance(limit, (int, float)) or isinstance(limit, bool) or limit <= 0:
        return {**out, "unlimited": True, "spent": data.get("usage")}
    remaining = limit if not isinstance(remaining, (int, float)) else remaining
    spent = max(0.0, limit - remaining)
    out.update(unlimited=False, limit=limit, remaining=remaining, spent=round(spent, 4),
               used=round(min(100.0, spent / limit * 100), 3), kind=data.get("limit_reset"))
    p = period(out["kind"], now)
    if p:
        out.update(resets=p[1].isoformat(), span=(p[1] - p[0]).total_seconds())
    return out


def write_status(st):
    if st["state"] == "error":  # transient failure: keep the last numbers so the dashboard still shows them
        old = read_status()
        st = {**{k: old[k] for k in KEPT if k in old}, **st}
    st["at"] = time.time()
    fsutil.write_json_atomic(STATUS, st)  # atomic: the dashboard must never read a half-written file


def append_history(used):
    now = time.time()
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
    key = read_key()
    if not key:
        write_status({"state": "nokey", "error": f"no OpenRouter key found ({ENV_VAR} or {KEY_FILE})"})
        return
    try:
        d = http_util.request_json(URL, headers={"Authorization": "Bearer " + key, "Accept": "application/json"}, timeout=30)
    except http_util.HttpError as e:
        if e.status in (401, 403):
            write_status({"state": "signin", "error": f"OpenRouter rejected the key (HTTP {e.status})"})
        elif e.status is not None:
            write_status({"state": "error", "error": f"OpenRouter API HTTP {e.status}"})
        else:
            write_status({"state": "error", "error": str(e)[:150]})
        return
    data = (d or {}).get("data") if isinstance(d, dict) else None
    if not isinstance(data, dict):
        write_status({"state": "error", "error": "OpenRouter key endpoint: unexpected answer"})
        return
    st = reading(data)
    write_status(st)
    if not st["unlimited"]:
        append_history(st["used"])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never print the key: only the exception text
        print(f"openrouter monitor failed: {e}", file=sys.stderr)
        sys.exit(1)
