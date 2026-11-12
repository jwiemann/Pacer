"""Live token throughput (an estimate) of AI use on this PC: Paperclip's agents, omp and Claude Code.

Paperclip agents (run logs the server writes during a run, in WSL):
  Claude  (claude_local, acpx):    {"type":"acpx.status","tag":"usage_update","used":N}   N = context tokens so far
  Copilot (opencode_local):        {"type":"step_finish","part":{"tokens":{"total":N,...}}}  per step, N = context so far
  Both are turned into the same unit: the growth of the run's context between readings (what the run newly processed).
  Only agents whose configured model is a Claude model (claude_local), github-copilot/* (opencode_local) or an
  openrouter/* model (opencode_local, pi_local) count.
omp / Claude Code (session transcripts, one line per reply with exact usage): input + output + cache writes of each reply;
  cache reads are not counted. omp replies count as Claude (provider anthropic), Copilot (github-copilot) or OpenRouter (openrouter).

Locations are configurable in claude-governor-settings.json: "runLogsDir", "ompDir", "claudecodeDir".
No Tk in here: the dashboard asks series() / rate() and draws.
"""
import bisect, json, math, os, threading, time
from collections import deque
from datetime import datetime

import http_util
from paperclip_common import CFG, URL, read_settings, paperclip_enabled

RUN_LOGS = CFG["runLogs"]   # detected UNC path of the WSL run-logs dir (None without Paperclip in WSL)
PROVIDERS = ("claude", "copilot", "openrouter")
OMP_PROVIDERS = {"anthropic": "claude", "github-copilot": "copilot", "openrouter": "openrouter"}  # omp's provider names
KEEP_S = 24 * 60 * 60  # the longest chart range
LEAD_S = 60 * 60       # events stay this much longer: the averaged curve at the left edge of the longest range needs them
ACTIVE_S = KEEP_S + LEAD_S  # a log file that was not written for this long is no longer followed
POLL_S = 2              # followed files are read this often
DISCOVER_S = 10         # the run-log directories are listed this often (a run starts a new file)
AGENTS_S = 60           # agent -> provider mapping is refreshed this often
BACKFILL_BYTES = 32 * 1024 * 1024  # a long session still covers the last 24 hours
HOME = os.path.expanduser("~")
TRANSCRIPTS = {"omp": os.path.join(HOME, ".omp", "agent", "sessions"),            # settings key "ompDir" overrides
               "claudecode": os.path.join(HOME, ".claude", "projects")}          # settings key "claudecodeDir" overrides


def provider_of(agent):
    """'claude' / 'copilot' / 'openrouter' for an agent dict of the Paperclip API, None for everything else."""
    model = (agent.get("adapterConfig") or {}).get("model") or ""
    kind = agent.get("adapterType")
    if kind == "claude_local" and model.startswith("claude-"):
        return "claude"
    if kind == "opencode_local" and model.startswith("github-copilot/"):
        return "copilot"
    if kind in ("opencode_local", "pi_local") and model.startswith("openrouter/"):
        return "openrouter"
    return None


FAMILIES = ("opus", "sonnet", "haiku", "fable")
RAMP_STEPS = 6  # sub-samples of the ramp that smooths the curve, see LiveTokens.series()


def family(model):
    """Model family shown in the chart: 'claude-sonnet-5-5' and 'github-copilot/claude-sonnet-5.5-1m' -> 'Sonnet';
    a model of no known family keeps the first word of its name ('gpt-5-mini' -> 'Gpt'), no name at all -> 'other'."""
    name = str(model or "").lower().rsplit("/", 1)[-1]
    for f in FAMILIES:
        if f in name:
            return f.capitalize()
    return name.split("-")[0].capitalize() or "other"


def parse_event(chunk):
    """The event dict inside one log record's chunk, or None."""
    try:
        ev = json.loads(chunk)
    except ValueError:
        return None
    return ev if isinstance(ev, dict) else None


def context_size(provider, ev):
    """Context tokens after this event, or None if the event carries none."""
    if provider == "claude":
        if ev.get("type") == "acpx.status" and ev.get("tag") == "usage_update" and isinstance(ev.get("used"), int):
            return ev["used"]
        return None
    if ev.get("type") == "step_finish":
        t = (ev.get("part") or {}).get("tokens") or {}
        total = t.get("total")
        return int(total) if isinstance(total, (int, float)) else None
    return None


