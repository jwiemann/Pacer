"""Data of the Pacer dashboard: what the governor, tray, watchdog, Copilot monitor and Paperclip files say.

No Tk and no side effects beyond pacer_config / paperclip_common reading their settings, so tests can import this
module without opening a window or taking the dashboard's single-instance mutex (that lives in dashboard.main()).
Colours come back as tone names ("good", "warn", "bad", "muted"); dashboard maps them to hex via ui_widgets.HEX.
"""
import json, os, shutil, threading, time
from datetime import datetime

import fsutil, http_util, pacer_config
import token_dump, usage_store
from paperclip_common import CFG, DIR, LOGS, CLAUDE_EXE, TRAY_STATE, STALE_S, fmt_min


CLAUDE_CREDS = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
COPILOT_HIST = os.path.join(pacer_config.DIR, "copilot-history.jsonl")
OPENROUTER_HIST = os.path.join(pacer_config.DIR, "openrouter-history.jsonl")
COPILOT_EVERY_S = 300  # the tray runs copilot_monitor.py and openrouter_monitor.py this often
LAST = {}  # newest governor poll: fallback when the usage endpoint fails (HTTP 429)
_hist = {"version": None, "rows": [], "resets": []}  # cache of the usage store, see _usage()


def _usage():
    """Usage store rows and resets, re-read only when the store changed (the dashboard renders every 5 s)."""
    version = usage_store.version()
    if version != _hist["version"]:
        rows = usage_store.rows()
        _hist.update(version=version, rows=[(r.ts, r.five, r.seven) for r in rows], resets=usage_store.resets())
        if rows:  # newest poll: the reading shown when the usage endpoint fails (HTTP 429)
            r = rows[-1]
            LAST.update(t=r.ts, five=r.five, seven=r.seven, resets5=r.resets5, resets7=r.resets7)
    return _hist


def history():
    """[(epoch, five, seven)] of every governor poll kept (30 days), oldest first."""
    return _usage()["rows"]


def past_resets():
    """[(epoch, "5h"|"7d")] of window resets that already happened: every reset the stored polls named, rounded to the
    minute, that lies in the past."""
    now = time.time()
    return [r for r in _usage()["resets"] if r[0] <= now]


def throughput(hist, idx, minutes=60):
    """Usage growth in %-points per hour over the last `minutes`; window resets (drops) are ignored."""
    pts = [h for h in hist if h[0] >= time.time() - minutes * 60]
    if len(pts) < 2:
        return None
    gained = sum(max(0.0, b[idx] - a[idx]) for a, b in zip(pts, pts[1:]))
    return gained / ((pts[-1][0] - pts[0][0]) / 3600)


def quota_spark(pts, reset, span, usage, now):
    """Mini graph of one quota window for a sidebar tab: ([(x 0..1, percent)], top) or None without a running window
    or with fewer than two readings. The window's readings (end = `reset` ISO time, length `span` s) up to `now` plus the
    current `usage` are stretched over the whole graph width and the height follows the highest value (at least 5 %),
    so even a small curve fills the space; the burn-up chart shows the absolute picture."""
    try:
        end = datetime.fromisoformat(reset).timestamp() if reset else None
    except ValueError:
        end = None
    if end is None or usage is None or not span:
        return None
    start = end - span
    rows = [(t, v) for t, v in pts if start <= t <= now] + [(min(now, end), usage)]
    t0, t1 = rows[0][0], rows[-1][0]
    if t1 <= t0:
        return None
    return [((t - t0) / (t1 - t0), v) for t, v in rows], max(max(v for _t, v in rows), 5.0)


TOKENS_FILE = os.path.join(DIR, "token-usage.json")
TOKENS_EVERY_S = 150
_tok = {"mtime": None, "rows": [], "last_kick": 0.0, "busy": False}


def kick_token_dump():
    """Refreshes token-usage.json (Claude runs of the Paperclip database) in the background. Without a Paperclip install
    there is no database to read; a database that does not answer is simply tried again at the next kick."""
    if _tok["busy"] or time.time() - _tok["last_kick"] < TOKENS_EVERY_S or not CFG.get("wslDistro"):
        return
    _tok.update(busy=True, last_kick=time.time())

    def work():
        try:
            token_dump.dump(TOKENS_FILE, CFG["db"])
        except Exception:
            pass  # quiet by design: the dashboard keeps showing the last dump
        finally:
            _tok["busy"] = False
    threading.Thread(target=work, daemon=True).start()


