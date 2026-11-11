"""Pacer governor: keeps Paperclip's agents inside the usage budgets of their AI providers (run by the tray every 3 min).

Every provider (throttle.PROVIDERS: Claude subscription, GitHub Copilot, OpenRouter) has its own settings and its own sub-state;
only that provider's agents pause and resume with it. Nothing is handed over to another agent or provider:
  - Claude: polls Anthropic's OAuth usage endpoint (five_hour + seven_day utilization, same data as /usage)
  - Copilot: reads the monthly quota the Copilot monitor wrote (copilot-status.json)
  - OpenRouter: reads the spend limit of the API key the OpenRouter monitor wrote (openrouter-status.json)
  - active  -> paused when ANY window >= pause limit, or (setting byPace) burns clearly too fast for its reset
  - paused  -> active when ALL windows <  resume limit / the usage is back on the plan line (throttle.decide)
  - drain: agents with a live run are not cancelled until a window reaches the hard limit; idle agents pause at once
  - agents paused manually are never resumed by the governor
  - fail closed: 3 consecutive Claude usage-poll failures count as "no capacity" for Claude
  - keeps the WSL-side copy of the Claude token + Paperclip's stored secret fresh (the server runs in WSL, only Windows
    has the logged-in `claude`). The WSL copy has NO refresh token so the two sides can never rotate each other's.
Without Paperclip (settings key paperclip false, or no install detected) it only reads the usage and writes the status.
Flags (verification only): --dry-run (no mutations), --fake=<5h%>,<7d%> (skip the usage fetch).
State: claude-governor-state.json   Log: claude-governor.log   Status for the UI: claude-governor-status.json
History: usage-history.sqlite (usage_store.py), one row per successful usage poll (the dashboard's history and resets).
"""
import hashlib, json, os, re, sqlite3, subprocess, sys, time, traceback

import copilot_monitor, fsutil, http_util, logutil, openrouter_monitor, throttle, tray_icon, usage_store
import pacer_config, paperclip_common

DIR = pacer_config.DIR
CREDS = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
LOGGER = logutil.get_logger("governor", "claude-governor.log")
STATE = os.path.join(DIR, "claude-governor-state.json")
LOCK = os.path.join(DIR, "claude-governor.lock")
STATUS = os.path.join(DIR, "claude-governor-status.json")
SETTINGS = os.path.join(DIR, "claude-governor-settings.json")  # written by the dashboard

USAGE_FAILS_BEFORE_PAUSE = 3
COPILOT_STALE_S = 3 * 300  # the tray reads the Copilot quota and the OpenRouter limit every 5 min; older than this = no reading
TOKEN_REFRESH_WINDOW_S = 20 * 60
LOCK_MAX_AGE_S = 30 * 60  # a tick takes seconds; a lock held longer than this belongs to a hung tick
CLAUDE_UA = "claude-code/2.1.302"
NO_WINDOW = 0x08000000


def js(x):
    """Value as JS prints it in a template string (None -> null)."""
    return "null" if x is None else x


def num(x):
    """JSON number as the JS governor wrote it: 31.0 -> 31 (the log lines and status readers expect that)."""
    return int(x) if isinstance(x, float) and x.is_integer() else x


def usage_window(j, key):
    """One window of the usage response ({} when absent or not an object)."""
    w = j.get(key) if isinstance(j, dict) else None
    return w if isinstance(w, dict) else {}


