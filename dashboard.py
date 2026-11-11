"""Pacer dashboard: the single UI for the Claude governor / tray.

Pace chart (5h / 7d tabs: usage vs. plan with a projection to the limit), live status, throughput and all
settings (Full budget, pause / resume limits) in one window. Data comes from the governor's status/log files and tray-state.json (written
by the tray); changes go back through claude-governor-settings.json plus a flag file that makes the tray run
the governor immediately. Launched by the tray as its own process: Tk must own the main thread of a process
(freeing Tk objects on another thread aborts the interpreter, which once took the tray down with it).
"""
import ctypes, json, math, os, re, subprocess, sys, threading, time, traceback, webbrowser
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta

import pacer_config

APP_ID = "Pacer.Dashboard"
TITLE = "Pacer"
UPDATE_FILE = os.path.join(pacer_config.DIR, "update-check.json")
UPDATE_EVERY_S = 6 * 3600
UPDATE_RETRY_S = 10 * 60  # a failed update check (offline, DNS) is repeated after this
UPDATE_RUN = os.path.join(pacer_config.DIR, "update-run.json")  # progress of updater.py
_mutex = None  # single-instance mutex, kept referenced for the life of the process (see main())
_k32, _u32 = ctypes.windll.kernel32, ctypes.windll.user32  # Win32 calls: single instance, dark title bar

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tray_icon  # noqa: E402
from paperclip_common import (CFG, DIR, LOGS, URL, CLAUDE_EXE, SPAN_S, CHECK_FLAG, read_status, migrate_settings,  # noqa: E402
                              read_settings, write_settings, full_budget, provider_settings, keep_awake, sleep_when_idle,
                              fmt_min, evaluate, paperclip_enabled)
import copilot_monitor, openrouter_monitor, throttle  # noqa: E402
import autostart  # noqa: E402
import i18n  # noqa: E402
from pacer_config import wsl_cmd  # noqa: E402
import http_util, logutil  # noqa: E402
from PIL import ImageTk  # noqa: E402
from live_tokens import LiveTokens, PROVIDERS, KEEP_S as LIVE_KEEP_S  # noqa: E402
from live_chart import draw_live, fmt_rate, PLOT_L, PLOT_R  # noqa: E402
from dashboard_data import (LAST, history, past_resets, throughput, kick_token_dump, tokens_between, fmt_tok, fmt_spend, usd_eur, ago, when,  # noqa: E402
                            claude_cli_found, last_governor_error, fault_reason, claude_login, tray_state,
                            quota_history, quota_moved, COPILOT_HIST, OPENROUTER_HIST, pct_text, copilot_login, openrouter_key,
                            quota_spark)
from ui_widgets import (BG, PANEL, FG, MUTED, BTN_HOVER, GOOD, WARN, BAD, BLUE, AREA, GRID, VIOLET, HEX, BORDER,  # noqa: E402
                        Switch, Tab, button, split_button, stepper)
from winpos import save_position, saved_size, saved_position, work_area  # noqa: E402

LOGGER = logutil.get_logger("dashboard", "tray-ui.log")  # the same log file as the tray's

PAD, GAP = 16, 12            # outer margin, gap between panels (half of GAP on each side of a seam)
PAD_COMPACT = 8              # outer margin and chart panel padding of the narrow layout
WIN_W0, CHART_H = 660, 250   # default window width and pace chart height (both grow/shrink with the window)
CHART_MIN_H = 110            # smallest chart height; defines the minimum window height
COL_MIN = 270                # smallest width of one of the two columns
WIDE_MIN = 2 * COL_MIN + 4 * PAD  # narrower than this: compact layout (tabs as tiles above the chart, icon buttons, one column)
LIVE_LAYOUT = [("claude", "Claude", BLUE), ("copilot", "Copilot", VIOLET), ("openrouter", "OpenRouter", "#4ec9b0")]
COPILOT_ROWS = ("Copilot agents", "Copilot login", "Copilot usage", "Copilot resets")
OPENROUTER_ROWS = ("OpenRouter agents", "OpenRouter key", "OpenRouter usage", "OpenRouter resets")
SYMBOLS = {"OK": "\u2713", "healthy": "\u2713", "up to date": "\u2713", "FAILED": "\u2717", "DOWN": "\u2717",
           "Too fast": "\u25b2", "Too slow": "\u25bc", "At limit": "\u25a0", "Too early to tell": "\u2026",
           "unlimited": "\u221e", "unknown": "?", "not run yet": "\u2013", "tray not running": "\u2013",
           "running": "\u2713", "paused": "\u23f8", "draining": "\u25bc"}
PAPERCLIP_ROWS = ("Claude agents", "Copilot agents", "OpenRouter agents", "Watchdog", "Server", "Paperclip")
LIVE_PAL = {"fg": FG, "muted": MUTED, "grid": GRID, "bg": PANEL,
           "tint": {"claude": AREA, "copilot": "#352e4a", "openrouter": "#2c4a45"}, "reset5h": WARN, "reset7d": GOOD}
LIVE_LEVELS = ((60, 1, 5), (300, 5, 30), (3600, 30, 120), (6 * 3600, 120, 600), (24 * 3600, 300, 1800))  # window, point step, averaging (s)
LIVE_CHIPS = {"5m": 300, "1h": 3600, "24h": 24 * 3600}  # presets; zoom reaches every width between 30 s and 24 h, the data level follows it
LIVE_MIN_S = 30
REFRESH_MS = 5000


def verdict(p, usage, thr, span_s):
    """Colour for one window's pace; the rule lives in tray_icon.window_verdict() so tabs and glow agree."""
    return HEX[tray_icon.window_verdict(p, usage, span_s)]


CLAUDE_SETUP_URL = "https://code.claude.com/docs/en/setup"  # official install + sign-in instructions of Claude Code


def countdown_text(reset):
    """'reset in 37m' while the ISO time `reset` is at most an hour away (minutes rounded up), else ''."""
    try:
        left = datetime.fromisoformat(reset).timestamp() - time.time()
    except (TypeError, ValueError):
        return ""
    return f"reset in {fmt_min(math.ceil(left / 60))}" if 0 < left <= 3600 else ""


