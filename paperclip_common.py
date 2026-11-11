"""Shared by tray.pyw and dashboard.py: file locations, status/settings IO, text formatting."""
import json, os, time
from datetime import datetime
import throttle, tray_icon
import pacer_config
import fsutil, http_util

CFG = pacer_config.get()   # detected + user overrides (pacer-config.json); see pacer_config.py
DIR = pacer_config.DIR
STATUS = os.path.join(DIR, "claude-governor-status.json")
SETTINGS = os.path.join(DIR, "claude-governor-settings.json")
TRAY_STATE = os.path.join(DIR, "tray-state.json")      # written by the tray: job results + server health
CHECK_FLAG = os.path.join(DIR, "tray-check.flag")      # created by the dashboard: tray runs the governor now
LOGS = {"governor": os.path.join(DIR, "claude-governor.log"), "watchdog": os.path.join(DIR, "watchdog.log")}
URL = CFG["url"]
STALE_S = 10 * 60
UPDATE_RUN = os.path.join(DIR, "update-run.json")      # progress of updater.py
SPAN_S = {"5h": 5 * 3600, "7d": 7 * 86400}
CLAUDE_EXE = CFG["claudeExe"]


def read_status():
    """(status dict, age in seconds) or (None, None)."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            s = json.load(f)
        age = time.time() - datetime.fromisoformat(s["at"].replace("Z", "+00:00")).timestamp()
        return s, age
    except Exception:
        return None, None


def read_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_settings(**kv):
    """Read-modify-write of the settings file. The lock covers the whole cycle, so the tray, the dashboard and the
    governor never drop each other's keys; the atomic replace means readers never see a half-written file."""
    with fsutil.file_lock(SETTINGS + ".lock"):
        cfg = read_settings()
        cfg.update(kv)
        fsutil.write_json_atomic(SETTINGS, cfg)


def paperclip_enabled():
    """Paperclip is part of the Pacer when the user said so (settings key paperclip: true/false) or, by default, when an
    install was detected. Without it the Pacer only watches the Claude limits: nothing is paused, no server is watched."""
    pref = read_settings().get("paperclip")
    return bool(pref) if pref is not None else bool(CFG.get("wslDistro"))


def migrate_settings():
    """One-time move of the limits from before the split by provider into providers.claude (see throttle.migrate)."""
    if throttle.migrate(read_settings()) is None:
        return
    with fsutil.file_lock(SETTINGS + ".lock"):
        new = throttle.migrate(read_settings())
        if new is not None:
            fsutil.write_json_atomic(SETTINGS, new)


def provider_settings(name):
    """Throttle settings of one provider (throttle.settings_for) from the settings file."""
    return throttle.settings_for(read_settings(), name)


def full_budget(provider="claude"):
    """Without Paperclip there is nobody to pause, so the only limit is the real one (100 %)."""
    return not paperclip_enabled() or provider_settings(provider)["fullBudget"]


def keep_awake():
    """True while the user wants the PC kept from sleeping (tray holds the wake lock; screen may still turn off)."""
    return read_settings().get("keepAwake") is True


def sleep_when_idle():
    """True if the PC may sleep while Paperclip does nothing (only meaningful together with keep_awake)."""
    return paperclip_enabled() and read_settings().get("sleepWhenIdle") is True


def paperclip_busy():
    """True while any agent is running or an update runs. An unreachable server counts as busy (restart / update in progress)."""
    try:
        def get(path):
            return http_util.request_json(URL + path, timeout=10)
        for c in get("/api/companies"):
            if any(a.get("status") == "running" for a in get(f"/api/companies/{c['id']}/agents")):
                return True
    except Exception:
        return True
    try:
        with open(UPDATE_RUN, encoding="utf-8") as f:
            return json.load(f).get("state") == "running"
    except Exception:
        return False


def fmt_min(m):
    if m is None:
        return "-"
    m = int(m)
    if m >= 1440:
        return f"{m // 1440}d {m % 1440 // 60}h"
    if m >= 60:
        return f"{m // 60}h {m % 60:02d}m"
    return f"{m}m"


