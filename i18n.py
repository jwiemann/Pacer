"""English -> German UI translation for the Pacer (dashboard and tray).

The code keeps writing English text. tr() translates it at the last moment: install() makes tkinter run every
widget / canvas text through tr(), and remembers the English original on the widget (_en) so retranslate() can
switch the language of an open window without rebuilding it. Dynamic texts ("in 3h 59m", "Last run: 5m ago") are
matched by RULES; anything unknown stays English. Language: settings key "lang" ("en" / "de"), default = Windows UI language.
"""
import ctypes, re

LANG = "en"


def detect():
    try:
        return "de" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0xFF == 0x07 else "en"
    except Exception:
        return "en"


def lang_from(settings):
    v = (settings or {}).get("lang")
    return v if v in ("en", "de") else detect()


def set_lang(lang):
    global LANG
    LANG = lang


EXACT = {
    # panels, tabs, rows
    "Status": "Status", "Services": "Dienste", "Settings": "Einstellungen", "Month": "Monat",
    "Spend over the last minute, per second, in euro cents (euros from 1 \u20ac)": "Ausgaben der letzten Minute pro Sekunde, in Euro-Cent (ab 1 \u20ac in Euro)",
    "OpenRouter's own prices (USD at the ECB rate): omp replies and OpenCode agents": "Originalpreise von OpenRouter (USD zum EZB-Kurs): omp-Antworten und OpenCode-Agenten",
    "at API list prices (USD at the ECB rate):": "zu API-Listenpreisen (USD zum EZB-Kurs):",
    "Claude and Copilot are flat plans, only OpenRouter is billed per use.": "Claude und Copilot sind Pauschalen, nur OpenRouter wird je Nutzung abgerechnet.",
    "omp replies and OpenCode agents only": "nur omp-Antworten und OpenCode-Agenten",
    "Claude login": "Claude-Login", "Copilot login": "Copilot-Login",
    "Throughput 5h": "Durchsatz 5h", "Throughput 7d": "Durchsatz 7d", "Copilot usage": "Copilot-Nutzung",
    "Resets 5h": "Reset 5h", "Resets 7d": "Reset 7d", "Copilot resets": "Copilot-Reset",
    "OpenRouter key": "OpenRouter-Key", "OpenRouter usage": "OpenRouter-Nutzung", "OpenRouter resets": "OpenRouter-Reset",
    # settings
    "Use full budget": "Volles Budget nutzen", "Pause agents at (%)": "Agenten pausieren ab (%)",
    "Resume below (%)": "Fortsetzen unter (%)", "Apply": "Übernehmen", "Pause when too fast": "Pausieren, wenn zu schnell",
    "Back": "Zurück", "General": "Allgemein",
    "The only limit is the real one: agents pause at 100%.": "Es gilt nur das echte Limit: Agenten pausieren bei 100%.",
    "Also pauses the agents while the usage runs clearly ahead of the plan (red), until it is back on the plan line.":
        "Pausiert die Agenten auch, solange die Nutzung deutlich über dem Plan liegt (rot), bis sie wieder auf der Planlinie ist.",
    "Claude agents": "Claude-Agenten", "Copilot agents": "Copilot-Agenten", "OpenRouter agents": "OpenRouter-Agenten",
    "running": "läuft", "paused": "pausiert", "draining": "läuft aus",
    "usage is ahead of the plan": "Nutzung liegt über dem Plan", "limit reached": "Limit erreicht",
    "no usage reading": "kein Nutzungswert", "saved": "Gespeichert",
    "Split token graph by model": "Token-Graph nach Modell aufteilen",
    "The Tokens graph shows one band per model family (Opus, Sonnet, Haiku ...) instead of one area per provider.":
        "Der Token-Graph zeigt ein Band je Modellfamilie (Opus, Sonnet, Haiku ...) statt einer Fläche je Anbieter.",
    "Keep PC awake": "PC wach halten", "Sleep when Paperclip is idle": "Ruhezustand, wenn Paperclip untätig ist",
    "Prevents Windows from sleeping while the Pacer tray runs. The screen may still turn off and the PC may lock.":
        "Verhindert, dass Windows in den Ruhezustand geht, solange das Pacer-Tray läuft. Der Bildschirm darf sich ausschalten und der PC sich sperren.",
    "The PC may sleep 10 min after the last agent run ended (no agent running, no update in progress). Needs 'Keep PC awake'.":
        "Der PC darf 10 Min nach dem Ende des letzten Agentenlaufs schlafen (kein Agent läuft, kein Update aktiv). Benötigt 'PC wach halten'.",
    # buttons and menu
    "Update Paperclip": "Paperclip aktualisieren", "Refresh usage": "Nutzung aktualisieren",
    "Open Paperclip": "Paperclip öffnen", "Restart Paperclip": "Paperclip neu starten",
    "Claude sign-in": "Claude-Anmeldung", "Copilot sign-in": "Copilot-Anmeldung",
    "Confirm update?": "Update bestätigen?", "Confirm restart?": "Neustart bestätigen?",
    "Check for Paperclip update": "Nach Paperclip-Update suchen", "Open watchdog log": "Watchdog-Log öffnen",
    "Open governor log": "Governor-Log öffnen", "Connect Paperclip...": "Paperclip verbinden...",
    "Connect GitHub Copilot...": "GitHub Copilot verbinden...", "Connect OpenRouter...": "OpenRouter verbinden...",
    "Restart Pacer": "Pacer neu starten",
    "Start with Windows": "Mit Windows starten",
    "click the red button within 5 s to restart Paperclip": "Roten Button innerhalb von 5 s drücken, um Paperclip neu zu starten",
    "Status and services": "Status und Dienste",
    "Quit": "Beenden",
    "Claude not signed in": "Claude nicht angemeldet", "Claude Code not installed": "Claude Code nicht installiert",
    "Claude login expired": "Claude-Login abgelaufen", "Get Claude Code": "Claude Code holen",
    "install Claude Code with the page that just opened, then press the button again to sign in":
        "Claude Code mit der soeben geöffneten Seite installieren, danach den Button erneut drücken und anmelden",
    # messages
    "restarting Paperclip, takes about a minute": "Paperclip wird neu gestartet, dauert etwa eine Minute",
    "finish the sign-in in the console window; the governor syncs the new token within 3 minutes":
        "Anmeldung im Konsolenfenster abschließen; der Governor übernimmt den neuen Token innerhalb von 3 Minuten",
    "follow the steps in the console window (GitHub device code)": "Den Schritten im Konsolenfenster folgen (GitHub-Gerätecode)",
    "no WSL: sign in to GitHub Copilot in VS Code, the Pacer picks that login up within minutes":
        "Kein WSL: In VS Code bei GitHub Copilot anmelden, der Pacer übernimmt den Login innerhalb weniger Minuten",
    "looking for Paperclip...": "Suche nach Paperclip...", "Paperclip found": "Paperclip gefunden",
    "no Paperclip found: install it in WSL, or set wslDistro / url in pacer-config.json":
        "Kein Paperclip gefunden: in WSL installieren oder wslDistro / url in pacer-config.json setzen",
    "PC may sleep 10 min after Paperclip goes idle": "PC darf 10 Min nach Ende der Paperclip-Aktivität schlafen",
    "PC stays awake while 'Keep PC awake' is on": "PC bleibt wach, solange 'PC wach halten' aktiv ist",
    "keep awake on: PC will not sleep": "Wach halten an: PC schläft nicht", "keep awake off": "Wach halten aus",
    "enter whole numbers": "Ganze Zahlen eingeben",
    "need 1 <= resume <= pause <= 100": "Es muss gelten: 1 <= Fortsetzen <= Pause <= 100",
    # status texts
    "not checked yet": "noch nicht geprüft", "check failed": "Prüfung fehlgeschlagen", "up to date": "aktuell",
    "checking...": "prüfe...", "no recent data": "keine aktuellen Daten", "sign-in required": "Anmeldung erforderlich",
    "no login found - sign in": "kein Login gefunden - anmelden",
    "token expired, renews on next check": "Token abgelaufen, wird bei der nächsten Prüfung erneuert",
    "FAILED": "FEHLGESCHLAGEN", "unknown": "unbekannt", "healthy": "gesund", "DOWN": "AUS", "unlimited": "unbegrenzt",
    "tray not running": "Tray läuft nicht", "not run yet": "noch nicht gelaufen", "check requested": "Prüfung angefordert",
    "just now": "gerade eben", "never": "nie",
    "Too fast": "Zu schnell", "Too slow": "Zu langsam", "At limit": "Am Limit", "Too early to tell": "Noch zu früh",
    "see governor log": "siehe Governor-Log",
    "Paperclip server is down": "Paperclip-Server ist nicht erreichbar",
    "Watchdog failing - see watchdog log": "Watchdog-Fehler - siehe Watchdog-Log", "No status yet": "Noch kein Status",
    "installed version not found in WSL": "installierte Version in WSL nicht gefunden",
    "the update script stopped reporting (see update-paperclip.log)": "das Update-Skript meldet sich nicht mehr (siehe update-paperclip.log)",
    "SERVER DOWN": "SERVER AUS", "governor": "Governor", "watchdog": "Watchdog",
    "full budget: pause at 100%": "volles Budget: Pause bei 100%",
    # update steps (updater.py)
    "starting": "startet", "checking installed version": "prüfe installierte Version", "checking agents": "prüfe Agenten",
    "waiting for the watchdog": "warte auf den Watchdog", "backing up database": "sichere Datenbank",
    "stopping server": "stoppe Server", "restarting server after failure": "starte Server nach Fehler neu",
    "starting server": "starte Server", "dry run ok": "Testlauf ok",
    # chart
    "No Copilot reading yet": "Noch kein Copilot-Wert", "No OpenRouter reading yet": "Noch kein OpenRouter-Wert",
    "The limit of this key never resets": "Das Limit dieses Keys wird nie zurückgesetzt",
    "No active window - it starts with your next message": "Kein aktives Fenster - es startet mit deiner nächsten Nachricht",
    "No AI activity": "Keine KI-Aktivität",
    "pace: too early to tell (tentative)": "Tempo: noch zu früh für eine Aussage (vorläufig)",
    "pace: limit reached": "Tempo: Limit erreicht",
}

