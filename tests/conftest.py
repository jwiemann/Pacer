"""Isolation for the Pacer test suite.

Runs before any test module and before any Pacer module is imported: PACER_HOME and the user profile point at a
throwaway directory tree. pacer_config creates its data dir at import, governor.py derives its credentials path from the
home directory, and the tray's single-instance mutex is keyed by PACER_HOME, so nothing here can reach Jens's real data
dir, his ~/.claude credentials or his running tray.
"""
import atexit, json, os, shutil, tempfile

_BASE = tempfile.mkdtemp(prefix="pacer-tests-")
atexit.register(shutil.rmtree, _BASE, True)
DATA_DIR = os.path.join(_BASE, "data")
HOME = os.path.join(_BASE, "home")
os.makedirs(DATA_DIR)
os.makedirs(HOME)
os.environ["PACER_HOME"] = DATA_DIR
os.environ["USERPROFILE"] = HOME  # os.path.expanduser on Windows
os.environ["HOME"] = HOME
os.environ["LOCALAPPDATA"] = os.path.join(_BASE, "localappdata")

import pacer_config  # noqa: E402  (must come after the environment is set)
from fake_paperclip import point_pacer_at  # noqa: E402

assert pacer_config.DIR == DATA_DIR, "pacer_config resolved a data dir outside the test sandbox"

# Pre-resolved config, so pacer_config never probes WSL or schtasks. Tests that need Paperclip re-point it at their server.
point_pacer_at("http://127.0.0.1:9")
# Default test settings: Paperclip off. Each test that needs Paperclip writes its own settings.
with open(os.path.join(DATA_DIR, "claude-governor-settings.json"), "w", encoding="utf-8") as f:
    json.dump({"paperclip": False}, f)