def pace_text(p):
    """One line verdict for a tray_icon.pace() result."""
    if not p:
        return None
    v = p["verdict"]
    if v == "early":
        return "pace: too early to tell (tentative)"
    if v == "limit":
        return "pace: limit reached"
    if v == "too much":
        return f"pace: TOO MUCH - limit in ~{fmt_min(p['eta_min'])}"
    if v == "too little":
        return f"pace: too little - ends ~{p['projected']:.0f}%"
    return f"pace: on plan - ends ~{p['projected']:.0f}%"


def evaluate(gov_ok=None, wd_ok=None, server_ok=None):
    """(icon model, tooltip) from the governor status + job/server health (shared by tray and dashboard)."""
    if not paperclip_enabled():
        wd_ok = server_ok = None
    s, age = read_status()
    fault = s is None or age > STALE_S or gov_ok is False or wd_ok is False or server_ok is False
    fb = full_budget()
    cl = {**provider_settings("claude"), "fullBudget": fb}
    pause_at, resume_below, _hard = throttle.limits(cl)
    model = {"pct": None, "threshold": pause_at, "full_budget": fb, "cooldown": None,
             "window": "5h", "paused": False, "fault": fault, "ratio": None, "verdict": None, "pace": None,
             "glow": "muted", "providers": {n: "active" for n in throttle.PROVIDERS}}
    if s is None:
        tip = "Claude: no status yet"
    else:
        u = s.get("usage") or {}
        five, seven = u.get("five"), u.get("seven")
        if isinstance(five, (int, float)) and isinstance(seven, (int, float)):
            win = "5h" if five >= seven else "7d"  # same rule as the governor: the higher window limits
            model["window"], model["pct"] = win, max(five, seven)
            reset = u.get("resets5" if win == "5h" else "resets7")
            if reset:
                try:
                    left = datetime.fromisoformat(reset).timestamp() - time.time()
                    model["cooldown"] = min(1.0, max(0.0, left / SPAN_S[win]))
                except ValueError:
                    pass
            p = tray_icon.pace(model["pct"], reset, win, model["threshold"])
            if p:
                model["pace"], model["ratio"], model["verdict"] = p, p["ratio"], p["verdict"]
            if isinstance(seven, (int, float)):  # the glow is always the 7d window's pace
                p7 = tray_icon.pace(seven, u.get("resets7"), "7d", model["threshold"])
                model["glow"] = tray_icon.window_verdict(p7, seven, SPAN_S["7d"])
        subs = s.get("providers") or {}
        model["providers"] = {n: (subs.get(n) or {}).get("mode", "active") for n in throttle.PROVIDERS}
        model["paused"] = s["mode"] != "active"  # overall: the worst sub-state
        pct, win = model["pct"], model["window"]
        mine = subs.get("claude") or {}
        state = {"active": "running", "draining": "draining"}.get(model["providers"]["claude"], "paused")
        l1 = f"{win} usage {pct:g}% - {state}" if pct is not None else f"usage unknown - {state}"
        if state != "running":
            l3 = "resumes when back on plan" if mine.get("reason") == "pace" else f"resumes below {resume_below}%"
        elif fb:
            l3 = "full budget: pause at 100%"
        else:
            l3 = f"pauses at {pause_at}%"
        if mine.get("fullBudget", fb) is not fb:
            l3 += " (applying...)"
        if model["cooldown"] is not None:
            l3 += f" - resets in {fmt_min(model['cooldown'] * SPAN_S[win] // 60)}"
        others = [f"{throttle.LABEL[n]} {'draining' if m == 'draining' else 'paused'}"
                  for n, m in model["providers"].items() if n != "claude" and m != "active"]
        tip = "\n".join(x for x in (l1, pace_text(model["pace"]), l3, *others) if x)
    probs = [n for n, ok in (("governor", gov_ok), ("watchdog", wd_ok)) if ok is False]
    if server_ok is False:
        probs.insert(0, "SERVER DOWN")
    if age is not None and age > STALE_S:
        probs.append(f"status {int(age // 60)}m old")
    if probs:
        tip += "\n" + ", ".join(probs)
    return model, tip[:127]