DAYS = {"Mon": "Mo", "Tue": "Di", "Wed": "Mi", "Thu": "Do", "Fri": "Fr", "Sat": "Sa", "Sun": "So"}
STATES = {"running": "läuft", "draining": "läuft aus", "paused": "pausiert"}
DUR = r"(\d+[dhm](?: \d+[dhm])*|\d+s)"


def dur(x):
    """'3h 59m' -> '3 Std 59 Min'"""
    return re.sub(r"(\d+)([dhms])", lambda m: f"{m[1]} {({'d': 'T', 'h': 'Std', 'm': 'Min', 's': 's'})[m[2]]}", x)


def _rule(pattern, fn):
    return re.compile(pattern), fn


RULES = [
    _rule(rf"in {DUR}", lambda m: f"in {dur(m[1])}"),
    _rule(rf"{DUR} ago", lambda m: f"vor {dur(m[1])}"),
    _rule(rf"{DUR} early", lambda m: f"{dur(m[1])} früher"),
    _rule(rf"Governor status is {DUR} old", lambda m: f"Governor-Status ist {dur(m[1])} alt"),
    _rule(rf"usage reading {DUR} old", lambda m: f"Nutzungswert ist {dur(m[1])} alt"),
    _rule(rf"token {DUR}", lambda m: f"Token {dur(m[1])}"),
    _rule(r"login (\d+)d", lambda m: f"Login {m[1]} T"),
    _rule(rf"login ends in {DUR}", lambda m: f"Login endet in {dur(m[1])}"),
    _rule(r"Last run: (.*)", lambda m: "Letzter Lauf: " + tr(m[1])),
    _rule(r"Last reading (.*) \(is the tray running\?\)", lambda m: f"Letzter Wert {tr(m[1])} (läuft das Tray?)"),
    _rule(r"([\d.,]+) of ([\d.,]+) credits used", lambda m: f"{m[1]} von {m[2]} Credits verwendet"),
    _rule(r"([+-]?[\d.]+) %/h over the last hour", lambda m: f"{m[1]} %/h in der letzten Stunde"),
    _rule(r"limit (\d+)%", lambda m: f"Limit {m[1]}%"),
    _rule(r"pause at (\d+)%", lambda m: f"Pause bei {m[1]}%"),
    _rule(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun)( \d[\d.: ]*)?", lambda m: DAYS[m[1]] + (m[2] or "")),
    _rule(r"update available: (.*)", lambda m: f"Update verfügbar: {m[1]}"),
    _rule(r"Update to (.*)", lambda m: f"Update auf {m[1]}"),
    _rule(r"Retry update to (.*)", lambda m: f"Update auf {m[1]} wiederholen"),
    _rule(r"updating: (.*)\.\.\.", lambda m: f"aktualisiere: {tr(m[1])}..."),
    _rule(r"installing (.*)", lambda m: f"installiere {m[1]}"),
    _rule(r"updated to (.*)", lambda m: f"aktualisiert auf {m[1]}"),
    _rule(r"Installed: (.*)", lambda m: f"Installiert: {m[1]}"),
    _rule(r"Last update failed: (.*)", lambda m: f"Letztes Update fehlgeschlagen: {m[1]}"),
    _rule(r"Updating (.*) -> (.*)\. The server is down for part of this \(about 6 min\)\.",
          lambda m: f"Aktualisiere {m[1]} -> {m[2]}. Der Server ist dabei zeitweise nicht erreichbar (etwa 6 Min)."),
    _rule(r"Governor failing: (.*)", lambda m: f"Governor-Fehler: {tr(m[1])}"),
    _rule(r"status (\d+)m old", lambda m: f"Status {m[1]} Min alt"),
    # tray tooltip
    _rule(r"(5h|7d) usage ([\d.]+)% - (running|draining|paused)", lambda m: f"{m[1]}-Nutzung {m[2]}% - {STATES[m[3]]}"),
    _rule(r"usage unknown - (running|draining|paused)", lambda m: f"Nutzung unbekannt - {STATES[m[1]]}"),
    _rule(rf"pace: TOO MUCH - limit in ~{DUR}", lambda m: f"Tempo: ZU VIEL - Limit in ~{dur(m[1])}"),
    _rule(r"pace: too little - ends ~(\d+)%", lambda m: f"Tempo: zu wenig - endet bei ~{m[1]}%"),
    _rule(r"pace: on plan - ends ~(\d+)%", lambda m: f"Tempo: im Plan - endet bei ~{m[1]}%"),
    _rule(rf"(resumes below (\d+)%|resumes when back on plan|pauses at (\d+)%|full budget: pause at 100%)( \(applying\.\.\.\))?(?: - resets in {DUR})?",
          lambda m: (f"setzt unter {m[2]}% fort" if m[2] else f"pausiert bei {m[3]}%" if m[3]
                     else "setzt fort, sobald wieder im Plan" if m[1].startswith("resumes") else "volles Budget: Pause bei 100%")
          + (" (wird angewendet...)" if m[4] else "") + (f" - Reset in {dur(m[5])}" if m[5] else "")),
    _rule(rf"reset in {DUR}", lambda m: f"Reset in {dur(m[1])}"),
    _rule(r"(Claude|Copilot|OpenRouter) (paused|draining)", lambda m: f"{m[1]} {STATES[m[2]]}"),
]


