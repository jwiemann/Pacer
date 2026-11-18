"""OpenRouter key overview: run by the tray every 5 min (also standalone: python openrouter_keys.py).

With a management key (stored encrypted, see secret_store.py) it lists every API key of the account
(GET /api/v1/keys) and writes openrouter-keys.json: per key its limit, spend and the share of the limit used. The
dashboard's Keys tab shows them all. Without a management key nothing is asked and nothing is written. The management
key never leaves secret_store, this module and the Authorization header of that one request.
"""
import json, os, sys, time

import fsutil, http_util, openrouter_monitor, pacer_config, secret_store

DIR = pacer_config.DIR
STATUS = os.path.join(DIR, "openrouter-keys.json")
KEY_FILE = secret_store.OPENROUTER_MGMT
URL = "https://openrouter.ai/api/v1/keys"
PAGE, MAX_PAGES = 100, 10


def read_status():
    """Latest listing ({} if never run)."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def key_reading(d):
    """One entry of the list endpoint as the dashboard shows it: the limit reading of openrouter_monitor.reading() plus
    the identity (hash, name, OpenRouter's own shortened label) and the spend of today / this week / this month."""
    out = openrouter_monitor.reading(d)
    out.update(hash=d.get("hash"), name=d.get("name") or d.get("label") or "-", disabled=bool(d.get("disabled")),
               usage=d.get("usage"), daily=d.get("usage_daily"), weekly=d.get("usage_weekly"), monthly=d.get("usage_monthly"),
               last_used=d.get("last_used_at"))
    out.pop("state", None)
    return out


def fetch(key):
    """All keys of the account, newest first, as a list of key_reading(). Raises http_util.HttpError."""
    keys = []
    for page in range(MAX_PAGES):
        d = http_util.request_json(f"{URL}?offset={page * PAGE}", headers={"Authorization": "Bearer " + key, "Accept": "application/json"},
                                   timeout=30)
        data = (d or {}).get("data") if isinstance(d, dict) else None
        if not isinstance(data, list):
            raise ValueError("unexpected answer")
        keys += [key_reading(k) for k in data if isinstance(k, dict)]
        if len(data) < PAGE:
            break
    return keys


def write_status(st):
    if st["state"] == "error":  # transient failure: keep the last list
        st = {**{k: v for k, v in read_status().items() if k == "keys"}, **st}
    st["at"] = time.time()
    fsutil.write_json_atomic(STATUS, st)


def main():
    key = secret_store.load(KEY_FILE)
    if not key:
        if os.path.exists(STATUS):  # the management key was removed: forget what it showed
            os.remove(STATUS)
        return
    try:
        write_status({"state": "ok", "keys": fetch(key)})
    except http_util.HttpError as e:
        if e.status in (401, 403):
            write_status({"state": "signin", "keys": [], "error": f"OpenRouter rejected the management key (HTTP {e.status})"})
        else:
            write_status({"state": "error", "error": f"OpenRouter API HTTP {e.status}" if e.status else str(e)[:150]})
    except ValueError as e:
        write_status({"state": "error", "error": f"OpenRouter key list: {e}"})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never print a key: only the exception text
        print(f"openrouter keys failed: {e}", file=sys.stderr)
        sys.exit(1)