class Governor:
    def __init__(self, dry=False, fake=None):
        self.dry, self.fake = dry, fake
        self.cfg = pacer_config.get()
        self.live = None  # agent ids with a live run, fetched once per tick when a provider has to pause

    # ---- plumbing
    def log(self, msg):
        text = f"{'[dry] ' if self.dry else ''}{msg}"
        LOGGER.info(text)
        if sys.stdout is not None:  # pythonw has no stdout
            sys.stdout.write(f"{logutil.iso_now()} {text}\n")

    def load_state(self):
        try:
            with open(STATE, encoding="utf-8") as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {"usageFailures": 0}
        if "providers" not in st:  # state from before the split by provider: everything belonged to Claude
            st["providers"] = {"claude": {"mode": st.pop("mode", "active"), "pausedAgents": st.pop("pausedAgents", [])}}
        for name in throttle.PROVIDERS:
            st["providers"].setdefault(name, {"mode": "active", "pausedAgents": []})
        return st

    def save_state(self, st):
        if not self.dry:
            fsutil.write_json_atomic(STATE, st, indent=2)

    def write_status(self, st, settings, usage):
        """Status for the tray and the dashboard: every provider's sub-state with the settings it was decided on, and
        the overall mode (the worst sub-state)."""
        if self.dry:
            return
        providers = {n: {"mode": st["providers"][n]["mode"], "reason": st["providers"][n].get("reason"), **settings[n]}
                     for n in throttle.PROVIDERS}
        fsutil.write_json_atomic(STATUS, {"at": logutil.iso_now(), "mode": throttle.overall(p["mode"] for p in providers.values()),
                                          "providers": providers, "usage": usage}, indent=2)

    def pc(self, method, path, body=None):
        """Paperclip is local_trusted: headerless requests act as the local board operator."""
        text = http_util.request_text(self.cfg["url"] + "/api" + path, method=method,
                                      headers={"Content-Type": "application/json"} if body is not None else {},
                                      body=None if body is None else json.dumps(body), timeout=60)
        try:
            return json.loads(text) if text else None
        except ValueError:
            return text

    def read_settings(self, st):
        """The dashboard rewrites the settings file in place; a torn read must NOT silently fall back to the 70/60 limits
        (that paused the agents while "Full budget" was on). Retry, then keep the last good copy."""
        err = None
        for _ in range(4):
            try:
                with open(SETTINGS, encoding="utf-8") as f:
                    st["settings"] = cfg = json.load(f)
                return cfg
            except FileNotFoundError:
                st.pop("settings", None)
                return {}
            except (OSError, ValueError) as e:
                err = e
                time.sleep(0.15)
        self.log(f"WARN settings unreadable ({err}) -> keeping last good settings {json.dumps(st.get('settings', {}))}")
        return st.get("settings", {})

    # ---- token upkeep
    def read_creds(self):
        try:
            with open(CREDS, encoding="utf-8") as f:
                return json.load(f)["claudeAiOauth"]
        except (OSError, ValueError, KeyError, TypeError):
            raise RuntimeError("Claude login not found - sign in with: claude auth login")

    def ensure_fresh_token(self):
        oauth = self.read_creds()
        left = oauth["expiresAt"] / 1000 - time.time()
        if left >= TOKEN_REFRESH_WINDOW_S:
            return oauth
        self.log(f"token expires in {left / 60:.1f} min -> refreshing via claude -p")
        if self.dry:
            return oauth
        try:
            r = subprocess.run([self.cfg["claudeExe"], "-p", "Reply with: ok", "--max-turns", "1"], capture_output=True,
                               text=True, timeout=120, creationflags=NO_WINDOW)
            code = r.returncode
        except (OSError, subprocess.TimeoutExpired) as e:
            code = type(e).__name__
        oauth = self.read_creds()
        after = oauth["expiresAt"] / 1000 - time.time()
        self.log(f"claude -p exit={code}; token now expires in {after / 60:.1f} min")
        if after <= left:
            self.log("WARN claude CLI did not refresh the token yet (will retry next tick)")
        return oauth

    def sync_token(self, oauth, company, st):
        fp = hashlib.sha256(oauth["accessToken"].encode()).hexdigest()
        if st.get("syncedTokenSha") == fp:
            return
        self.log("token changed -> syncing to WSL and reconnecting Paperclip's stored connection")
        if self.dry:
            return
        stripped = {k: v for k, v in oauth.items() if k not in ("refreshToken", "refreshTokenExpiresAt")}
        payload = json.dumps({"claudeAiOauth": stripped})
        w = pacer_config.wsl_run(self.cfg, "mkdir -p ~/.claude && umask 077 && cat > ~/.claude/.credentials.json.tmp && "
                                 "mv ~/.claude/.credentials.json.tmp ~/.claude/.credentials.json", 60, payload)
        if not w.ok:
            raise RuntimeError(f"writing WSL credentials failed (status {w.code})")
        conns = self.pc("GET", f"/companies/{company['id']}/ai-connections")
        conn = next((c for c in conns["connections"] if c["provider"] == "anthropic" and c["method"] == "subscription"
                     and c["ownership"] == "personal"), None)
        if not conn:
            raise RuntimeError("no personal anthropic subscription connection found in Paperclip")
        # Paperclip >= 2026.1023 only accepts a local connect for an open sign-in attempt. Open one, drop the
        # (refresh-token-free) credential into the attempt's isolated login home - the server only reads accessToken
        # from there - and complete it.
        intent = {"provider": "anthropic", "method": "subscription", "name": conn["name"], "ownership": "personal",
                  "connectionId": conn["id"], "agentIds": [], "allAgents": False}
        attempt = self.pc("POST", f"/companies/{company['id']}/ai-connections/local/attempts", {**intent, "restart": True})
        if not re.fullmatch(r"[0-9a-f-]{36}", attempt.get("sessionId", "")):
            raise RuntimeError("unexpected sign-in attempt id")
        home = f"{self.cfg['wslInstance']}/ai-local-logins/{attempt['sessionId']}"
        h = pacer_config.wsl_run(self.cfg, f"umask 077 && cat > {home}/.credentials.json", 60, payload)
        if not h.ok:
            raise RuntimeError(f"writing sign-in attempt credentials failed (status {h.code}): {h.err or h.error}")
        self.pc("POST", f"/companies/{company['id']}/ai-connections/local", {**intent, "localSessionId": attempt["sessionId"]})
        st["syncedTokenSha"] = fp
        self.log("token synced")

    # ---- usage
    def read_usage(self, oauth):
        if self.fake:
            a, b = (float(x) for x in self.fake.split(","))
            return {"five": num(a), "seven": num(b), "resets5": None, "resets7": None}
        try:
            j = http_util.request_json("https://api.anthropic.com/api/oauth/usage", headers={
                "Authorization": f"Bearer {oauth['accessToken']}", "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": CLAUDE_UA, "Content-Type": "application/json"}, timeout=15)
        except http_util.HttpError as e:
            if e.status is None:
                raise
            raise RuntimeError(f"usage endpoint HTTP {e.status}") from None
        five_w, seven_w = usage_window(j, "five_hour"), usage_window(j, "seven_day")
        five, seven = five_w.get("utilization"), seven_w.get("utilization")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (five, seven)):
            keys = ",".join(sorted(j)) if isinstance(j, dict) else type(j).__name__
            self.log(f"ERROR usage endpoint schema changed: keys seen: {keys}")
            raise RuntimeError("usage endpoint schema changed")  # tick() counts it as a failed usage poll
        return {"five": num(five), "seven": num(seven), "resets5": five_w.get("resets_at"), "resets7": seven_w.get("resets_at")}

    def record_usage(self, usage):
        """Keeps the poll for the dashboard's history. A store that cannot be written must not stop the governor deciding."""
        try:
            usage_store.append(usage["five"], usage["seven"], usage["resets5"], usage["resets7"])
        except (sqlite3.Error, OSError, TimeoutError) as e:
            self.log(f"WARN usage history not written: {e}")

    def live_runs(self, company):
        runs = self.pc("GET", f"/companies/{company['id']}/live-runs")
        return runs if isinstance(runs, list) else (runs or {}).get("runs", [])

    # ---- one tick
    def tick(self):
        st = self.load_state()
        cfg = self.read_settings(st)
        # Without Paperclip (no install detected and no settings.paperclip = true) the governor only watches the Claude
        # limits: it reads the usage and writes the status file, and never pauses anything.
        with_pc = (cfg["paperclip"] is True) if "paperclip" in cfg else bool(self.cfg.get("wslDistro"))
        company, agents = None, []
        if with_pc:
            try:
                http_util.request_text(self.cfg["url"] + "/api/health", timeout=8)
                healthy = True
            except http_util.HttpError:
                healthy = False
            if not healthy:
                self.log("Paperclip not healthy, skipping tick")
                return
            companies = [c for c in self.pc("GET", "/companies") if c.get("status") == "active"]
            if len(companies) != 1:
                raise RuntimeError(f"expected exactly 1 active company, found {len(companies)}")
            company = companies[0]
            agents = [a for a in self.pc("GET", f"/companies/{company['id']}/agents") if a.get("status") != "terminated"]
            if not any(a.get("adapterType") == "claude_local" for a in agents):
                raise RuntimeError("no claude_local agent found")

        settings = {n: throttle.settings_for(cfg, n) for n in throttle.PROVIDERS}
        oauth = self.ensure_fresh_token()
        if with_pc:
            self.sync_token(oauth, company, st)

        # usage -> decision
        usage = None
        try:
            usage = self.read_usage(oauth)
            st["usageFailures"] = 0
        except Exception as e:
            st["usageFailures"] = st.get("usageFailures", 0) + 1
            self.log(f"WARN usage poll failed ({st['usageFailures']}/{USAGE_FAILS_BEFORE_PAUSE}): {e}")
        if usage and not self.dry:
            self.record_usage(usage)
        if not with_pc:  # no Paperclip: report the usage, never pause anything
            if usage:
                self.log(f"5h {usage['five']}% (resets {js(usage['resets5'])}), 7d {usage['seven']}% (resets {js(usage['resets7'])}); monitor only")
                self.write_status(st, settings, usage)
            self.save_state(st)
            return
        unknown = not usage and st["usageFailures"] >= USAGE_FAILS_BEFORE_PAUSE
        if not usage and not unknown:
            self.save_state(st)
            return

        readings = {"claude": [] if unknown else [
            {"usage": usage["five"], "reset": usage["resets5"], "key": "5h", "span": tray_icon.SPAN_S["5h"]},
            {"usage": usage["seven"], "reset": usage["resets7"], "key": "7d", "span": tray_icon.SPAN_S["7d"]}],
            "copilot": self.copilot_windows(cfg), "openrouter": self.openrouter_windows(cfg)}
        desc = {"claude": (f"5h {usage['five']}% (resets {js(usage['resets5'])}), 7d {usage['seven']}% (resets {js(usage['resets7'])})"
                           if usage else "usage unavailable")}
        if readings["openrouter"]:
            w = readings["openrouter"][0]
            desc["openrouter"] = f"key limit {w['usage']:g}%" + (f" (resets {w['reset']})" if w["reset"] else " (never resets)")
        for name in throttle.PROVIDERS:
            self.govern(name, st["providers"][name], settings[name], readings[name], agents, company,
                        desc.get(name), unknown and name == "claude")
        self.write_status(st, settings, usage)
        self.save_state(st)

    def copilot_windows(self, cfg):
        """The monthly Copilot quota as a usage window list: [] when Copilot is not throttled (hidden in the dashboard, or
        an unlimited plan), None without a usable reading (not signed in, monitor not running): then nothing changes."""
        if cfg.get("copilot") is False:
            return []
        cs = copilot_monitor.read_status()
        if cs.get("unlimited"):
            return []
        if cs.get("state") not in ("ok", "error") or cs.get("used") is None or not cs.get("resets") \
                or time.time() - cs.get("at", 0) > COPILOT_STALE_S:
            return None
        start, end = copilot_monitor.period(cs["resets"])
        return [{"usage": cs["used"], "reset": cs["resets"], "key": "cop", "span": end - start, "early": 6 * 3600}]

    def openrouter_windows(self, cfg):
        """The spend limit of the OpenRouter key as a usage window list: [] when it is not throttled (hidden in the
        dashboard, or a key without a limit), None without a usable reading (no key, monitor not running): then nothing
        changes. A limit that never resets has no window to pace against: reset and span are None."""
        if cfg.get("openrouter") is False:
            return []
        os_ = openrouter_monitor.read_status()
        if os_.get("unlimited"):
            return []
        if os_.get("state") not in ("ok", "error") or os_.get("used") is None or time.time() - os_.get("at", 0) > COPILOT_STALE_S:
            return None
        span = os_.get("span")
        return [{"usage": os_["used"], "reset": os_.get("resets"), "key": "or", "span": span, "early": span * 0.05 if span else None}]

    def govern(self, name, ps, s, windows, agents, company, desc, unknown):
        """Decide and apply one provider's sub-state. Only that provider's agents pause and resume with its limits;
        agents of no known provider are never paused, and one the governor paused earlier is resumed at once. Nothing
        is handed to another agent. Only agents the governor paused itself are resumed again."""
        mine = set(ps.get("pausedAgents") or [])
        for a in agents:
            if throttle.provider_of(a) != name and a.get("status") == "paused" and a["id"] in mine:
                self.log(f"resuming {a['name']} (not on the {throttle.LABEL[name]} budget)")
                if not self.dry:
                    self.pc("POST", f"/agents/{a['id']}/resume")
                mine.discard(a["id"])
        ps["pausedAgents"] = sorted(mine)
        if windows is None:
            return
        agents = [a for a in agents if throttle.provider_of(a) == name]
        if not agents:  # nobody to pause: this provider cannot hold the overall state
            ps.update(mode="active", reason=None, pausedAgents=[])
            return
        mode = ps.get("mode", "active")
        if windows or unknown:
            target, reason = throttle.decide(mode, ps.get("reason"), windows, s, unknown)
        else:
            target, reason = "active", None  # not throttled
        hold = max((w["usage"] for w in windows), default=100)
        if desc is None:
            desc = f"month {hold:g}% (resets {windows[0]['reset']})" if windows else "not throttled"
        self.log(f"{name}: {desc}; mode={mode} target={target}{f' ({reason})' if reason else ''}")
        ps["reason"] = reason
        if target != "active":
            if self.live is None:
                self.live = {r["agentId"] for r in self.live_runs(company)}
            hard_at = throttle.limits(s)[2]
            hard = hold >= hard_at
            draining = 0
            for a in agents:
                if a.get("status") == "paused":
                    continue  # already paused (manually or by us)
                if a["id"] in self.live and not hard:
                    draining += 1  # let the running run finish
                    continue
                self.log(f"pausing {a['name']}{' (HARD limit: cancelling live run)' if a['id'] in self.live else ''}")
                if not self.dry:
                    self.pc("POST", f"/agents/{a['id']}/pause")
                mine.add(a["id"])
            if draining:
                self.log(f"draining: {draining} agent(s) with a live run, pausing once they finish (hard stop at {hard_at}%)")
            ps["mode"] = "draining" if draining else "paused"
        else:
            ps["mode"] = "active"
            for a in agents:
                if a.get("status") != "paused" or a["id"] not in mine:
                    continue
                self.log(f"resuming {a['name']}")
                if not self.dry:
                    self.pc("POST", f"/agents/{a['id']}/resume")
            mine.clear()
        ps["pausedAgents"] = sorted(mine)


def run(dry=False, fake=None):
    """One governor tick under the single-instance lock. A lock whose holder died, or that is older than LOCK_MAX_AGE_S,
    is taken over. Returns True unless the tick failed."""
    if not dry:
        paperclip_common.migrate_settings()
    if not fsutil.pid_lock_take(LOCK, LOCK_MAX_AGE_S):
        if sys.stdout is not None:
            print("another governor tick is running")
        return True
    try:
        g = Governor(dry, fake)
        try:
            g.tick()
            return True
        except Exception as e:
            g.log(f"ERROR {traceback.format_exc().strip()}" if not isinstance(e, RuntimeError) else f"ERROR {e}")
            return False
    finally:
        fsutil.pid_lock_release(LOCK)


if __name__ == "__main__":
    fake = next((a[7:] for a in sys.argv if a.startswith("--fake=")), None)
    sys.exit(0 if run("--dry-run" in sys.argv, fake) else 1)