def _tr_line(s):
    if s in EXACT:
        return EXACT[s]
    for rx, fn in RULES:
        m = rx.fullmatch(s)
        if m:
            return fn(m)
    for sep in ("  -  ", "  \u00b7 ", ", "):  # composite texts: translate the parts
        if sep in s:
            parts = s.split(sep)
            out = [_tr_line(p) for p in parts]
            if out != parts:
                return sep.join(out)
    if len(s) > 2 and s.isupper():  # panel titles are upper-cased by the dashboard
        k = s.title()
        if k in EXACT:
            return EXACT[k].upper()
    return s


def tr(s):
    if LANG != "de" or not isinstance(s, str) or not s:
        return s
    return "\n".join(_tr_line(x) for x in s.split("\n"))


def install(tk):
    """Route every widget / canvas text of tkinter `tk` through tr(); the English original stays on the widget (_en)."""
    base_init, base_cfg, canvas_text = tk.BaseWidget.__init__, tk.Misc.configure, tk.Canvas.create_text

    def init(self, master, widgetName, cnf={}, kw={}, extra=()):
        cnf = tk._cnfmerge((cnf, kw))
        if isinstance(cnf.get("text"), str):
            self._en = cnf["text"]
            cnf = {**cnf, "text": tr(self._en)}
        base_init(self, master, widgetName, cnf, {}, extra)

    def configure(self, cnf=None, **kw):
        if isinstance(kw.get("text"), str):
            self._en = kw["text"]
            kw["text"] = tr(self._en)
        return base_cfg(self, cnf, **kw)

    def create_text(self, *args, **kw):
        if isinstance(kw.get("text"), str):
            kw["text"] = tr(kw["text"])
        return canvas_text(self, *args, **kw)
    tk.BaseWidget.__init__ = init
    tk.Misc.configure = tk.Misc.config = configure
    tk.Canvas.create_text = create_text


def retranslate(widget):
    """Re-apply the current language to every widget below `widget` that carries an English original."""
    for w in widget.winfo_children():
        en = getattr(w, "_en", None)
        if en is not None:
            try:
                w.configure(text=en)
            except Exception:
                pass
        retranslate(w)