def transcript_tokens(kind, rec, state):
    """[(timestamp, provider, tokens, cache_read, prompt, family)] of one transcript line. tokens = what a reply newly consumed:
    input + output + cache writes. Cache READS are the re-read conversation and are not counted in tokens (that is what
    the Paperclip streams approximate by context growth); they are reported separately for the cache hit rate:
    cache_read / (cache_read + prompt), prompt = input + cache writes (the prompt side only). Claude Code writes one
    line per content block of the same message id, so a message counts once (state keeps what was already counted per id)."""
    m = rec.get("message") or {}
    if kind == "omp":
        if rec.get("type") != "message" or m.get("role") != "assistant" or not m.get("usage"):
            return None
        provider = OMP_PROVIDERS.get(m.get("provider"))
        u = m["usage"]
        prompt = int(u.get("input") or 0) + int(u.get("cacheWrite") or 0)
        out = int(u.get("output") or 0)
        read = int(u.get("cacheRead") or 0)
        key = rec.get("id")
    else:  # claudecode
        if rec.get("type") != "assistant" or not m.get("usage"):
            return None
        provider = "claude"
        u = m["usage"]
        prompt = int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        read = int(u.get("cache_read_input_tokens") or 0)
        key = m.get("id")
    if not provider:
        return None
    now = (prompt + out, read, prompt)
    seen = state.setdefault("seen", {})
    before = seen.get(key, (0, 0, 0)) if key else (0, 0, 0)
    if key:
        seen[key] = tuple(max(a, b) for a, b in zip(now, before))
    n, r, p = (max(0, a - b) for a, b in zip(now, before))
    return [(rec["timestamp"], provider, n, r, p, family(m.get("model")))]


def transcript_cost(kind, rec):
    """(timestamp, provider, USD) of one omp reply that reports its price (usage.cost.total), else None. omp prices every
    reply from the model's list price: real money for OpenRouter, an API-equivalent estimate for Claude and Copilot (flat
    subscriptions). Claude Code transcripts and the Paperclip run streams carry no cost."""
    m = rec.get("message") or {}
    if kind != "omp" or rec.get("type") != "message" or m.get("role") != "assistant":
        return None
    provider = OMP_PROVIDERS.get(m.get("provider"))
    cost = ((m.get("usage") or {}).get("cost") or {}).get("total")
    if not provider or not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
        return None
    return rec["timestamp"], provider, float(cost)


def event_tokens(provider, ev, state):
    """Tokens this event adds: the growth of the run's context since the previous reading. The first reading of a run
    is only the baseline (system prompt, instructions, resumed history, mostly cache reads) and counts nothing.
    `state` is the per-file memory."""
    used = context_size(provider, ev)
    if used is None:
        return 0
    last = state.get("used")
    state["used"] = used
    if last is None:
        return 0
    return max(0, used - last)  # a drop = context compaction: nothing consumed