class Dashboard:
    def __init__(self):
        root = self.root = tk.Tk()
        root.title(TITLE)
        root.configure(bg=BG)
        root.resizable(True, True)
        self.dark_titlebar()
        self.pinned = self.saved_choice("pinnedTab", ("live", "5h", "7d", "cop", "or"))  # tab picked by the user (remembered); None = follow the limiting window
        self.upd_busy = False  # paperclipai update check running in a worker thread
        self._job = None    # pending refresh timer
        self._geo_job = None  # pending save of the window geometry
        self.ka_var = tk.BooleanVar()
        self.si_var = tk.BooleanVar()
        self.split_var = tk.BooleanVar()
        self.auto_var = tk.BooleanVar()
        self.lang_var = tk.BooleanVar()
        self.editing = False  # a setting was changed but not applied yet: refresh must not overwrite it
        self.pc_on = True  # Paperclip parts shown; set_paperclip_visible() follows paperclip_enabled()
        self.vals, self.lbls = {}, {}
        self.compact, self.details = False, True  # narrow layout; Status / Services panels shown
        self.tabs_only, self.normal_size = False, None  # tabs-only mode (double click on a tab); size of the normal window meanwhile
        self.page_open, self.page_h = False, 0  # settings page covers the window; window height before it opened
        root.rowconfigure(0, weight=1)  # extra window height goes to the chart

        def panel(title=None, parent=None, card=False):  # placed by apply_layout(); card: bordered, divider under the title
            f = tk.Frame(parent or root, bg=PANEL, padx=PAD, pady=PAD - 2,
                         **({"highlightbackground": BORDER, "highlightthickness": 1} if card else {}))
            if title:
                tk.Label(f, text=title.upper(), bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold")).grid(
                    row=0, column=0, columnspan=2, sticky="w", pady=(0, 10 if card else 6))
                if card:
                    tk.Frame(f, bg=BORDER, height=1).grid(row=0, column=0, columnspan=2, sticky="sew", pady=(0, 6))
            return f

        # pace: tabs stacked on the left (narrow window: as a row of tiles on top), one chart for the selected window
        pace = self.pace = panel(card=True)
        pace.columnconfigure(1, weight=1)
        pace.rowconfigure(2, weight=1)
        side = tk.Frame(pace, bg=PANEL)
        self.vdiv = tk.Frame(pace, bg=BORDER, width=1)  # wide layout: divider between the tabs and the graph
        self.tabs, self.groups, self.side, self.cop_on, self.or_on = {}, [], side, True, True
        self.idle = set()  # tab groups hidden because their provider used nothing for 24 h
        self.or_tab = True  # OPENROUTER tab shown: key present and it has a spend limit
        for title, wins in (("LIVE", (("live", "Tokens"),)), ("CLAUDE", (("5h", "5h"), ("7d", "7d"))), ("COPILOT", (("cop", "Month"),)),
                            ("OPENROUTER", (("or", "Limit"),))):
            hdr = tk.Label(side, text=title, bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold"), anchor="w", padx=0, pady=0)
            for win, label in wins:
                self.tabs[win] = Tab(side, label, lambda w=win: self.pick(w), self.unpin, self.toggle_tabs_only)
            self.groups.append((title, hdr, [w for w, _l in wins]))
        self.place_side()

        self.big_box = tk.Frame(pace, bg=PANEL)  # the big percentage, the spend rate and the reset countdown below it
        self.big = tk.Label(self.big_box, bg=PANEL, fg=FG, font=("Segoe UI", 26, "bold"), width=5, anchor="e", pady=0)
        self.big.pack(anchor="e")
        self.cost = tk.Label(self.big_box, text=" ", bg=PANEL, fg=MUTED, font=("Segoe UI", 9), anchor="e", pady=0)  # spend rate, blank while zero
        self.cost.pack(anchor="e")
        self.sub = tk.Label(self.big_box, text=" ", bg=PANEL, fg=GOOD, font=("Segoe UI", 9), anchor="e")  # a blank keeps its row
        self.sub.pack(anchor="e")
        self.cost.bind("<Enter>", lambda e: self.show_tip(e, self.cost_tip()))
        self.cost.bind("<Leave>", self.hide_tip)
        self.chart = tk.Canvas(pace, width=1, height=CHART_MIN_H, bg=PANEL, highlightthickness=0)
        self.chart_args = None
        self.zoom, self.plot_x = None, (0, 1)  # visible share (lo, hi) of the time axis, plot edges (px)
        self.zoom_btn = button(self.pace, "Reset zoom", self.reset_zoom, icon="\uE7A7", pad=(8, 0))  # thin; placed on the top panel while zoomed
        self.zoom_pos = None  # where the button is placed now (None = hidden): it is touched only when that changes
        self.chart.bind("<Configure>", lambda _e: self.redraw_chart())
        self.chart.bind("<Button-1>", self.chart_click)
        self.chart.bind("<Double-Button-1>", self.chart_double)
        root.bind("<MouseWheel>", self.chart_wheel)
        root.bind("<Shift-MouseWheel>", self.chart_wheel_pan)
        self.chart.bind("<B1-Motion>", self.chart_drag)
        self.chart.bind("<Motion>", self.chart_motion)
        self.chart.bind("<Leave>", self.chart_leave)
        self.drag = None  # (pointer x, zoom, live width, live end) at the start of a drag on the chart
        self.live_preset = self.saved_choice("livePreset", tuple(LIVE_CHIPS)) or "5m"  # Live chart: preset (remembered), shown span (s), end (None = now)
        self.live_width, self.live_end = LIVE_CHIPS[self.live_preset], None
        self.window_ends, self.live_hits, self.live_n = {}, [], 0
        self.hover_t, self.live_t0, self.live_t1 = None, None, None  # Live chart: the epoch under the pointer, the drawn window
        # fault / stale-data message: left of the big percentage, so it never changes the window height
        self.headline = tk.Label(pace, bg=PANEL, fg=FG, font=("Segoe UI", 9), anchor="w", justify="left", wraplength=200)
        self.hl_chars = 100  # two lines of text fit beside the percentage; recomputed when the window is resized

        def fit_headline(_e):
            avail = pace.winfo_width() - 2 * int(pace.cget("padx"))
            if not self.compact:
                avail -= side.winfo_reqwidth() + self.big_box.winfo_reqwidth() + 4 * GAP
            self.headline.config(wraplength=max(60, avail))
            self.hl_chars = max(8, int(2 * avail / 5.8) - 2)
            self.render_headline()
        pace.bind("<Configure>", fit_headline)
        self.f_small = tkfont.Font(family="Segoe UI", size=8)

        # status (health, throughput, resets) | accounts (logins) on the right, settings below them
        def rows(frame, keys):
            """Label / value rows from grid row 1; a key starting with '#' is a section header (hidden with its provider)."""
            for r, key in enumerate(keys, 1):
                if key.startswith("#"):
                    hdr = self.heads[key[1:]] = tk.Label(frame, text=key[1:], bg=PANEL, fg=MUTED, font=("Segoe UI", 8, "bold"))
                    hdr.grid(row=r, column=0, columnspan=2, sticky="w", pady=(0 if r == 1 else 14, 0))
                    hdr.sep = None if r == 1 else tk.Frame(frame, bg=BORDER, height=1)  # divider above every section but the first
                    if hdr.sep:
                        hdr.sep.grid(row=r, column=0, columnspan=2, sticky="new", pady=(5, 0))
                    continue
                self.lbls[key] = tk.Label(frame, text=key, bg=PANEL, fg=MUTED, font=("Segoe UI", 9))
                self.lbls[key].grid(row=r, column=0, sticky="w", pady=2)
                self.vals[key] = tk.Label(frame, bg=PANEL, fg=FG, font=("Segoe UI", 9), anchor="w")
                self.vals[key].grid(row=r, column=1, sticky="w", padx=(GAP, 0), pady=2)

        self.heads = {}
        st = self.st = panel("Status", card=True)
        st.columnconfigure(1, weight=1)
        rows(st, ("#CLAUDE", "Claude login", "Throughput 5h", "Throughput 7d", "Resets 5h", "Resets 7d",
                  "#COPILOT", "Copilot login", "Copilot usage", "Copilot resets",
                  "#OPENROUTER", "OpenRouter key", "OpenRouter usage", "OpenRouter resets"))
        svc = self.svc = panel("Services", card=True)
        svc.columnconfigure(1, weight=1)
        rows(svc, ("Governor", "Watchdog", "Server", "Paperclip"))
        # the Paperclip row holds either the status text or, while an update is available, the update button
        self.vals["Paperclip"].destroy()
        pc = tk.Frame(svc, bg=PANEL)
        pc.grid(row=4, column=1, sticky="w", padx=(GAP, 0), pady=2)
        self.pc_frame = pc
        self.vals["Paperclip"] = tk.Label(pc, bg=PANEL, fg=FG, font=("Segoe UI", 9), anchor="w")
        self.vals["Paperclip"].grid(row=0, column=0, sticky="w")
        self.upd_btn = button(pc, "Update Paperclip", self.start_update, pad=(10, 2))
        self.upd_btn.grid(row=0, column=0, sticky="w")
        self.upd_btn.grid_remove()
        self.upd_armed, self.upd_offer = 0.0, None  # confirm-click time; label currently offered on the button
        ag = self.agents = panel("Agents", card=True)  # run state of each provider's agents, below the services
        ag.columnconfigure(1, weight=1)
        rows(ag, ("Claude agents", "Copilot agents", "OpenRouter agents"))
        self.align_details()

        self.tips, self.tip, self.sym_tip = {}, None, {}  # hover texts per Status row; the spelled-out status behind a symbol
        for k in ("Governor", "Watchdog", "Server", "Paperclip", "Throughput 5h", "Throughput 7d", "Claude login", "Copilot login",
                  "OpenRouter key", "Copilot usage", "OpenRouter usage", "Resets 5h", "Resets 7d", "Copilot resets", "OpenRouter resets",
                  "Claude agents", "Copilot agents", "OpenRouter agents"):
            self.vals[k].bind("<Enter>", lambda e, k=k: self.show_tip(e, self.tip_text(k)))
            self.vals[k].bind("<Leave>", self.hide_tip)
        for w in [self.upd_btn] + list(self.upd_btn.winfo_children()) + list(self.upd_btn.winfo_children()[0].winfo_children()):
            w.bind("<Enter>", lambda e: self.show_tip(e, self.tip_text("Paperclip")))
            w.bind("<Leave>", self.hide_tip)


        # settings page: covers the whole window while open. One card of throttle settings per provider, one for the PC.
        page = self.page = tk.Frame(root, bg=BG)
        page.columnconfigure(0, weight=1, uniform="card")
        head = self.page_head = tk.Frame(page, bg=BG)
        back = button(head, "Back", self.close_settings, icon="\uE72B")
        back.pack(side="left")
        tk.Label(head, text="SETTINGS", bg=BG, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(side="left", padx=GAP)
        self.apply_btn = button(head, "Apply", self.apply_limits, primary=True, icon="\uE74E")
        self.prov = {}  # provider -> its setting variables, limit rows (hidden under full budget), card and saved settings
        for name in throttle.PROVIDERS:
            card = panel(throttle.LABEL[name], page)
            card.columnconfigure(0, weight=1)
            pv = self.prov[name] = {"fb": tk.BooleanVar(), "pace": tk.BooleanVar(), "pause": tk.StringVar(), "resume": tk.StringVar(),
                                    "rows": [], "card": card, "saved": throttle.DEFAULTS[name]}
            fb = Switch(card, "Use full budget", pv["fb"], self.setting_changed)
            fb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=3)
            fb.bind("<Enter>", lambda e: self.show_tip(e, "The only limit is the real one: agents pause at 100%."))
            fb.bind("<Leave>", self.hide_tip)
            for r, (label, key) in enumerate((("Pause agents at (%)", "pause"), ("Resume below (%)", "resume")), 2):
                lb = tk.Label(card, text=label, bg=PANEL, fg=FG, font=("Segoe UI", 9))
                lb.grid(row=r, column=0, sticky="w", pady=2)
                sb = stepper(card, pv[key], self.setting_changed)
                sb.grid(row=r, column=1, sticky="e", pady=2)
                pv["rows"] += [lb, sb]
                pv[key].trace_add("write", lambda *_a: self.update_apply())
            pace_sw = Switch(card, "Pause when too fast", pv["pace"], self.setting_changed)
            pace_sw.grid(row=4, column=0, columnspan=2, sticky="ew", pady=3)
            pace_sw.bind("<Enter>", lambda e: self.show_tip(e, "Also pauses the agents while the usage runs clearly ahead of the plan (red), until it is back on the plan line."))
            pace_sw.bind("<Leave>", self.hide_tip)
        gen = self.gen = panel("General", page)
        gen.columnconfigure(0, weight=1)
        ka = Switch(gen, "Keep PC awake", self.ka_var, self.toggle_ka)
        ka.grid(row=1, column=0, sticky="ew", pady=3)
        ka.bind("<Enter>", lambda e: self.show_tip(e, "Prevents Windows from sleeping while the Pacer tray runs. The screen may still turn off and the PC may lock."))
        ka.bind("<Leave>", self.hide_tip)
        self.si = Switch(gen, "Sleep when Paperclip is idle", self.si_var, self.toggle_si)
        self.si.grid(row=2, column=0, sticky="ew", pady=3)
        self.si.bind("<Enter>", lambda e: self.show_tip(e, "The PC may sleep 10 min after the last agent run ended (no agent running, no update in progress). Needs 'Keep PC awake'."))
        self.si.bind("<Leave>", self.hide_tip)
        split = Switch(gen, "Split token graph by model", self.split_var, self.toggle_split)
        split.grid(row=3, column=0, sticky="ew", pady=3)
        split.bind("<Enter>", lambda e: self.show_tip(e, "The Tokens graph shows one band per model family (Opus, Sonnet, Haiku ...) instead of one area per provider."))
        split.bind("<Leave>", self.hide_tip)
        auto = Switch(gen, "Start with Windows", self.auto_var, self.toggle_autostart)
        auto.grid(row=4, column=0, sticky="ew", pady=3)
        lang = Switch(gen, "Deutsch", self.lang_var, self.toggle_language)  # on = German UI, off = English
        lang.grid(row=5, column=0, sticky="ew", pady=3)
        self.msg = tk.Label(page, bg=BG, fg=MUTED, font=("Segoe UI", 9), wraplength=300, justify="left", anchor="w")

        # bottom row: frequent actions as buttons, sign-ins only while a login needs them, rarely used ones behind "..."
        bar = self.bar = tk.Frame(root, bg=BG)
        self.restart_armed = 0.0
        self.check_req = None  # epoch of a pending "Check now"; shown as a suffix on the Governor value
        actions = (("Refresh usage", self.check_now, "\uE72C"),)
        self.bar_order = []
        for text, cmd, ic in actions:
            self.bar_order.append(button(bar, text, cmd, icon=ic))
        # combo: the main part opens Paperclip (or confirms a pending restart), the arrow part lists the Paperclip actions
        self.open_btn = split_button(bar, "Open Paperclip", self.open_paperclip, lambda: self.open_menu(self.open_btn, self.paperclip_actions()),
                                     icon="\uE774")
        self.bar_order.append(self.open_btn)
        self.signin_btn = button(bar, "Claude sign-in", self.claude_signin, icon="\uE77B")
        self.cop_btn = button(bar, "Copilot sign-in", self.copilot_signin, icon="\uE77B")
        self.signin_btn.shown = self.cop_btn.shown = False
        self.details_btn = button(bar, "Status and services", self.toggle_details, icon="\uE946")
        self.settings_btn = button(bar, "Settings", self.open_settings, icon="\uE713")
        more = button(bar, "\u2026", lambda: None)
        for b in (self.details_btn, self.settings_btn):
            b.set_icon_only(True)
            b.compact = True
        more.compact = True  # the compact ones keep their own width; the action buttons share the rest
        self.bar_order += [self.signin_btn, self.cop_btn, self.details_btn, self.settings_btn, more]
        self.place_bar()
        for b in self.bar_order[:-1]:  # narrow window: the buttons are icons only, their text moves into a tooltip
            for w in b.parts:
                w.bind("<Enter>", lambda e, b=b: self.show_tip(e, b.label()) if self.compact or getattr(b, "compact", False) else None, add="+")
                w.bind("<Leave>", self.hide_tip, add="+")
        self.menu = None

        def show_menu(_e):
            self.open_menu(more, self.menu_items())
        for w in [more] + list(more.winfo_children()) + list(more.winfo_children()[0].winfo_children()):
            w.bind("<Button-1>", show_menu)

        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _e: self.close_settings() if self.page_open else self.close())
        root.bind("<Return>", lambda _e: self.apply_limits())
        self.sel = None  # tab shown in the chart ("5h" / "7d" / "cop" / "live")
        self.live = LiveTokens()
        self.live.start()
        self.refresh()
        self.live_tick()
        # the narrow layout defines the minimum width; the window then starts in the layout its width asks for
        self.compact = True
        self.apply_layout()
        root.update_idletasks()
        self.min_w = max(max(w.winfo_reqwidth() for w in (pace, bar)) + 2 * PAD_COMPACT, page.winfo_reqwidth())
        root.pack_propagate(False)
        root.grid_propagate(False)  # text length must never resize the window; the user does that
        size = saved_size()
        w = max(size[0] if size else WIN_W0, self.min_w)
        self.compact = w < WIDE_MIN
        self.details = self.details_pref()
        self.apply_layout()
        min_h = self.fit_minsize()
        h = max(size[1] if size else min_h + CHART_H - CHART_MIN_H, min_h)
        self.normal_size = (w, h)
        if read_settings().get("tabsOnly"):  # reopened as the tab column
            self.tabs_only = True
            self.set_resizable(True, False)
            self.apply_layout()
            w, h = self.tabs_size(read_settings().get("tabsOnlyWidth"))
        pos = saved_position(w, h)
        x, y = pos or ((root.winfo_screenwidth() - w) // 2, (root.winfo_screenheight() - h) // 4)
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.update()  # drop the <Configure> events of the sizes the window had while it was built
        root.bind("<Configure>", self.on_resize)

    # ---- responsive layout
    def apply_layout(self):
        """Place everything for the current mode. Wide: tabs left of the chart, panels in two columns, text buttons.
        Compact (narrow window): tabs as a row of tiles above the chart, panels in one column, icon-only buttons."""
        if self.tabs_only:
            self.apply_tabs_only()
            return
        c, root, pace, half = self.compact, self.root, self.pace, GAP // 2
        m = PAD_COMPACT if c else PAD
        for col in (0, 1):
            root.columnconfigure(col, weight=1, uniform="col", minsize=0 if c else COL_MIN)
        pace.config(padx=m if c else PAD // 2, pady=m if c else PAD - 2, highlightthickness=1)
        pace.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=m, pady=(m, half))
        if c:
            self.side.grid(row=0, column=0, columnspan=3, rowspan=1, sticky="ew", padx=0)
            self.big_box.grid_remove()
            self.vdiv.grid_remove()
            self.chart.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(m, 0))
        else:
            self.side.grid(row=0, column=0, columnspan=1, rowspan=3, sticky="nw", padx=(0, GAP))
            self.vdiv.grid(row=0, column=0, columnspan=1, rowspan=3, sticky="nse", padx=(0, GAP // 2))  # midway between tabs and graph
            self.big_box.grid(row=1, column=2, sticky="e")
            self.headline.grid(row=1, column=1, columnspan=1, sticky="w", pady=0)
            self.chart.grid(row=2, column=1, columnspan=2, sticky="nsew", pady=(GAP, 0))
        self.place_side()
        self.render_headline()
        self.place_details()
        self.bar.grid(row=4, column=0, columnspan=2, sticky="ew", padx=m, pady=(half, m))
        for b in self.bar_order:
            if not getattr(b, "compact", False):
                b.set_icon_only(c)
        self.place_page()

    def place_details(self):
        """Status panel (left) and Services + Agents panels (right; stacked in one column when compact). The Agents panel
        belongs to Paperclip."""
        m = PAD_COMPACT if self.compact else PAD
        half = GAP // 2
        panels = [self.st, self.svc] + ([self.agents] if self.pc_on else [])
        for f in (self.st, self.svc, self.agents):
            f.grid_remove()
        if not self.details or self.tabs_only:
            return
        if self.compact:
            for r, f in enumerate(panels, 1):
                f.grid(row=r, column=0, columnspan=2, rowspan=1, sticky="nsew", padx=m, pady=half)
        else:
            self.st.grid(row=1, column=0, columnspan=1, rowspan=2, sticky="nsew", padx=(PAD, half), pady=half)
            self.svc.grid(row=1, column=1, columnspan=1, rowspan=1, sticky="nsew", padx=(half, PAD), pady=half)
            if self.pc_on:
                self.agents.grid(row=2, column=1, columnspan=1, rowspan=1, sticky="nsew", padx=(half, PAD), pady=half)

    def place_page(self):
        """Settings page: header (back, title, Apply while something is unsaved), the provider cards side by side (wide)
        or stacked (compact), the General card and the message line."""
        page, half = self.page, GAP // 2
        m = PAD_COMPACT if self.compact else PAD
        cards = [self.prov[n]["card"] for n in throttle.PROVIDERS
                 if self.pc_on and (n != "copilot" or self.cop_on) and (n != "openrouter" or self.or_on)]
        for w in [pv["card"] for pv in self.prov.values()] + [self.gen, self.msg]:
            w.grid_forget()
        wide = not self.compact and len(cards) > 1
        page.columnconfigure(1, weight=1 if wide else 0, uniform="card" if wide else "")
        self.page_head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=m, pady=(m, half))
        row = 1
        for i, card in enumerate(cards):
            if wide:
                card.grid(row=1, column=i, sticky="nsew", padx=(m, half) if i == 0 else (half, m), pady=half)
            else:
                card.grid(row=row, column=0, columnspan=2, sticky="nsew", padx=m, pady=half)
                row += 1
        row = max(row, 2)
        self.gen.grid(row=row, column=0, columnspan=2, sticky="nsew", padx=m, pady=half)
        self.msg.grid(row=row + 1, column=0, columnspan=2, sticky="w", padx=m + PAD, pady=(half, m))

    def details_height(self):
        """Window height the Status / Services panels take in the current layout (0 while hidden)."""
        if not self.details:
            return 0
        self.root.update_idletasks()
        st, svc = self.st.winfo_reqheight(), self.svc.winfo_reqheight()
        ag = self.agents.winfo_reqheight() + GAP if self.pc_on else 0
        return st + svc + ag + 2 * GAP if self.compact else max(st, svc + ag) + GAP

    def fit_minsize(self):
        """Minimum window size of the current layout: everything visible with the chart at its smallest (the whole
        settings page while that is open). Returns the height."""
        self.root.update_idletasks()
        if self.tabs_only:
            return self.tabs_size()[1]
        m = PAD_COMPACT if self.compact else PAD
        min_h = 2 * m + GAP + self.pace.winfo_reqheight() + self.bar.winfo_reqheight() + self.details_height()
        if self.page_open:
            min_h = self.page.winfo_reqheight()
        self.root.minsize(self.min_w, min_h)
        return min_h

    def apply_tabs_only(self):
        """Tabs-only mode: the window is just the tab column, flush to the window edges (no panels, chart or buttons)."""
        root, pace = self.root, self.pace
        for col in (0, 1):
            root.columnconfigure(col, weight=1, uniform="col", minsize=0)
        for w in (self.big_box, self.vdiv, self.headline, self.chart, self.bar, self.st, self.svc, self.agents):
            w.grid_remove()
        pace.config(padx=0, pady=0, highlightthickness=0)
        pace.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=0, pady=0)
        self.side.grid(row=0, column=0, columnspan=3, rowspan=1, sticky="nsew", padx=0)
        self.place_side()

    def tabs_size(self, width=None):
        """(w, h) of the tabs-only window: as tall as the tab column, at least as wide (wider = bigger graphs). Sets the
        minimum size."""
        self.root.update_idletasks()
        w, h = self.side.winfo_reqwidth(), self.side.winfo_reqheight()
        self.root.minsize(w, h)
        return max(width or 0, w), h

    def fit_tabs_only(self, width=None):
        """Window = the tab column (its height follows the tabs, e.g. when a group appears); position and width are kept."""
        w, h = self.tabs_size(width or self.root.winfo_width())
        self.root.geometry(f"{w}x{h}")

    def toggle_tabs_only(self):
        self.set_tabs_only(not self.tabs_only)

    def set_tabs_only(self, on):
        """Double click on a tab: the whole window turns into the tab column and back. The normal window keeps its size and
        returns at the same place; the mode is remembered across restarts."""
        if on == self.tabs_only or self.page_open:
            return
        root = self.root
        self.hide_tip()
        if on:
            self.store_geometry()  # position of the normal window
            self.normal_size = (root.winfo_width(), root.winfo_height())
        self.tabs_only = on
        write_settings(tabsOnly=on)
        self.set_resizable(True, not on)
        if on:
            self.apply_layout()
            w, h = self.tabs_size(read_settings().get("tabsOnlyWidth"))  # the tabs' own width unless widened before
            root.geometry(f"{w}x{h}")
            return
        w, h = self.normal_size or (WIN_W0, 0)
        w = max(w, self.min_w)
        self.compact = w < WIDE_MIN
        self.details = self.details_pref()
        self.apply_layout()
        h = max(h, self.fit_minsize())
        self.set_height(h, w)  # geometry is applied asynchronously: the width must be passed, not read back
        self.refresh()

    def details_pref(self):
        """Panels shown? Remembered per layout (settings detailsCompact / detailsWide); a narrow window starts without
        them, because the stacked column costs the chart most of the height."""
        return bool(read_settings().get("detailsCompact" if self.compact else "detailsWide", not self.compact))

    @staticmethod
    def saved_choice(key, allowed):
        """A UI choice remembered in the settings file; None when missing or no longer valid."""
        v = read_settings().get(key)
        return v if v in allowed else None

    def remember_geometry(self):
        """Saves size and position shortly after the last move or resize, so a dashboard that was killed (restart after an
        update, crash, logoff) reopens where it was, not only after a proper close."""
        if self._geo_job:
            self.root.after_cancel(self._geo_job)
        self._geo_job = self.root.after(1500, self.save_geometry)

    def store_geometry(self):
        """Writes the window position and size to the position file. In tabs-only mode the normal window keeps its size and
        the width of the tabs goes to the settings."""
        if self.tabs_only:
            write_settings(tabsOnlyWidth=self.root.winfo_width())
            save_position(self.root, size=self.normal_size)
        else:
            save_position(self.root)

    def save_geometry(self):
        self._geo_job = None
        if self.root.state() == "normal" and not self.page_open:  # not while minimised or while the settings page resizes it
            self.store_geometry()

    def on_resize(self, e):
        """Switch between the wide and the compact layout when the window width crosses WIDE_MIN; remember the geometry."""
        if e.widget is self.root:
            self.remember_geometry()
        compact = e.width < WIDE_MIN
        if e.widget is not self.root or self.tabs_only or compact == self.compact:
            return
        self.compact = compact
        self.details = self.details_pref()
        self.apply_layout()
        self.fit_minsize()

    def dark_titlebar(self):
        """Dark title bar (DWMWA_USE_IMMERSIVE_DARK_MODE). Tk builds a new native frame window whenever the window style
        changes (resizable() ...), which drops the attribute: call this after every such change."""
        try:
            self.root.update_idletasks()
            ctypes.windll.dwmapi.DwmSetWindowAttribute(_u32.GetParent(self.root.winfo_id()), 20, ctypes.byref(ctypes.c_int(1)), 4)
        except Exception:
            pass

    def set_resizable(self, width, height):
        self.root.resizable(width, height)
        self.dark_titlebar()

    def set_height(self, h, width=None):
        """Resize the window to height h (width kept, or `width`), moved up when it would reach below the screen."""
        root = self.root
        w = width or root.winfo_width()
        x, y = (int(v) for v in re.fullmatch(r"\d+x\d+\+(-?\d+)\+(-?\d+)", root.geometry()).groups())
        _l, top, _r, bottom = work_area(x + w // 2, y)
        h = min(h, bottom - top - 40)  # leave room for the title bar
        if y + h + 40 > bottom:
            root.geometry(f"{w}x{h}+{x}+{max(top, bottom - h - 40)}")
        else:
            root.geometry(f"{w}x{h}")

    def toggle_details(self):
        """Show / hide the panels; the window grows or shrinks by their height, so the chart keeps its size."""
        h = self.root.winfo_height()
        old = self.details_height()
        self.details = not self.details
        write_settings(**{"detailsCompact" if self.compact else "detailsWide": self.details})
        self.apply_layout()
        self.set_height(max(h + self.details_height() - old, self.fit_minsize()))

    def open_settings(self):
        """Settings page over the whole window; the window grows to fit it and gets its old height back on close."""
        if self.page_open:
            return
        self.page_open, self.page_h = True, self.root.winfo_height()
        self.page.place(x=0, y=0, relwidth=1, relheight=1)
        self.page.lift()
        self.set_height(max(self.page_h, self.fit_minsize()))

    def close_settings(self):
        """Back to the dashboard; settings that were changed but not applied are dropped."""
        if not self.page_open:
            return
        self.page_open = False
        self.page.place_forget()
        self.editing = False
        self.refresh()
        self.set_height(max(self.page_h, self.fit_minsize()))

    def render_headline(self):
        msg, fg = getattr(self, "flash_msg", None) or (getattr(self, "headline_msg", ""), getattr(self, "headline_fg", FG))
        n = self.hl_chars  # two lines max: the tab row must not grow
        self.headline.config(text=msg if len(msg) <= n else msg[:n - 1] + "\u2026", fg=fg)
        if self.compact and not self.tabs_only:  # its own row between tiles and chart, only while there is something to say
            if bool(msg) == bool(self.headline.winfo_manager()):
                return
            if msg:
                self.headline.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
            else:
                self.headline.grid_remove()
            if hasattr(self, "min_w"):  # the chart keeps its minimum height below the message
                self.fit_minsize()

    def redraw_chart(self):
        if self.sel == "live":
            self.live_frame()
        elif self.chart_args is not None:
            self.draw_pace(*self.chart_args)

    # ---- live token throughput (data: live_tokens.py, drawing: live_chart.py)
    def live_tab(self):
        """(rate text, colour, mini graph) for the Live tab: the last 5 minutes of both providers added up."""
        total = sum(self.live.rate(p) for p in PROVIDERS)
        series = self.live.series(300, 10, 30)
        t0 = series["t"][0]
        pts = [((t - t0) / 300, sum(series[p][i] for p in PROVIDERS)) for i, t in enumerate(series["t"])]
        top = max(v for _x, v in pts)
        return fmt_rate(total), (GOOD if total else MUTED), ((pts, top) if top > 0 else None)

    def live_frame(self):
        now = time.time()
        width = self.live_width
        end = now if self.live_end is None else min(self.live_end, now)
        level = next((lv for lv in LIVE_LEVELS if lv[0] >= width), LIVE_LEVELS[-1])  # the finest data level that covers the span
        series = self.live.series(width, level[1], level[2], now=end)
        layout = [x for x in LIVE_LAYOUT if (x[0] != "copilot" or self.cop_on or any(series["copilot"]))
                  and (x[0] != "openrouter" or self.or_on or any(series["openrouter"]))]
        cache = {n: self.cache_text(n, width, end) for n, _l, _c in layout}
        self.big.config(text=fmt_rate(sum(series[x[0]][-1] for x in layout)))
        split = bool(self.split_var.get())
        active = next((k for k, w in LIVE_CHIPS.items() if self.live_end is None and abs(w - width) < 1), None)
        changed = self.live_end is not None or abs(width - LIVE_CHIPS[self.live_preset]) >= 1  # not the preset any more
        self.live_t0, self.live_t1 = series["t"][0], series["t"][-1]
        hover = None if self.hover_t is None else min(max(self.hover_t, self.live_t0), self.live_t1)
        self.live_hits = draw_live(self.chart, series, layout, LIVE_PAL, list(LIVE_CHIPS), active, self.reset_marks(),
                                   cache, split, hover)
        self.plot_x = (PLOT_L, self.chart.winfo_width() - PLOT_R)
        self.draw_zoom_chip(changed, self.chart.winfo_height() - 22)

    def reset_marks(self):
        """[(epoch, '5h'|'7d')] for the Live chart at every zoom level: the resets the stored polls named, plus the start of
        each running window (the moment its counter began again), which no poll has named as a reset yet."""
        marks = past_resets()
        for key, end in self.window_ends.items():
            try:
                start = datetime.fromisoformat(end).timestamp() - SPAN_S[key]
            except (TypeError, ValueError):
                continue
            if start <= time.time() and not any(k == key and abs(t - start) < 600 for t, k in marks):
                marks.append((start, key))
        return marks

    def cache_text(self, provider, window_s, now=None):
        """'cache hit 94%': share of the prompt served from the cache over the shown window ('' without a reading)."""
        hit = self.live.cache_hit_rate(provider, window_s, now)
        return "" if hit is None else f"cache hit {hit * 100:.0f}%"

    ZOOM_MIN = 0.02  # the narrowest pace-chart view: this share of the window

    def chart_click(self, e):
        """Clicks on the reset-zoom chip reset the zoom; on the Live chart's preset chips (top right) they jump to that
        span; anywhere else they start a drag that pans the zoomed time axis."""
        self.drag = (e.x, self.zoom, self.live_width, time.time() if self.live_end is None else self.live_end)
        if self.sel != "live" or e.y > 26:
            return
        for x0, x1, key in self.live_hits:
            if x0 <= e.x <= x1:
                self.live_preset, self.live_width, self.live_end = key, LIVE_CHIPS[key], None
                write_settings(livePreset=key)
                self.live_frame()
                return

    def chart_drag(self, e):
        """Dragging with the left button moves a zoomed time axis (drag right = back in time)."""
        if self.drag is None:
            return
        left, right = self.plot_x
        if self.sel == "live":
            x0, _zoom, width, end0 = self.drag
            self.set_live_view(width, end0 - (e.x - x0) / max(right - left, 1) * width)
            return
        if self.zoom is None or self.drag[1] is None:
            return
        f0, f1 = self.drag[1]
        shift = -(e.x - self.drag[0]) / max(right - left, 1) * (f1 - f0)
        lo = min(max(f0 + shift, 0.0), 1.0 - (f1 - f0))
        self.zoom = (lo, lo + (f1 - f0))
        self.redraw_chart()

    def chart_motion(self, e):
        """Pointer over the Live chart: the epoch under it. The next live tick (about 8 a second) redraws the chart
        with the dots, the x-axis time and the legend values of the sample nearest to it."""
        if self.sel != "live" or self.live_t0 is None:
            return
        left, right = self.plot_x
        frac = (min(max(e.x, left), right) - left) / max(right - left, 1)
        self.hover_t = self.live_t0 + frac * (self.live_t1 - self.live_t0)

    def chart_leave(self, _e=None):
        self.hover_t = None

    def chart_double(self, e):
        if not (self.sel == "live" and e.y <= 26):  # not on the preset chips
            self.reset_zoom()

    def reset_zoom(self):
        if self.sel == "live":
            self.reset_live()
            self.redraw_chart()
        elif self.zoom is not None:
            self.zoom = None
            self.redraw_chart()

    def reset_live(self):
        """The Live chart back to its preset span, following now."""
        self.live_width, self.live_end = LIVE_CHIPS[self.live_preset], None

    def set_live_view(self, width, end):
        """Shows the span `width` s ending at epoch `end` (clamped to the 24 h of data; near now = follow now)."""
        now = time.time()
        width = min(max(width, LIVE_MIN_S), LIVE_KEEP_S)
        end = min(max(end, now - LIVE_KEEP_S + width), now)
        self.live_width = width
        self.live_end = None if now - end < width * 0.02 else end
        self.live_frame()

    def chart_wheel(self, e):
        """Mouse wheel over the chart zooms its time axis around the pointer (wheel up = in); on the Live chart the data
        level (second ... 24 h resolution) follows the zoom. The reset-zoom chip, a double click and every switch to another
        graph undo it."""
        if self.root.winfo_containing(e.x_root, e.y_root) is not self.chart or (self.sel != "live" and self.chart_args is None):
            return
        left, right = self.plot_x
        cx = min(max((e.x_root - self.chart.winfo_rootx() - left) / max(right - left, 1), 0.0), 1.0)
        if self.sel == "live":
            end = time.time() if self.live_end is None else self.live_end
            width = min(max(self.live_width * (0.8 if e.delta > 0 else 1.25), LIVE_MIN_S), LIVE_KEEP_S)
            anchor = end - self.live_width * (1 - cx)  # the time under the pointer stays under it
            self.set_live_view(width, anchor + width * (1 - cx))
            return
        f0, f1 = self.zoom or (0.0, 1.0)
        width = min(1.0, max(self.ZOOM_MIN, (f1 - f0) * (0.8 if e.delta > 0 else 1.25)))
        lo = min(max(f0 + (f1 - f0) * cx - width * cx, 0.0), 1.0 - width)  # the time under the pointer stays under it
        self.zoom = None if width >= 0.999 else (lo, lo + width)
        self.redraw_chart()

    def chart_wheel_pan(self, e):
        """The second wheel (the one that usually scrolls sideways) / Shift+wheel: Windows Tk delivers WM_MOUSEHWHEEL
        as a wheel event with the Shift bit set, so without this binding it would zoom like the main wheel. It pans the
        time axis instead (wheel right = forward in time). Tk negates the horizontal delta: right = negative."""
        if self.root.winfo_containing(e.x_root, e.y_root) is not self.chart or (self.sel != "live" and self.chart_args is None):
            return
        if self.sel == "live":
            end = time.time() if self.live_end is None else self.live_end
            self.set_live_view(self.live_width, end + (0.1 if e.delta < 0 else -0.1) * self.live_width)
            return
        if self.zoom is None:  # nothing to pan while the whole window is shown (dragging behaves the same)
            return
        f0, f1 = self.zoom
        shift = (0.1 if e.delta < 0 else -0.1) * (f1 - f0)
        lo = min(max(f0 + shift, 0.0), 1.0 - (f1 - f0))
        self.zoom = (lo, lo + (f1 - f0))
        self.redraw_chart()

    def draw_zoom_chip(self, active, axis_y):
        """The thin reset-zoom button while zoomed: centred between the x axis (`axis_y`, canvas pixels) and the border of
        the top panel, its left edge on the left edge of the y labels (it may cover x labels). It is a widget above the
        chart, so nothing the chart draws covers it; it is re-placed only when its position changes, because placing and
        raising it on every redraw (up to 8 a second, and while dragging) makes it flicker."""
        box = self.chart.bbox("ylabel")
        if not active or box is None:
            if self.zoom_pos is not None:
                self.zoom_btn.place_forget()
                self.zoom_pos = None
            return
        below = axis_y + self.chart.winfo_height() + int(float(self.pace.cget("pady")))  # panel border, in canvas pixels
        pos = (box[0] + 2, below / 2)  # +2: the label box has a 2 px margin
        if pos != self.zoom_pos:
            self.zoom_btn.place(in_=self.chart, x=pos[0], y=pos[1], anchor="w")
            self.zoom_btn.lift()
            self.zoom_pos = pos

    COST_PROVIDERS = {"live": PROVIDERS, "5h": ("claude",), "7d": ("claude",), "cop": ("copilot",), "or": ("openrouter",)}

    def update_cost(self):
        """Spend rate under the big number: euro cents (euros from 1 EUR) per second over the last minute, for the providers
        of the shown tab (omp replies only, priced in USD and converted at the ECB rate). Blank while it is zero; the
        label keeps its row, so the window never changes height."""
        provs = self.COST_PROVIDERS.get(self.sel)
        if not provs:
            return
        self.cost.config(text=fmt_spend(self.live.cents_per_s(provs) * usd_eur()) or " ")

    def cost_tip(self):
        if not self.cost.cget("text").strip():  # nothing shown, nothing to explain
            return ""
        head = "Spend over the last minute, per second, in euro cents (euros from 1 \u20ac)"
        if self.COST_PROVIDERS.get(self.sel) == ("openrouter",):
            return head + "\nOpenRouter's own prices (USD at the ECB rate), omp replies only"
        return (head + "\nat API list prices (USD at the ECB rate):\n"
                "Claude and Copilot are flat plans, only OpenRouter is billed per use.\nomp replies only")

    def live_tick(self):
        """About 8 times a second while the Live tab is shown, so the chart slides smoothly (the rest renders every 5 s);
        the tab's number is updated once a second."""
        try:
            self.live_n += 1
            if self.live_n % 8 == 0 or self.sel != "live":
                text, color, spark = self.live_tab()
                self.tabs["live"].set(text, color, self.sel == "live", spark)
                self.update_cost()
            if self.sel == "live":
                self.live_frame()
        except Exception:
            LOGGER.error("dashboard live\n%s", traceback.format_exc().rstrip())
        self.root.after(125 if self.sel == "live" else 1000, self.live_tick)

    def close(self):
        try:
            self.store_geometry()
        finally:
            self.root.destroy()

    # ---- actions
    def flash(self, text, color=MUTED):
        """Short message for 6 s: on the settings page, and in the headline of the dashboard."""
        if getattr(self, "_flash_job", None):
            self.root.after_cancel(self._flash_job)  # an older timer must not clear this newer message
        self.flash_msg = (text, color)
        self.msg.config(text=text, fg=color)
        self.render_headline()

        def clear():
            self.flash_msg = None
            self.msg.config(text="")
            self.render_headline()
        self._flash_job = self.root.after(6000, clear)

    # ---- paperclipai update check (installed version read from the WSL npm install, latest from the npm registry)
    def load_update(self):
        try:
            with open(UPDATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def check_update(self):
        if self.upd_busy:
            return
        self.upd_busy = True
        threading.Thread(target=self._update_worker, daemon=True).start()

    def show_tip(self, e, text):
        self.hide_tip()
        if not text:
            return
        self.tip = tw = tk.Toplevel(self.root)
        tw.overrideredirect(True)
        tw.attributes("-topmost", True)
        tk.Label(tw, text=text, bg="#4a505a", fg=FG, font=("Segoe UI", 9), padx=8, pady=4).pack()
        tw.geometry(f"+{e.x_root + 12}+{e.y_root + 18}")

    def hide_tip(self, _e=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None

    def check_update_now(self):
        self.check_update()
        self.refresh()

    def _update_worker(self):
        res = {"at": time.time()}
        try:
            out = subprocess.run(
                wsl_cmd(CFG, f"grep -m1 '\"version\"' '{CFG['wslPkg']}/package.json'"),
                capture_output=True, text=True, timeout=45, creationflags=0x08000000).stdout
            m = re.search(r'"version"\s*:\s*"([^"]+)"', out)
            if not m:
                raise RuntimeError("installed version not found in WSL")
            res["installed"] = m.group(1)
            res["latest"] = http_util.request_json("https://registry.npmjs.org/paperclipai/latest", timeout=20)["version"]
        except Exception as e:
            res["error"] = str(e)[:120] or type(e).__name__
        try:
            with open(UPDATE_FILE, "w", encoding="utf-8") as f:
                json.dump(res, f)
        finally:
            self.upd_busy = False

    @staticmethod
    def _ver(v):
        return tuple(int(x) for x in re.findall(r"\d+", v))

    def load_run(self):
        """State of the last updater.py run; a 'running' run that stopped reporting counts as failed."""
        try:
            with open(UPDATE_RUN, encoding="utf-8") as f:
                run = json.load(f)
        except Exception:
            return {}
        if run.get("state") == "running" and time.time() - run.get("at", 0) > 180:
            return {**run, "state": "failed", "error": "the update script stopped reporting (see update-paperclip.log)"}
        return run

    def update_text(self):
        """(text, colour, button label or None): the button replaces the text while an update can be started."""
        self.tips["Paperclip"] = ""
        run = self.load_run()
        if run.get("state") == "running":
            self.tips["Paperclip"] = f"Updating {run.get('from')} -> {run.get('target')}. The server is down for part of this (about 6 min)."
            return f"updating: {run.get('step')}...", BLUE, None
        u = self.load_update()
        if self.upd_busy:
            return "checking...", MUTED, None
        if not u:
            return "not checked yet", MUTED, None
        if "error" in u:
            self.tips["Paperclip"] = f"The check for a newer version failed ({u['error']}); it is repeated every 10 minutes."
            return "check failed", BAD, None
        inst, latest = u["installed"], u["latest"]
        self.tips["Paperclip"] = f"Installed: {inst}"
        if self._ver(latest) > self._ver(inst):
            failed = run.get("state") == "failed" and run.get("target") == latest
            if failed:
                self.tips["Paperclip"] += f"\nLast update failed: {run.get('error')}"
            return f"update available: {latest}", WARN, (f"Retry update to {latest}" if failed else f"Update to {latest}")
        return "up to date", GOOD, None

    def set_update_button(self, label):
        """Show the update button (label) instead of the status text, or the text again (label None)."""
        self.upd_offer = label
        if label is None:
            self.upd_btn.grid_remove()
            self.vals["Paperclip"].grid()
            return
        self.vals["Paperclip"].grid_remove()
        self.upd_btn.grid()
        if time.time() - self.upd_armed > 5:  # keep "Confirm update?" while it is armed
            self.upd_btn.set_state(label)

    def start_update(self):
        """Two clicks within 5 s: runs updater.py for the latest version (agents must be idle; the server restarts)."""
        latest = self.load_update().get("latest")
        if not latest or self.upd_offer is None:
            return
        if time.time() - self.upd_armed > 5:
            self.upd_armed = time.time()
            self.upd_btn.set_state("Confirm update?", danger=True)
            self.upd_btn.after(5000, lambda: self.upd_btn.set_state(self.upd_offer) if self.upd_offer else None)
            return
        self.upd_armed = 0.0
        inst = self.load_update().get("installed")
        with open(UPDATE_RUN, "w", encoding="utf-8") as f:  # shown immediately; the script overwrites it
            json.dump({"state": "running", "step": "starting", "target": latest, "from": inst, "error": None, "at": time.time()}, f)
        subprocess.Popen(pacer_config.self_cmd("update", latest), cwd=DIR, creationflags=0x08000000 | 0x00000200,  # NO_WINDOW | NEW_PROCESS_GROUP
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.refresh()

    def check_now(self):
        open(CHECK_FLAG, "w").close()  # the tray picks this up within a second
        self.check_req = time.time()

    def open_paperclip(self):
        """Main part of the combo button: opens Paperclip, or, while a restart waits for its confirmation, is that confirmation."""
        if time.time() - self.restart_armed <= 5:
            self.restart_server()
        else:
            webbrowser.open(URL)

    def paperclip_actions(self):
        return [("Restart Paperclip", self.restart_server), ("Check for Paperclip update", self.check_update_now)]

    def restart_server(self):
        """Arms the restart: the combo button turns red ('Confirm restart?') for 5 s, its main part then kills paperclipai +
        its Postgres in WSL and starts the server task again (watchdog.py --restart)."""
        if time.time() - self.restart_armed > 5:
            self.restart_armed = time.time()
            self.open_btn.set_state("Confirm restart?", danger=True)
            self.open_btn.after(5000, lambda: self.open_btn.set_state())
            self.flash("click the red button within 5 s to restart Paperclip", WARN)
            return
        self.restart_armed = 0.0
        self.open_btn.set_state()
        subprocess.Popen(pacer_config.self_cmd("watchdog", "--restart"), cwd=DIR, creationflags=0x08000000,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.flash("restarting Paperclip, takes about a minute", GOOD)

    def open_menu(self, anchor, items):
        """Borderless popup styled like the dashboard (a tk.Menu is drawn natively and ignores most colours)."""
        self.close_menu()
        top = self.menu = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg="#4a505a")
        body = tk.Frame(top, bg=PANEL, pady=4)
        body.pack(padx=1, pady=1)
        for text, cmd in items:
            lbl = tk.Label(body, text=text, bg=PANEL, fg=FG, font=("Segoe UI", 9), anchor="w", padx=16, pady=6, cursor="hand2")
            lbl.pack(fill="x")
            lbl.bind("<Enter>", lambda _e, l=lbl: l.config(bg=BTN_HOVER))
            lbl.bind("<Leave>", lambda _e, l=lbl: l.config(bg=PANEL))
            lbl.bind("<ButtonRelease-1>", lambda _e, c=cmd: (self.close_menu(), c()))
        top.bind("<FocusOut>", lambda _e: self.close_menu())
        top.bind("<Escape>", lambda _e: self.close_menu())
        top.update_idletasks()
        w, h = top.winfo_reqwidth(), top.winfo_reqheight()
        ax, ay, ah = anchor.winfo_rootx(), anchor.winfo_rooty(), anchor.winfo_height()
        wl, wt, wr, wb = work_area(ax + anchor.winfo_width() // 2, ay)  # work area of the monitor the button is on
        x = min(max(ax + anchor.winfo_width() - w, wl), wr - w)
        y = ay + ah if ay + ah + h <= wb else max(wt, ay - h)  # not enough room below: open upwards
        top.geometry(f"+{x}+{y}")
        top.deiconify()
        top.focus_force()

    def close_menu(self):
        if self.menu is not None:
            self.menu.destroy()
            self.menu = None

    def restart_tool(self):
        """Stops the tray and every dashboard of this tool, then starts tray + dashboard again (`pacer restart`)."""
        self.store_geometry()  # the force-stop skips close(); the relaunched dashboard restores from this
        subprocess.Popen(pacer_config.self_cmd("restart"), cwd=DIR, creationflags=0x08000000 | 0x00000008,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def claude_signin(self):
        """Opens `claude auth login` in its own console (browser sign-in). The governor picks up the new token on its next check."""
        if not claude_cli_found():
            webbrowser.open(CLAUDE_SETUP_URL)
            self.flash("install Claude Code with the page that just opened, then press the button again to sign in", WARN)
            return
        subprocess.Popen([CLAUDE_EXE, "auth", "login"], creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        self.flash("finish the sign-in in the console window; the governor syncs the new token within 3 minutes", GOOD)

    def copilot_on(self, cs):
        """Copilot is part of the UI when the user said so (settings key copilot: true/false) or, by default, once a login was found."""
        pref = read_settings().get("copilot")
        return bool(pref) if pref is not None else bool(cs) and cs.get("state") != "nologin"

    def openrouter_on(self, os_):
        """OpenRouter is part of the UI when the user said so (settings key openrouter: true/false) or, by default, once a key was found."""
        pref = read_settings().get("openrouter")
        return bool(pref) if pref is not None else bool(os_) and os_.get("state") != "nokey"

    def set_openrouter_visible(self, on):
        if on == self.or_on:
            return
        self.or_on = on
        self.place_side()
        self.place_rows()

    def set_copilot_visible(self, on):
        if on == self.cop_on:
            return
        self.cop_on = on
        self.place_side()
        self.place_rows()

    def place_rows(self):
        """Status / Services rows and settings cards that belong to Copilot or to Paperclip show only while those are in use."""
        for k, lbl in self.lbls.items():
            on = ((self.cop_on or k not in COPILOT_ROWS) and (self.or_on or k not in OPENROUTER_ROWS)
                  and (self.pc_on or k not in PAPERCLIP_ROWS))
            for w in (lbl, self.pc_frame if k == "Paperclip" else self.vals[k]):
                w.grid() if on else w.grid_remove()
        for title, on in (("CLAUDE", True), ("COPILOT", self.cop_on), ("OPENROUTER", self.or_on)):
            for w in (self.heads[title], self.heads[title].sep):
                if w is not None:
                    w.grid() if on else w.grid_remove()
        self.place_page()
        if hasattr(self, "min_w"):
            self.place_details()
            self.fit_minsize()

    def place_side(self):
        """Tab column (wide): the tab groups, Live first; the Copilot / OpenRouter groups only while they are in use.
        Compact: one row of tab tiles without group titles."""
        for w in self.side.winfo_children():
            w.pack_forget()
        shown = [(hdr, wins) for title, hdr, wins in self.groups if title not in self.idle
                 and (title != "COPILOT" or self.cop_on) and (title != "OPENROUTER" or self.or_tab)]
        for _hdr, wins in shown:
            for win in wins:
                self.tabs[win].layout(self.compact and not self.tabs_only)
        if self.compact and not self.tabs_only:
            tabs = [self.tabs[win] for _hdr, wins in shown for win in wins]
            for i, tab in enumerate(tabs):
                tab.frame.pack(side="left", fill="both", expand=True, padx=(0, 0 if i == len(tabs) - 1 else 4))
            return
        for hdr, wins in shown:
            hdr.pack(fill="x", pady=(0, 2))
            for i, win in enumerate(wins):
                self.tabs[win].frame.pack(fill="x", pady=(0, 10 if i == len(wins) - 1 else 4))
        if self.tabs_only:
            self.fit_tabs_only()

    def paperclip_menu(self):
        if self.pc_on:
            return [("Open watchdog log", lambda: os.startfile(LOGS["watchdog"]))]
        return [("Connect Paperclip...", self.connect_paperclip)]

    def menu_items(self):
        items = [("Open governor log", lambda: os.startfile(LOGS["governor"]))] + self.paperclip_menu()
        if not self.cop_on:
            items.append(("Connect GitHub Copilot...", self.connect_copilot))
        if not self.or_on:
            items.append(("Connect OpenRouter...", self.connect_openrouter))
        return items + [("Restart Pacer", self.restart_tool)]

    def toggle_autostart(self):
        autostart.set_enabled(bool(self.auto_var.get()))

    def toggle_language(self):
        self.set_language("de" if self.lang_var.get() else "en")

    def tip_text(self, key):
        """Hover text of a Status row: the spelled-out status behind a symbol, then the row's own details."""
        return "\n".join(x for x in (self.sym_tip.get(key, ""), self.tips.get(key, "")) if x)

    def put(self, key, text, color=FG):
        """Set a Status value: known states become a coloured symbol (the words move into the tooltip), the rest stays text."""
        head, sep, rest = text.partition("  \u00b7 ")
        glyph = SYMBOLS.get(head)
        self.sym_tip[key] = "" if glyph is None or head == "OK" else head
        if glyph is None:
            self.vals[key].config(text=text, fg=color, font=("Segoe UI", 9))
        else:
            self.vals[key].config(text=glyph + sep + rest, fg=color, font=("Segoe UI Symbol", 10, "bold"))

    def align_details(self):
        """Status, Services and Agents give their label column the width of the widest label of all three, so the values
        start at the same distance from the left edge of every card (again after a language change)."""
        self.root.update_idletasks()
        width = max(lbl.winfo_reqwidth() for lbl in self.lbls.values())
        for card in (self.st, self.svc, self.agents):
            card.columnconfigure(0, minsize=width)

    def set_language(self, lang):
        write_settings(lang=lang)
        i18n.set_lang(lang)
        i18n.retranslate(self.root)
        self.align_details()
        self.refresh()

    def set_paperclip_visible(self, on):
        """Paperclip-only parts: agent states, Watchdog/Server/Paperclip rows, Open/Restart buttons, throttle settings, idle sleep."""
        if on == self.pc_on:
            return
        self.pc_on = on
        self.place_rows()
        self.set_shown(self.open_btn, on)
        self.si.grid() if on else self.si.grid_remove()
        self.update_apply()

    def connect_paperclip(self):
        """Looks for a Paperclip install again (WSL distro with paperclipai, or a server answering at the configured URL)
        and turns the Paperclip parts on when one is found."""
        self.flash("looking for Paperclip...", MUTED)

        def work():
            try:
                found = bool(pacer_config.get(force=True).get("wslDistro"))
                CFG.update(pacer_config.get())
                if not found:
                    http_util.request_text(URL + "/api/health", timeout=5)
                    found = True
            except Exception:
                pass
            if found:
                write_settings(paperclip=True)
            self.root.after(0, lambda: (self.flash("Paperclip found" if found else "no Paperclip found: install it in WSL, or set wslDistro / url in pacer-config.json",
                                                   GOOD if found else WARN), self.refresh()))
        threading.Thread(target=work, daemon=True).start()


    def connect_copilot(self):
        write_settings(copilot=True)
        self.copilot_signin()
        self.refresh()

    def connect_openrouter(self):
        write_settings(openrouter=True)
        self.flash(f"set OPENROUTER_API_KEY (user environment) or put the key in {openrouter_monitor.KEY_FILE}", GOOD)
        self.refresh()

    def copilot_signin(self):
        """GitHub device-code login for opencode in WSL (the credential Paperclip's Copilot agents use), in its own console.
        When that console closes the tray is asked to re-read the Copilot status. Without WSL the monitor reads the VS Code login."""
        if not CFG.get("wslDistro"):
            open(CHECK_FLAG, "w").close()
            self.flash("no WSL: sign in to GitHub Copilot in VS Code, the Pacer picks that login up within minutes", WARN)
            return
        script = (f'oc="{CFG["wslNodeBin"]}/opencode"; [ -x "$oc" ] || oc=$(command -v opencode); PATH=$(dirname "$oc"):$PATH; '
                  'opencode auth login -p github-copilot; echo; read -p "Done - press Enter to close "')
        proc = subprocess.Popen(wsl_cmd(CFG, script, login=True), creationflags=0x00000010)  # CREATE_NEW_CONSOLE
        threading.Thread(target=lambda: (proc.wait(), open(CHECK_FLAG, "w").close()), daemon=True).start()
        self.flash("follow the steps in the console window (GitHub device code)", GOOD)

    def entered(self, name):
        """What the settings page shows for one provider, comparable with its saved settings (limits as text; they do not
        count under full budget, where they are hidden)."""
        pv = self.prov[name]
        fb = bool(pv["fb"].get())
        return (fb, bool(pv["pace"].get())) + (() if fb else (pv["pause"].get().strip(), pv["resume"].get().strip()))

    def update_apply(self):
        """Show 'Apply' only while the entered settings differ from the saved ones; nothing takes effect before that."""
        dirty = False
        for name, pv in self.prov.items():
            fb, s = bool(pv["fb"].get()), pv["saved"]
            for w in pv["rows"]:  # limits are meaningless under full budget
                (w.grid_remove if fb else w.grid)()
            saved = (s["fullBudget"], s["byPace"]) + (() if fb else (str(s["pauseAt"]), str(s["resumeBelow"])))
            dirty = dirty or self.entered(name) != saved
        if dirty and self.pc_on:
            self.apply_btn.pack(side="right")
        else:
            self.apply_btn.pack_forget()
        if self.page_open:  # limit rows came or went: the page must still fit
            need = self.fit_minsize()
            if need > self.root.winfo_height():
                self.set_height(need)

    def set_shown(self, btn, shown):
        """Show or hide a bottom-row button; the row is re-laid out only when something changes."""
        if getattr(btn, "shown", True) != shown:
            btn.shown = shown
            self.place_bar()

    def place_bar(self):
        """Lay out the bottom row left to right: visible buttons only, so hidden sign-ins leave no gap."""
        for w in self.bar_order:
            w.grid_remove()
        for k in range(len(self.bar_order)):
            self.bar.columnconfigure(k, weight=0, uniform="")
        shown = [w for w in self.bar_order if getattr(w, "shown", True)]
        for k, w in enumerate(shown):
            compact = getattr(w, "compact", False)
            self.bar.columnconfigure(k, weight=0 if compact else 1, uniform="" if compact else "btn")
            w.grid(row=0, column=k, sticky="nsew", padx=(0 if k == 0 else GAP // 2, GAP // 2 if k < len(shown) - 1 else 0))

    def toggle_split(self):
        write_settings(splitModels=bool(self.split_var.get()))
        self.redraw_chart()

    def toggle_si(self):
        on = bool(self.si_var.get())
        write_settings(sleepWhenIdle=on)
        self.flash("PC may sleep 10 min after Paperclip goes idle" if on else "PC stays awake while 'Keep PC awake' is on", GOOD)

    def toggle_ka(self):
        """Keep-awake applies at once (no Apply step): the tray polls the settings file and holds the wake lock."""
        on = bool(self.ka_var.get())
        write_settings(keepAwake=on)
        self.si.config(state="normal" if on else "disabled")
        self.flash("keep awake on: PC will not sleep" if on else "keep awake off", GOOD)

    def setting_changed(self):
        self.editing = True  # a pending choice: keep it until applied, refresh must not reset it
        self.update_apply()

    def apply_limits(self):
        """Save the throttle settings of every provider and have the governor decide on them at once."""
        if not self.page_open:
            return
        new = {}
        for name, pv in self.prov.items():
            s = {**pv["saved"], "fullBudget": bool(pv["fb"].get()), "byPace": bool(pv["pace"].get())}
            if not s["fullBudget"]:
                try:
                    p, r = int(pv["pause"].get()), int(pv["resume"].get())
                except ValueError:
                    return self.flash("enter whole numbers", BAD)
                if not 1 <= r <= p <= 100:
                    return self.flash("need 1 <= resume <= pause <= 100", BAD)
                s["pauseAt"], s["resumeBelow"] = p, r
            new[name] = s
        write_settings(providers=new)
        self.editing = False
        self.root.focus_set()
        self.check_now()
        self.flash("saved", GOOD)
        self.refresh()

    def pick(self, win):
        """Click a tab to show (and keep) that window."""
        self.pinned = win
        write_settings(pinnedTab=win)
        self.refresh()

    def unpin(self):
        """Right click on a tab: follow the limiting window again."""
        self.pinned = None
        write_settings(pinnedTab=None)
        self.refresh()

    # ---- rendering
    def refresh(self):
        if self._job is not None:
            self.root.after_cancel(self._job)  # handlers call refresh() too: keep exactly one timer
        try:
            self.render()
        except Exception:  # a bad frame must not kill the window
            LOGGER.error("dashboard render\n%s", traceback.format_exc().rstrip())
        self._job = self.root.after(REFRESH_MS, self.refresh)

    def render(self):
        s, age = read_status()
        now0 = time.time()
        pc_on = paperclip_enabled()
        self.set_paperclip_visible(pc_on)
        if pc_on:
            kick_token_dump()
        ts = tray_state()
        jobs = (ts or {}).get("jobs", {})
        gov, wd = jobs.get("governor", {}), jobs.get("watchdog", {})
        srv = (ts or {}).get("server_ok")
        model, _ = evaluate(gov.get("ok"), wd.get("ok"), srv)
        fb = full_budget()
        thr = throttle.limits({**provider_settings("claude"), "fullBudget": fb})[0]
        cp_thr = throttle.limits({**provider_settings("copilot"), "fullBudget": full_budget("copilot")})[0]
        or_thr = throttle.limits({**provider_settings("openrouter"), "fullBudget": full_budget("openrouter")})[0]
        subs = {n: ((s or {}).get("providers") or {}).get(n) or {} for n in throttle.PROVIDERS}  # sub-state per provider
        held = {n: pc_on and subs[n].get("mode", "active") != "active" for n in throttle.PROVIDERS}

        # window / taskbar icon follows the live state
        self.icon_img = ImageTk.PhotoImage(tray_icon.make_icon(model, 64))
        self.root.iconphoto(True, self.icon_img)

        hist = history()
        u = (s or {}).get("usage") or {}
        stale_usage = False
        self.window_ends = {"5h": u.get("resets5"), "7d": u.get("resets7")}
        if not u and LAST:  # usage endpoint down: show the last known reading instead of an empty chart
            u = {k: LAST[k] for k in ("five", "seven", "resets5", "resets7")}
            stale_usage = True
            model["pct"] = max(u["five"], u["seven"])
            model["window"] = "5h" if u["five"] >= u["seven"] else "7d"
        if model["fault"]:
            msg, fg = fault_reason(gov.get("ok"), wd.get("ok"), srv, age), BAD
        elif stale_usage:
            msg, fg = f"usage reading {fmt_min((now0 - LAST['t']) // 60)} old", MUTED
        else:
            msg, fg = "", FG
        self.headline_msg, self.headline_fg = msg, fg
        self.render_headline()

        # pace panel: tabs carry both windows' numbers, the chart shows the selected one
        now = time.time()
        wins = {"5h": ("five", "resets5", 1), "7d": ("seven", "resets7", 2)}
        info = {}
        for win, (key, rk, _) in wins.items():
            usage = u.get(key)
            p = tray_icon.pace(usage, u.get(rk), win, thr)
            info[win] = (usage, p, verdict(p, usage, thr, SPAN_S[win]))
        limiting = model["window"]
        sel = self.pinned or limiting
        if self.pinned is None and not u.get(wins[sel][1]):  # idle window: show the one that is running
            sel = next((w for w in wins if u.get(wins[w][1])), sel)
        cs = copilot_monitor.read_status()
        cop_on = self.copilot_on(cs)
        self.set_copilot_visible(cop_on)
        if not cop_on and sel == "cop":
            self.pinned, sel = None, limiting
        os_ = openrouter_monitor.read_status()
        or_on = self.openrouter_on(os_)
        self.set_openrouter_visible(or_on)
        # a key without a spend limit has no graph: only the Status rows, no tab
        or_tab = or_on and not os_.get("unlimited")
        if or_tab != self.or_tab:
            self.or_tab = or_tab
            self.place_side()
        if not or_tab and sel == "or":
            self.pinned, sel = None, limiting
        cp_usage = None if cs.get("unlimited") else cs.get("used")
        cp_reset = cs.get("resets")
        cp_period = copilot_monitor.period(cp_reset) if cp_reset else None
        cp_p, cp_color, cp_span = None, MUTED, None
        if cp_usage is not None and cp_period:
            cp_span = cp_period[1] - cp_period[0]
            cp_p = tray_icon.pace(cp_usage, cp_reset, "cop", cp_thr, span_s=cp_span, early_s=6 * 3600)
            cp_color = HEX[tray_icon.window_verdict(cp_p, cp_usage, cp_span)]
        cp_hist = quota_history(COPILOT_HIST, cp_period[0]) if cp_period else []
        or_usage = None if os_.get("unlimited") else os_.get("used")
        or_reset, or_span = os_.get("resets"), os_.get("span")
        or_p, or_color, or_hist = None, MUTED, []
        if or_usage is not None and or_reset and or_span:  # a limit that resets is a pace like Copilot's month
            or_p = tray_icon.pace(or_usage, or_reset, "or", or_thr, span_s=or_span, early_s=or_span * 0.05)
            or_color = HEX[tray_icon.window_verdict(or_p, or_usage, or_span)]
            or_hist = quota_history(OPENROUTER_HIST, datetime.fromisoformat(or_reset).timestamp() - or_span)
        elif or_usage is not None:  # a limit that never resets has no plan line: colour by distance to the limit
            or_color = HEX["bad" if or_usage >= or_thr else "warn" if or_usage >= 0.8 * or_thr else "good"]
        for win, (usage, _p, color) in info.items():
            idx = wins[win][2]
            spark = quota_spark([(h[0], h[idx]) for h in hist], u.get(wins[win][1]), SPAN_S[win], usage, now)
            self.tabs[win].set("-" if usage is None else f"{usage:g}%", color, win == sel, spark, held["claude"])
        self.tabs["cop"].set("-" if cp_usage is None else pct_text(cp_usage), cp_color, sel == "cop",
                             quota_spark(cp_hist, cp_reset, cp_span, cp_usage, now), held["copilot"])
        self.tabs["or"].set("-" if or_usage is None else pct_text(or_usage), or_color, sel == "or",
                            quota_spark(or_hist, or_reset, or_span, or_usage, now), held["openrouter"])
        if sel != self.sel:
            self.zoom = None  # a zoom belongs to one graph
            self.reset_live()
        self.sel = sel
        # a provider whose usage did not move for 24 h (and that runs no tokens and is not paused) has no tabs, unless one is shown
        busy = {"CLAUDE": ("claude", quota_moved([(h[0], h[1]) for h in hist], u.get("five"), now)
                           or quota_moved([(h[0], h[2]) for h in hist], u.get("seven"), now)),
                "COPILOT": ("copilot", quota_moved(cp_hist, cp_usage, now)),
                "OPENROUTER": ("openrouter", quota_moved(or_hist, or_usage, now))}
        idle = {title for title, _h, wins in self.groups if title in busy and sel not in wins
                and not (busy[title][1] or self.live.active(busy[title][0]) or held[busy[title][0]])}
        if idle != self.idle:
            self.idle = idle
            self.place_side()
        text, color, spark = self.live_tab()
        self.tabs["live"].set(text, color, sel == "live", spark)
        if sel == "live":
            self.chart_args = None
            self.live_frame()
        elif sel == "cop":
            self.big.config(text="-" if cp_usage is None else pct_text(cp_usage))
            self.draw_pace("cop", 1, cp_usage, cp_reset, cp_p, cp_thr, cp_hist, cp_color, now, cp_span)
        elif sel == "or":
            self.big.config(text="-" if or_usage is None else pct_text(or_usage))
            self.draw_pace("or", 1, or_usage, or_reset, or_p, or_thr, or_hist, or_color, now, or_span)
        else:
            usage, p, vcolor = info[sel]
            key, rk, idx = wins[sel]
            self.big.config(text="-" if usage is None else f"{usage:g}%")
            self.draw_pace(sel, idx, usage, u.get(rk), p, thr, hist, vcolor, now)
        resets = {"5h": u.get("resets5"), "7d": u.get("resets7"), "cop": cp_reset, "or": or_reset}
        self.sub.config(text=countdown_text(resets.get(sel)) or " ")
        self.update_cost()

        # throughput
        for key, idx, win in (("Throughput 5h", 1, "5h"), ("Throughput 7d", 2, "7d")):
            r = throughput(hist, idx)
            pv = (info[win][1] or {}).get("verdict")
            label = {"on plan": "OK", "too much": "Too fast", "too little": "Too slow", "limit": "At limit",
                     "early": "Too early to tell"}.get(pv, "-")
            self.tips[key] = "" if r is None else f"{r:+.1f} %/h over the last hour"
            self.put(key, label, info[win][2])
        for key, rk, win in (("Resets 5h", "resets5", "5h"), ("Resets 7d", "resets7", "7d")):
            reset = u.get(rk)
            txt = "-"
            self.tips[key] = ""
            if reset:
                try:
                    dt = datetime.fromisoformat(reset)
                    txt = f"in {fmt_min((dt.timestamp() - time.time()) // 60)}"
                    self.tips[key] = dt.astimezone().strftime('%a %H:%M')
                except ValueError:
                    pass
            self.vals[key].config(text=txt)

        last_err = last_governor_error() if gov.get("ok") is False else ""
        login_err = "local subscription" in last_err or "Claude login not found" in last_err
        t, c, self.tips["Claude login"] = claude_login(login_err)
        self.put("Claude login", t, HEX[c])
        self.set_shown(self.signin_btn, login_err)
        self.signin_btn.set_state("Get Claude Code" if login_err and not claude_cli_found() else None)

        def job_text(j):
            if ts is None:
                return "tray not running", MUTED
            if j.get("ok") is None:
                return "not run yet", MUTED
            return 'OK' if j['ok'] else 'FAILED', GOOD if j["ok"] else BAD
        for key, j in (("Governor", gov), ("Watchdog", wd)):
            t, c = job_text(j)
            self.tips[key] = f"Last run: {ago(j.get('at'))}" if j.get("ok") is not None else ""
            if key == "Governor" and self.check_req is not None:
                if (j.get("at") or 0) >= self.check_req or time.time() - self.check_req > 120:
                    self.check_req = None
                else:
                    t += "  \u00b7 check requested"
            self.put(key, t, c)
        if pc_on:
            self.put("Server", "unknown" if srv is None else "healthy" if srv else "DOWN",
                     MUTED if srv is None else GOOD if srv else BAD)
            last = self.load_update().get("at") or 0
            run = self.load_run()
            finished = run.get("state") in ("done", "failed") and (run.get("at") or 0) > last  # re-read the installed version
            retry = "error" in self.load_update()
            if not self.upd_busy and (finished or time.time() - last > (UPDATE_RETRY_S if retry else UPDATE_EVERY_S)):
                self.check_update()  # first run, every 6 h and after an update run; manual check is in the "..." menu
            t, c, offer = self.update_text()
            self.put("Paperclip", t, c)
            self.set_update_button(offer)

        # Copilot rows: login state, pace verdict (credits tooltip) and time to the monthly reset
        t, c, self.tips["Copilot login"], need_signin = copilot_login(cs)
        self.put("Copilot login", t, HEX[c])
        self.set_shown(self.cop_btn, need_signin and cop_on)
        self.tips["Copilot usage"] = ""
        if cs.get("unlimited"):
            self.put("Copilot usage", "unlimited", GOOD)
        elif cp_usage is None:
            self.put("Copilot usage", "-", MUTED)
        else:
            pv = (cp_p or {}).get("verdict")
            label = {"on plan": "OK", "too much": "Too fast", "too little": "Too slow", "limit": "At limit",
                     "early": "Too early to tell"}.get(pv, "-")
            ent = cs["entitlement"]
            self.tips["Copilot usage"] = f"{ent - cs['remaining']:,.1f} of {ent:,} credits used"
            self.put("Copilot usage", label, cp_color)
        txt = "-"
        self.tips["Copilot resets"] = ""
        if cp_reset:
            dt = datetime.fromisoformat(cp_reset)
            txt = f"in {fmt_min((dt.timestamp() - time.time()) // 60)}"
            self.tips["Copilot resets"] = dt.astimezone().strftime("%a %d.%m. %H:%M")
        self.vals["Copilot resets"].config(text=txt)

        # OpenRouter rows: key state, spend (tooltip) and time to the reset of the key's limit
        t, c, self.tips["OpenRouter key"] = openrouter_key(os_)
        self.put("OpenRouter key", t, HEX[c])
        self.tips["OpenRouter usage"] = ""
        if os_.get("unlimited"):
            self.tips["OpenRouter usage"] = f"the key has no spend limit in OpenRouter, so nothing is throttled; ${os_.get('spent') or 0:,.2f} spent so far"
            self.put("OpenRouter usage", "no limit", GOOD)
        elif or_usage is None:
            self.put("OpenRouter usage", "-", MUTED)
        else:
            pv = (or_p or {}).get("verdict")
            label = {"on plan": "OK", "too much": "Too fast", "too little": "Too slow", "limit": "At limit",
                     "early": "Too early to tell"}.get(pv) or pct_text(or_usage)
            self.tips["OpenRouter usage"] = f"${os_['spent']:,.2f} of ${os_['limit']:,.2f} spent ({pct_text(or_usage)})"
            self.put("OpenRouter usage", label, or_color)
        txt = "never" if or_usage is not None and not or_reset else "-"
        self.tips["OpenRouter resets"] = ""
        if or_reset:
            dt = datetime.fromisoformat(or_reset)
            txt = f"in {fmt_min((dt.timestamp() - time.time()) // 60)}"
            self.tips["OpenRouter resets"] = dt.astimezone().strftime("%a %d.%m. %H:%M")
        self.vals["OpenRouter resets"].config(text=txt)

        # agents of each provider: its sub-state
        for name, key in (("claude", "Claude agents"), ("copilot", "Copilot agents"), ("openrouter", "OpenRouter agents")):
            mode = subs[name].get("mode")
            self.tips[key] = "" if mode in (None, "active") else {
                "pace": "usage is ahead of the plan", "limit": "limit reached",
                "unknown": "no usage reading"}.get(subs[name].get("reason"), "")
            self.put(key, mode and {"active": "running"}.get(mode, mode) or "-",
                     {"active": GOOD, None: MUTED}.get(mode, WARN))

        # settings (do not clobber a pending change)
        for name, pv in self.prov.items():
            pv["saved"] = saved = provider_settings(name)
            if not self.editing:
                pv["fb"].set(saved["fullBudget"])
                pv["pace"].set(saved["byPace"])
                pv["pause"].set(str(saved["pauseAt"]))
                pv["resume"].set(str(saved["resumeBelow"]))
        if not self.editing:
            self.ka_var.set(keep_awake())
            self.si_var.set(sleep_when_idle())
            self.split_var.set(read_settings().get("splitModels") is True)
            self.auto_var.set(autostart.is_enabled())
            self.lang_var.set(i18n.LANG == "de")
        self.si.config(state="normal" if keep_awake() else "disabled")
        self.update_apply()

    def draw_pace(self, win, idx, usage, reset, p, thr, hist, color, now, span=None):
        """Burn-up chart of one window: usage so far vs. the plan (a steady burn that reaches `thr` exactly at the
        reset), the limit / pause line and a dotted projection at the window's average pace."""
        self.chart_args = (win, idx, usage, reset, p, thr, hist, color, now, span)
        c = self.chart
        c.delete("all")
        W, H = c.winfo_width(), c.winfo_height()
        if W < 100 or H < 60:  # not laid out yet; <Configure> redraws
            self.draw_zoom_chip(False, 0)
            return
        L, R, T, B = 26, 6, 38, 22  # y labels | right edge | limit label + "early" bracket | ticks
        pw, ph = W - L - R, H - T - B
        yb = T + ph
        self.plot_x = (L, W - R)
        small = ("Segoe UI", 8)

        def Y(v):
            return T + ph * (1 - min(max(v, 0), 100) / 100)

        def grid():
            for v in (0, 50, 100):
                c.create_line(L, Y(v), W - R, Y(v), fill=GRID)

        def ylabels():
            for v in (0, 50, 100):
                c.create_text(L - 6, Y(v), text=str(v), fill=MUTED, font=small, anchor="e", tags="ylabel")
        try:
            end = datetime.fromisoformat(reset).timestamp() if reset else None
        except ValueError:
            end = None
        if end is None or usage is None:
            grid()
            ylabels()
            c.create_text(L + pw / 2, T + ph / 2, fill=MUTED, font=("Segoe UI", 10),
                          text={"cop": "No Copilot reading yet",
                                "or": "The limit of this key never resets" if usage is not None else "No OpenRouter reading yet",
                                }.get(win, "No active window - it starts with your next message"))
            self.draw_zoom_chip(False, 0)
            return
        span = span or SPAN_S[win]
        start = end - span
        f0, f1 = self.zoom or (0.0, 1.0)
        vs, ve = start + span * f0, start + span * f1  # visible part of the window

        def X(t):
            return L + (min(max(t, start), end) - vs) / (ve - vs) * pw

        def dots(x0, y0, x1, y1):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 7))
            for i in range(1, n + 1):
                x, y = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n
                if L <= x <= W - R:
                    c.create_oval(x - 1.4, y - 1.4, x + 1.4, y + 1.4, fill=color, outline="")

        # usage so far, from the governor log; a window starts empty
        pts = [(h[0], h[idx]) for h in hist if start <= h[0] <= now]
        if pts and pts[0][1] <= 2 and pts[0][0] - start < span * 0.02:
            pts.insert(0, (start, 0.0))
        pts.append((now, usage))
        xy = [(X(t), Y(v)) for t, v in pts]
        flat = [q for pt in xy for q in pt]
        c.create_polygon(xy[0][0], yb, *flat, xy[-1][0], yb, fill=AREA, outline="")
        grid()
        c.create_line(X(start), Y(0), X(end), Y(thr), fill=MUTED, dash=(5, 4))  # plan
        c.create_line(L, Y(thr), W - R, Y(thr), fill=WARN, dash=(6, 4))         # limit / pause point
        c.create_text(L + 4, Y(thr) - 3, text=f"limit {thr}%" if thr >= 100 else f"pause at {thr}%",
                      fill=WARN, font=small, anchor="sw")

        # x axis (follows the zoom): full hours or less for up to 2 days, day starts for a week, local midnights for a month
        days = span / 86400
        vlen = ve - vs
        axis = "hour" if vlen <= 2 * 86400 else "week" if win == "7d" or (win == "or" and days <= 8) else "month"
        if axis == "week":  # ticks at midnight, weekday centred under its day (partial first/last day: under the visible part)
            day = datetime.fromtimestamp(vs).replace(hour=0, minute=0, second=0, microsecond=0)
            while day.timestamp() < ve:
                t0, t1 = day.timestamp(), (day + timedelta(days=1)).timestamp()
                if t0 > vs:
                    c.create_line(X(t0), yb, X(t0), yb + 4, fill=MUTED)
                x0, x1 = X(max(t0, vs)), X(min(t1, ve))
                if x1 - x0 >= 28:
                    c.create_text((x0 + x1) / 2, yb + 6, text=day.strftime("%a"), fill=MUTED, font=small, anchor="n")
                day += timedelta(days=1)
        elif axis == "month":  # a tick at every local midnight, a date label every few days
            day = datetime.fromtimestamp(vs).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            step = max(1, math.ceil(46 / (pw * 86400 / vlen)))
            i = 0
            while day.timestamp() < ve:
                x = X(day.timestamp())
                c.create_line(x, yb, x, yb + 4, fill=MUTED)
                if i % step == 0 and x < W - R - 20:
                    c.create_text(x, yb + 6, text=day.strftime("%d.%m."), fill=MUTED, font=small, anchor="n")
                day += timedelta(days=1)
                i += 1
        else:  # ticks every 1 min ... 12 h, the finest spacing that leaves 50 px between labels
            step_min = next((m for m in (1, 5, 10, 15, 30, 60, 120, 180, 360, 720) if m * 60 / vlen * pw >= 50), 720)
            d = datetime.fromtimestamp(vs).replace(second=0, microsecond=0)
            if step_min < 60:
                d = d.replace(minute=d.minute // step_min * step_min)
            else:
                d = d.replace(minute=0, hour=d.hour // (step_min // 60) * (step_min // 60))
            d += timedelta(minutes=step_min)
            while d.timestamp() < ve:
                x = X(d.timestamp())
                if L + 8 <= x < W - R - 16:
                    c.create_line(x, yb, x, yb + 4, fill=MUTED)
                    c.create_text(x, yb + 6, text=d.strftime("%H:%M"), fill=MUTED, font=small, anchor="n")
                d += timedelta(minutes=step_min)

        if len(xy) >= 2:
            c.create_line(*flat, fill=BLUE, width=2, joinstyle="round")
        xn, yn = xy[-1]
        c.create_line(xn, T, xn, yb, fill="#4a505a")

        # projection at the average pace of this window (the same number the tray tooltip uses)
        if p and p["verdict"] == "too much":
            t_hit = now + p["eta_min"] * 60
            xc, yc = X(t_hit), Y(thr)
            dots(xn, yn, xc, yc)
            c.create_oval(xc - 5, yc - 5, xc + 5, yc + 5, outline=color, width=2)
            yk = yc - 12  # bracket: how much earlier than the reset the limit is reached
            c.create_line(xc, yk, W - R, yk, fill=color)
            c.create_line(xc, yk - 3, xc, yk + 3, fill=color)
            c.create_line(W - R, yk - 3, W - R, yk + 3, fill=color)
            c.create_text(W - R, yk - 5, text=f"{when(t_hit)}  -  {fmt_min(p['left_min'] - p['eta_min'])} early", fill=color, font=small, anchor="se")
        elif p and p["verdict"] in ("on plan", "too little"):
            xe, ye = X(end), Y(p["projected"])
            dots(xn, yn, xe, ye)
            c.create_text(W - R, ye + 16, text=f"{p['projected']:.0f}%", fill=color, font=small, anchor="ne")

        # gap to the plan line right now, in percentage points (rounded): + = ahead of plan (too much), - = budget left
        plan_now = thr * (min(max(now, start), end) - start) / span
        gap = round(usage - plan_now)
        yp = Y(plan_now)
        c.create_line(xn, yn, xn, yp, fill=color, width=2)
        c.create_oval(xn - 2.5, yp - 2.5, xn + 2.5, yp + 2.5, fill=color, outline="")
        label = f"{gap:+d} %"  # fallback where no token estimate exists (Copilot, <5 % usage)
        if win in ("5h", "7d") and usage >= 5:  # 1 % = (Paperclip tokens of this window) / usage: a lower-bound estimate
            used = tokens_between(start, now)
            if used:
                label = f"{'+' if usage >= plan_now else '-'}{fmt_tok((usage - plan_now) * used / usage)}"
        label = label.replace("-", "\u2212")
        font9 = ("Segoe UI", 9, "bold")
        tw = tkfont.Font(font=font9).measure(label)

        def plan_y(x):  # y of the plan line at pixel x
            return Y(thr * (min(max(x, L), L + pw) - L) / pw)
        # the free space around the marker: below the plan line when ahead of it, above it when behind
        if usage >= plan_now:
            if xn + 6 + tw <= W - R and yp + 18 <= yb:
                lx, ly, anc = xn + 6, yp + 4, "nw"
            elif xn + 6 + tw <= W - R:  # no room below the plan line (near the axis): between marker and plan, above the line
                lx, ly, anc = xn + 6, min((yn + yp) / 2, plan_y(xn + 6 + tw) - 9), "w"
            else:
                lx, ly, anc = xn - 6, plan_y(xn - 6 - tw) + 4, "ne"
        elif xn - 6 - tw >= L:
            lx, ly, anc = xn - 6, yp - 4, "se"
        else:
            lx, ly, anc = xn + 6, plan_y(xn + 6 + tw) - 4, "sw"
        c.create_text(lx, ly, text=label, fill=color, font=font9, anchor=anc)
        c.create_oval(xn - 4.5, yn - 4.5, xn + 4.5, yn + 4.5, fill=BLUE, outline=FG, width=2)
        if self.zoom is not None:  # the zoomed data runs over the margins: cover them, then redraw the axis labels
            c.create_rectangle(0, 0, L, H, fill=PANEL, outline="")
            c.create_rectangle(W - R, 0, W, H, fill=PANEL, outline="")
        ylabels()
        self.draw_zoom_chip(self.zoom is not None, yb)


def main():
    global _mutex
    # one window: a second launch raises the first and exits
    _mutex = _k32.CreateMutexW(None, False, pacer_config.mutex_name("PaperclipPacerDashboard"))
    if _k32.GetLastError() == 183:
        hwnd = _u32.FindWindowW(None, TITLE)
        if hwnd:
            _u32.ShowWindow(hwnd, 9)  # SW_RESTORE
            _u32.SetForegroundWindow(hwnd)
        sys.exit(0)
    # own AppUserModelID: the taskbar shows this window's icon instead of grouping it under pythonw
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    migrate_settings()
    i18n.install(tk)  # every widget text goes through i18n.tr(); must happen before any widget exists
    i18n.set_lang(i18n.lang_from(read_settings()))
    try:
        Dashboard().root.mainloop()
    except BaseException:  # pythonw has no stderr; leave a trace instead of vanishing
        LOGGER.error("dashboard\n%s", traceback.format_exc().rstrip())
        sys.exit(1)


if __name__ == "__main__":
    main()
