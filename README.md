# Pacer

Windows tray app that watches your Claude usage limits (5 h and 7 d windows), shows pace and forecast in a dashboard
and, if you use [Paperclip](https://github.com/paperclipai/paperclip), pauses and resumes its Claude agents so the
limits are not blown. Optional GitHub Copilot quota monitor and OpenRouter key-limit monitor. UI in English and German.

## Requirements

- Windows 10/11, Python 3.11+ with tkinter (only for running from source; the installer bundles everything)
- Claude Code CLI, logged in (`claude auth login`): Pacer reads `~/.claude/.credentials.json` and refreshes the token
  through the CLI. Usage comes from Anthropic's **undocumented** OAuth usage endpoint (whole-percent resolution); it
  can change without notice. Pacer then logs `usage endpoint schema changed` and fails closed.
- Paperclip in WSL is optional and auto-detected (`settings.json` key `paperclip` true/false overrides).

## Run from source

```powershell
pip install -r requirements.txt
python pacer.py                              # tray + dashboard
python pacer.py governor --dry-run           # one limit check without pausing anything
python pacer.py detect                       # show the detected environment
```

Roles are documented in the docstring of `pacer.py`.

## Layout

| Module | Role |
| --- | --- |
| `pacer.py` | entry point / subcommands |
| `tray.py`, `tray_icon.py` | tray icon and background jobs (governor, watchdog, Copilot and OpenRouter monitors) |
| `governor.py` | one limit check: usage poll, pause/resume of each provider's agents (Claude: `claude_local`, Copilot: `opencode_local` on a `github-copilot/` model, OpenRouter: `opencode_local` / `pi_local` on an `openrouter/` model) |
| `openrouter_monitor.py` | spend limit of the OpenRouter API key (`OPENROUTER_API_KEY`, user environment or `openrouter.key` in the data dir) |
| `throttle.py` | per-provider throttle settings (`providers.<name>` in the settings file) and the pause / resume rule |
| `watchdog.py`, `watchdog-inner.cjs` | Paperclip server / hung-run watchdog (the `.cjs` runs inside WSL) |
| `updater.py` | manual Paperclip update procedure |
| `dashboard.py`, `ui_widgets.py`, `winpos.py`, `dashboard_data.py`, `live_chart.py`, `live_tokens.py` | UI and its Tk-free data layer |
| `pacer_config.py` | environment detection and overrides, data dir |
| `fsutil.py`, `http_util.py`, `logutil.py` | atomic writes and locks, HTTP helper, rotating logs |
| `packaging/` | PyInstaller spec, Inno Setup installer, build script |

## Data

Settings, state and logs live outside the code: `PACER_HOME` if set, else an existing `~/.paperclip` (older installs),
else `%LOCALAPPDATA%\Pacer`. Tests and experiments MUST set `PACER_HOME` to a throwaway directory; the tray and dashboard
are single-instance (named mutex per data dir) and a start without it raises the running Pacer window instead.

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -q
```

## Build

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
```

Produces `packaging\dist\Pacer\Pacer.exe` and, if Inno Setup 6 is installed, `packaging\dist\Pacer-Setup.exe`
(per user, no admin, unsigned: SmartScreen will warn).

## Moving an old install

`packaging\migrate-from-paperclip-dir.ps1` (elevated, run once) re-points the keepalive task and the autostart entry
to this folder and removes the old code from `~/.paperclip`.