class LiveTokens:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = deque()   # (epoch, provider, tokens, cache read, prompt, model family)
        self.costs = deque()    # (epoch, provider, USD) of every omp reply that reports its price
        self.agents = {}        # agent id -> (provider, model family)
        self.files = {}         # path -> {"kind", "provider", "family", "offset", "state", "mtime"}
        self.error = None       # last problem worth showing (logs unreachable ...)
        self._agents_at = self._discover_at = 0.0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    # ---- reading
    def _root(self):
        root = read_settings().get("runLogsDir") or RUN_LOGS
        if not root:
            raise RuntimeError("Paperclip run logs not found (no WSL distro with Paperclip detected)")
        return root

    def _refresh_agents(self):
        def get(path):
            return http_util.request_json(URL + path, timeout=10)
        agents = {}
        for c in get("/api/companies"):
            for a in get(f"/api/companies/{c['id']}/agents"):
                p = provider_of(a)
                if p:
                    agents[a["id"]] = (p, family((a.get("adapterConfig") or {}).get("model")))
        self.agents = agents

    def _discover(self, root, now):
        """Follow every log file of a counted agent that was written recently."""
        for company in os.scandir(root):
            if not company.is_dir():
                continue
            for agent in os.scandir(company.path):
                provider = self.agents.get(agent.name)
                if not provider:
                    continue
                for f in os.scandir(agent.path):
                    if f.name.endswith(".ndjson") and f.path not in self.files and now - f.stat().st_mtime < ACTIVE_S:
                        self._follow_file(f.path, f.stat(), "paperclip", provider)

    def _discover_transcripts(self, kind, root, now):
        """omp sessions (<root>/<project>/*.jsonl, plus subagent transcripts in <root>/<project>/<session>/*.jsonl) and
        Claude Code projects (<root>/<project>/*.jsonl)."""
        if not os.path.isdir(root):
            return
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if name.endswith(".jsonl"):
                    path = os.path.join(dirpath, name)
                    if path not in self.files:
                        st = os.stat(path)
                        if now - st.st_mtime < ACTIVE_S:
                            self._follow_file(path, st, kind, (None, None))

    def _follow_file(self, path, st, kind, source):
        """Start following a file; a long one is read from its last BACKFILL_BYTES only. source: (provider, model
        family) of a Paperclip run log; transcripts name both per line."""
        start = max(0, st.st_size - BACKFILL_BYTES)
        self.files[path] = {"kind": kind, "provider": source[0], "family": source[1], "offset": start, "mtime": st.st_mtime,
                            "state": {}, "skip_partial": start > 0}

    def _follow(self, now):
        for path, fs in list(self.files.items()):
            try:
                st = os.stat(path)
            except OSError:
                del self.files[path]
                continue
            if st.st_size > fs["offset"]:
                with open(path, "rb") as f:
                    f.seek(fs["offset"])
                    data = f.read(st.st_size - fs["offset"])
                end = data.rfind(b"\n") + 1  # a half-written last line is read next time
                lines = data[:end].decode("utf-8", "replace").splitlines()
                if fs.pop("skip_partial", False) and lines:
                    lines = lines[1:]  # started inside a line
                fs["offset"] += end
                fs["mtime"] = st.st_mtime
                self._ingest(fs, lines)
            if now - fs["mtime"] > ACTIVE_S:
                del self.files[path]

    def _ingest(self, fs, lines):
        out, spent = [], []
        for ln in lines:
            try:
                rec = json.loads(ln)
                if fs["kind"] == "paperclip":
                    ev = parse_event(rec.get("chunk", ""))
                    # the run streams carry context growth only, no cache split: they stay out of the hit rate
                    got = None if ev is None else [(rec["ts"], fs["provider"], event_tokens(fs["provider"], ev, fs["state"]), 0, 0,
                                                    fs["family"])]
                else:
                    got = transcript_tokens(fs["kind"], rec, fs["state"])
                    c = transcript_cost(fs["kind"], rec)
                    if c:
                        spent.append((datetime.fromisoformat(c[0].replace("Z", "+00:00")).timestamp(), c[1], c[2]))
                for ts, provider, n, read, prompt, fam in got or ():
                    if n or read:
                        out.append((datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp(), provider, n, read, prompt, fam))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        if out or spent:
            with self.lock:
                self.events.extend(out)
                self.costs.extend(spent)

    def _loop(self):
        while True:
            now = time.time()
            problems = []

            def step(what, fn, *a):  # one source being unreachable (WSL down ...) must not stop the others
                try:
                    fn(*a)
                except Exception as e:
                    problems.append(f"{what}: {type(e).__name__}: {e}"[:120])
            with_pc = paperclip_enabled()
            if with_pc and now - self._agents_at >= AGENTS_S:
                self._agents_at = now
                step("paperclip agents", self._refresh_agents)
            if now - self._discover_at >= DISCOVER_S:
                self._discover_at = now
                if with_pc:
                    step("paperclip logs", lambda: self._discover(self._root(), now))
                cfg = read_settings()
                for kind, default in TRANSCRIPTS.items():
                    step(kind, self._discover_transcripts, kind, cfg.get(kind + "Dir") or default, now)
            step("follow", self._follow, now)
            self.error = problems[0] if problems else None
            with self.lock:
                self.events = deque(e for e in self.events if e[0] >= now - KEEP_S - LEAD_S)  # backfilled files arrive unordered
                self.costs = deque(c for c in self.costs if c[0] >= now - KEEP_S)
            time.sleep(POLL_S)

    # ---- queries
    def series(self, window_s=300, step_s=5, avg_s=30, now=None, sub=1):
        """{"t": [epochs], provider: [tokens/min at each t], "models": {provider: {family: [tokens/min at each t]}}}
        for the window ending at `now`; a provider's curve is the sum of its model families (only families with tokens
        in the window are listed). The rate is a trailing average over avg_s (events are per turn, single points would
        be spikes) that ramps in and out over another avg_s / 2, so a burst rises and falls as a slope instead of a
        step. It is taken on a fixed absolute time grid (multiples of step_s, reaching past both window edges) and
        rounded into a curve by a quadratic B-spline through those grid values, drawn as straight pieces between the
        multiples of step_s / sub. That line is a function of absolute time alone: the window only cuts a piece out of
        it (`t` is the window start, the multiples of step_s / sub inside it, and `now`; both edges lie on the line),
        so a redraw slides the picture and nothing changes shape where data enters or leaves. Right of the newest grid
        point the grid values are those the rate will have if no more tokens arrive."""
        now = now or time.time()
        start = now - window_s
        with self.lock:
            ev = sorted(e for e in self.events if e[0] > start - 1.5 * avg_s - 3 * step_s)
        fine = step_s / sub
        knots = [k * fine for k in range(math.floor(start / fine), math.ceil(now / fine) + 1)]  # corners of the line: one at or outside each edge
        cut = (start - knots[0]) / fine, (knots[-1] - now) / fine  # how far into the first / last piece the window edges lie
        first = math.floor(knots[0] / step_s + 0.5) - 1  # grid index of the first value the spline needs
        grid = [k * step_s for k in range(first, math.floor(knots[-1] / step_s + 0.5) + 2)]
        pieces = []  # per knot: (i, weights of grid values i - 1 and i + 1): the spline piece around grid point i, the nearest
        for t in knots:
            k = math.floor(t / step_s + 0.5)
            u = t / step_s + 0.5 - k
            pieces.append((k - first, (1 - u) ** 2 / 2, u * u / 2))
        times = [start] + knots[1:-1] + [now]
        out = {"t": times, "models": {}}
        shifts = [j * avg_s / 2 / RAMP_STEPS for j in range(RAMP_STEPS + 1)]
        for p in PROVIDERS:
            fams = {}
            for e in ev:
                if e[1] == p and e[2]:
                    ts, cum = fams.setdefault(e[5], ([], []))
                    ts.append(e[0])
                    cum.append((cum[-1] if cum else 0) + e[2])

            def curve(ts, cum):
                def upto(x):  # tokens of events at or before x
                    i = bisect.bisect_right(ts, x)
                    return cum[i - 1] if i else 0
                v = [sum(upto(g - s) - upto(g - s - avg_s) for s in shifts) * 60.0 / avg_s / len(shifts) for g in grid]
                y = [a * v[i - 1] + (1 - a - b) * v[i] + b * v[i + 1] for i, a, b in pieces]
                y[0], y[-1] = y[0] + (y[1] - y[0]) * cut[0], y[-1] + (y[-2] - y[-1]) * cut[1]
                return y
            out["models"][p] = {fam: curve(*fams[fam]) for fam in sorted(fams)}
            out[p] = [sum(v) for v in zip(*out["models"][p].values())] or [0.0] * len(times)
        return out

    def rate(self, provider, avg_s=30):
        """Tokens/min over the trailing avg_s seconds."""
        now = time.time()
        with self.lock:
            return sum(e[2] for e in self.events if e[1] == provider and e[0] > now - avg_s) * 60.0 / avg_s

    def cents_per_s(self, providers=PROVIDERS, window_s=60, now=None):
        """Spend in US cents per second of `providers` over the trailing window_s seconds (omp replies only)."""
        now = now or time.time()
        with self.lock:
            usd = sum(c[2] for c in self.costs if c[1] in providers and now - window_s < c[0] <= now)
        return usd * 100.0 / window_s

    def active(self, provider, window_s=24 * 3600):
        """True if the provider consumed tokens within the last window_s seconds."""
        since = time.time() - window_s
        with self.lock:
            return any(e[1] == provider and e[0] > since and e[2] > 0 for e in self.events)

    def cache_hit_rate(self, provider, window_s=300, now=None):
        """Share of the prompt that was served from the cache in the last window_s: cache_read / (cache_read + input +
        cache_write), or None without a reading. Only sources with a cache split count (omp, Claude Code)."""
        now = now or time.time()
        with self.lock:
            read = prompt = 0
            for e in self.events:
                if e[1] == provider and e[0] > now - window_s:
                    read += e[3]
                    prompt += e[4]
        total = read + prompt
        return read / total if total else None
