"""Throttle rules per AI provider: settings, which agents belong to a provider, and the pause / resume decision.

Pure functions shared by the governor (decides), the tray and the dashboard (show the same thresholds). Every provider
has its own settings under settings["providers"][name] and its own sub-state (active / draining / paused); the Pacer's
overall state is the worst of them.

  fullBudget   the only limit is the real one (100 %)
  pauseAt      pause the provider's agents when a usage window reaches this percentage
  resumeBelow  resume them once every window is below this percentage
  byPace       also pause while a window burns so fast that it would hit the limit clearly before its reset (the red
               verdict of the dashboard), and resume once the usage is back on the plan line
"""
import tray_icon

PROVIDERS = ("claude", "copilot", "openrouter")
LABEL = {"claude": "Claude", "copilot": "Copilot", "openrouter": "OpenRouter"}
DEFAULTS = {"claude": {"fullBudget": False, "pauseAt": 70, "resumeBelow": 60, "byPace": False},
            # a monthly quota is only meaningful as a pace: percentage limits would pause until the next month
            "copilot": {"fullBudget": True, "pauseAt": 70, "resumeBelow": 60, "byPace": True},
            # the 100 % is the spend limit the user set on the API key: by default only that limit pauses the agents
            "openrouter": {"fullBudget": True, "pauseAt": 70, "resumeBelow": 60, "byPace": False}}
LEGACY_KEYS = ("fullBudget", "pauseAt", "resumeBelow")  # Claude's limits before the settings were split by provider
HARD_AT = 90  # a live run is cancelled (instead of drained) from here on, or from pauseAt when that is higher
RANK = {"active": 0, "draining": 1, "paused": 2}


def _pct(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100


def settings_for(cfg, name):
    """Validated throttle settings of one provider; anything missing or invalid falls back to its default."""
    raw = ((cfg or {}).get("providers") or {}).get(name)
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS[name])
    for key in ("fullBudget", "byPace"):
        if isinstance(raw.get(key), bool):
            out[key] = raw[key]
    if _pct(raw.get("pauseAt")) and _pct(raw.get("resumeBelow")) and raw["resumeBelow"] <= raw["pauseAt"]:
        out["pauseAt"], out["resumeBelow"] = raw["pauseAt"], raw["resumeBelow"]
    return out


def migrate(cfg):
    """Moves the limits of a settings file from before the split (top-level fullBudget / pauseAt / resumeBelow) to
    providers.claude. Returns the new settings dict, or None when there is nothing to move."""
    if not any(k in cfg for k in LEGACY_KEYS):
        return None
    out = {k: v for k, v in cfg.items() if k not in LEGACY_KEYS}
    providers = dict(out.get("providers") or {})
    providers["claude"] = {**{k: cfg[k] for k in LEGACY_KEYS if k in cfg}, **(providers.get("claude") or {})}
    out["providers"] = providers
    return out


def limits(s):
    """(pause at, resume below, hard stop) in percent for provider settings `s`."""
    if s["fullBudget"]:
        return 100, 100, 100
    return s["pauseAt"], s["resumeBelow"], max(HARD_AT, s["pauseAt"])


def provider_of(agent):
    """Provider whose budget a Paperclip agent runs on, or None (such agents are never paused by the Pacer)."""
    adapter = agent.get("adapterType")
    if adapter == "claude_local":
        return "claude"
    model = str((agent.get("adapterConfig") or {}).get("model") or "")
    if adapter == "opencode_local" and model.startswith("github-copilot/"):
        return "copilot"
    if adapter in ("opencode_local", "pi_local") and model.startswith("openrouter/"):
        return "openrouter"
    return None


def overall(modes):
    """The single state of the Pacer: the worst sub-state."""
    return max(modes, key=RANK.__getitem__, default="active")


def decide(mode, reason, windows, s, unknown=False):
    """(target, reason) for one provider. mode: its current sub-state; reason: why it was paused ("limit", "pace",
    "unknown") or None; windows: [{"usage": %, "reset": ISO or None, "key": "5h"|"7d"|"cop", "span": seconds,
    "early": seconds or None}]; unknown: no usage reading can be had (fail closed). target is "active" or "paused";
    draining is the caller's business (agents with a live run)."""
    if unknown:
        return "paused", "unknown"
    pause_at, resume_below, _hard = limits(s)
    over = any(w["usage"] >= pause_at for w in windows)
    low = all(w["usage"] < resume_below for w in windows)
    fast = calm = False
    if s["byPace"]:
        paces = [(tray_icon.pace(w["usage"], w["reset"], w["key"], pause_at, span_s=w["span"], early_s=w.get("early")), w)
                 for w in windows]
        fast = any(p is not None and p["verdict"] == "too much" and tray_icon.window_verdict(p, w["usage"], w["span"]) == "bad"
                   for p, w in paces)
        calm = not any(p is not None and p["verdict"] == "too much" for p, _w in paces)  # back on the plan line
    if mode == "active":
        if over:
            return "paused", "limit"
        if fast:
            return "paused", "pace"
        return "active", None
    if over:
        return "paused", "limit"
    if reason == "pace":
        if calm or not s["byPace"]:
            return "active", None
        return "paused", "pace"
    if low and not fast:
        return "active", None
    return "paused", reason or "limit"