def tokens_between(start, end):
    """Fresh tokens (input + output, cache reads excluded) of all Paperclip Claude runs finished in [start, end], or None."""
    try:
        m = os.stat(TOKENS_FILE).st_mtime_ns
        if m != _tok["mtime"]:
            with open(TOKENS_FILE, encoding="utf-8") as f:
                _tok.update(mtime=m, rows=json.load(f)["rows"])
    except (OSError, ValueError, KeyError):
        return None
    return sum(r[1] + r[2] for r in _tok["rows"] if start <= r[0] <= end)


def fmt_tok(n):
    n = abs(n)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{round(n / 1e3):d}k"
    return f"{round(n):d}"

def ago(epoch):
    if not epoch:
        return "never"
    s = int(time.time() - epoch)
    return "just now" if s < 10 else f"{s}s ago" if s < 90 else f"{fmt_min(s // 60)} ago"


def when(t):
    """Clock time of an epoch in local time: 'HH:MM' today, 'Day HH:MM' otherwise."""
    d = datetime.fromtimestamp(t)
    return d.strftime("%H:%M" if d.date() == datetime.now().date() else "%a %H:%M")


def claude_cli_found():
    return bool(shutil.which(CLAUDE_EXE) or os.path.exists(CLAUDE_EXE))


def last_governor_error():
    try:
        with open(LOGS["governor"], "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            errs = [ln for ln in f.read().decode("utf-8", "replace").splitlines() if " ERROR " in ln]
    except OSError:
        return ""
    return errs[-1] if errs else ""


def fault_reason(gov_ok, wd_ok, srv, age):
    """One short line naming what is wrong (the pill only says FAULT)."""
    if srv is False:
        return "Paperclip server is down"
    if gov_ok is False:
        last = last_governor_error()
        if "Claude login not found" in last:
            return "Claude Code not installed" if not claude_cli_found() else "Claude not signed in"
        if "local subscription" in last or "expires in -" in last:
            return "Claude login expired"
        return "Governor failing: " + (last.split(" ERROR ", 1)[1][:70] if last else "see governor log")
    if wd_ok is False:
        return "Watchdog failing - see watchdog log"
    if age is not None and age > STALE_S:
        return f"Governor status is {fmt_min(age // 60)} old"
    return "No status yet"


def claude_login(governor_failed_on_login):
    """(text, tone, tooltip) for the Claude login row. The access token (~8 h) is renewed by the governor through the
    refresh token; only when that refresh token runs out (or the governor reports an unverifiable login) must the user sign in."""
    try:
        with open(CLAUDE_CREDS, encoding="utf-8") as f:
            o = json.load(f)["claudeAiOauth"]
        acc, ref = o["expiresAt"] / 1000 - time.time(), o.get("refreshTokenExpiresAt")
        ref = None if ref is None else ref / 1000 - time.time()
    except (OSError, KeyError, ValueError, TypeError):
        return "no login found - sign in", "bad", ""
    if governor_failed_on_login or (ref is not None and ref <= 0):
        return "sign-in required", "bad", ""
    tok = "token expired, renews on next check" if acc <= 0 else f"token {fmt_min(acc // 60)}"
    if ref is None:
        return "OK", "good", tok
    days = ref / 86400
    left = f"login {int(days)}d" if days >= 3 else f"login ends in {fmt_min(ref // 60)}"
    if days < 3:  # about to run out: keep it visible
        return left, "warn", tok
    return "OK", "good", f"{tok}  -  {left}"


def tray_state():
    try:
        with open(TRAY_STATE, encoding="utf-8") as f:
            st = json.load(f)
        return st if time.time() - st["at"] < 3 * STALE_S else None  # tray gone -> unknown
    except Exception:
        return None


def quota_history(path, start):
    """[(epoch, used %)] of every reading in a monitor's history file since `start` (same row shape the pace chart uses for Claude: index 1 = usage)."""
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                    if r["t"] >= start:
                        rows.append((r["t"], r["used"]))
                except (ValueError, KeyError):
                    pass
    except OSError:
        pass
    return rows


def pct_text(v):
    return f"{v:.1f}%" if v < 10 else f"{v:.0f}%"


def fmt_cents(v):
    """Euro cents per second with two places after the dot: 0.0237 -> '0.02', 1.4 -> '1.40'."""
    return f"{max(v, 0.0):.2f}"


FX_FILE = os.path.join(DIR, "fx-usd-eur.json")
FX_URL = "https://api.frankfurter.dev/v1/latest?base=USD&symbols=EUR"  # ECB reference rate
FX_EVERY_S, FX_RETRY_S = 6 * 3600, 600
FX_FALLBACK = 0.9  # until a rate was fetched once
_fx = {"rate": None, "at": 0.0, "tried": 0.0, "busy": False}


def fx_fetch():
    """Fetches the ECB rate USD -> EUR and caches it; True on success."""
    try:
        rate = float(http_util.request_json(FX_URL, timeout=15)["rates"]["EUR"])
        if not 0.1 < rate < 10:
            return False
        _fx.update(rate=rate, at=time.time())
        fsutil.write_json_atomic(FX_FILE, {"rate": rate, "at": _fx["at"]})
        return True
    except (http_util.HttpError, KeyError, TypeError, ValueError, OSError):
        return False


def usd_eur():
    """Euros per US dollar: the cached ECB rate (read from the data dir on the first call), refreshed in the background
    every 6 h (a failed fetch is retried after 10 min; until then the last rate or a fallback is used)."""
    now = time.time()
    if _fx["rate"] is None:
        try:
            with open(FX_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            _fx.update(rate=float(cached["rate"]), at=float(cached["at"]))
        except (OSError, ValueError, KeyError, TypeError):
            _fx["rate"] = FX_FALLBACK
    if now - _fx["at"] >= FX_EVERY_S and now - _fx["tried"] >= FX_RETRY_S and not _fx["busy"]:
        _fx.update(busy=True, tried=now)

        def work():
            try:
                fx_fetch()
            finally:
                _fx["busy"] = False
        threading.Thread(target=work, daemon=True).start()
    return _fx["rate"]


def quota_moved(pts, usage, now, window_s=24 * 3600):
    """True if a quota's usage changed within the last window_s seconds: `pts` [(epoch, %)] readings (the newest one
    older than the window is the starting value) plus the current `usage` (None = no reading, never moved)."""
    if usage is None:
        return False
    since = now - window_s
    older = [v for t, v in pts if t < since]
    vals = ([older[-1]] if older else []) + [v for t, v in pts if t >= since] + [usage]
    return max(vals) - min(vals) > 0


def copilot_login(cs):
    """(text, tone, tooltip, needs_signin) for the Copilot login row."""
    if not cs:
        return "not checked yet", "muted", "", False
    state = cs.get("state")
    if state == "signin":
        return "sign-in required", "bad", cs.get("error", ""), True
    if state == "nologin":
        return "no login found - sign in", "bad", cs.get("error", ""), True
    if state == "error":
        return "check failed", "bad", cs.get("error", ""), False
    if time.time() - cs.get("at", 0) > 3 * COPILOT_EVERY_S:
        return "no recent data", "muted", f"Last reading {ago(cs.get('at'))} (is the tray running?)", False
    return "OK", "good", f"{cs.get('login', '?')}  -  {cs.get('plan', '?')}", False


def openrouter_key(os_):
    """(text, tone, tooltip) for the OpenRouter key row."""
    if not os_:
        return "not checked yet", "muted", ""
    state = os_.get("state")
    if state == "nokey":
        return "no key found", "bad", os_.get("error", "")
    if state == "signin":
        return "key rejected", "bad", os_.get("error", "")
    if state == "error":
        return "check failed", "bad", os_.get("error", "")
    if time.time() - os_.get("at", 0) > 3 * COPILOT_EVERY_S:
        return "no recent data", "muted", f"Last reading {ago(os_.get('at'))} (is the tray running?)"
    return "OK", "good", f"{os_.get('label') or 'key'}  -  " + ("no limit set" if os_.get("unlimited")
                                                                  else f"${os_['spent']:,.2f} of ${os_['limit']:,.2f} spent")
