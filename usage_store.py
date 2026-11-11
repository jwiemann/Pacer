"""Claude usage history: one row per successful usage poll of the governor (every 3 min), kept 30 days.

SQLite in WAL mode: the governor (tray process) writes, the dashboard (another process) reads, and readers never block
the writer. Connections are opened per call and closed again, so no handle stays open on the file (Windows would not let
another process replace or delete it meanwhile).

The governor's human log (claude-governor.log) is still written as before. Its history from before this store existed
is imported once, on first use (migrate_log); a flag row written in the same transaction makes that run exactly once.
"""
import os, re, sqlite3, time
from collections import namedtuple
from contextlib import closing
from datetime import datetime

import fsutil, logutil
import pacer_config

PATH = os.path.join(pacer_config.DIR, "usage-history.sqlite")
LOG = os.path.join(pacer_config.DIR, "claude-governor.log")  # migration source (its rotated copy is LOG + ".1")
RETENTION_S = 30 * 86400
LOG_LINE = re.compile(r"^(\S+Z) 5h ([\d.]+)% \(resets (\S+?)\), 7d ([\d.]+)% \(resets (\S+?)\)")
SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    ts REAL PRIMARY KEY,   -- epoch seconds of the poll, millisecond precision
    five REAL NOT NULL,    -- 5h utilisation in percent
    seven REAL NOT NULL,   -- 7d utilisation in percent
    resets5 TEXT,          -- next 5h reset as the usage endpoint names it (ISO 8601), NULL when it named none
    resets7 TEXT           -- next 7d reset, same form
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

Usage = namedtuple("Usage", "ts five seven resets5 resets7")


def _open():
    """Connection with the schema in place and the log migrated. Migration runs under a lock, so the governor and the
    dashboard starting at the same time import the history only once."""
    conn = sqlite3.connect(PATH, timeout=10, isolation_level=None)  # autocommit; transactions are explicit
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        if not _migrated(conn):
            with fsutil.file_lock(PATH + ".lock"):
                if not _migrated(conn):
                    migrate_log(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _migrated(conn):
    return conn.execute("SELECT 1 FROM meta WHERE k = 'log_migrated'").fetchone() is not None


def migrate_log(conn):
    """Imports every poll line of the governor log (rotated copy first, so rows stay in time order)."""
    found = []
    for path in (LOG + ".1", LOG):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    m = LOG_LINE.match(ln)
                    if m:
                        found.append((datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp(),
                                      float(m.group(2)), float(m.group(4)),
                                      None if m.group(3) == "null" else m.group(3),
                                      None if m.group(5) == "null" else m.group(5)))
        except OSError:
            pass
    with conn:  # one transaction: the rows and the flag land together or not at all
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("INSERT OR IGNORE INTO usage VALUES (?, ?, ?, ?, ?)", found)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('log_migrated', ?)", (logutil.iso_now(),))


def append(five, seven, resets5=None, resets7=None):
    """Records the successful usage poll that just happened and prunes what is past the retention."""
    now = round(time.time(), 3)
    with closing(_open()) as conn:
        conn.execute("INSERT INTO usage VALUES (?, ?, ?, ?, ?)", (now, five, seven, resets5, resets7))
        conn.execute("DELETE FROM usage WHERE ts < ?", (now - RETENTION_S,))


def rows(since=None):
    """Stored polls as Usage(ts, five, seven, resets5, resets7), oldest first; only those with ts >= since if given."""
    with closing(_open()) as conn:
        return [Usage(*r) for r in conn.execute(
            "SELECT ts, five, seven, resets5, resets7 FROM usage WHERE ts >= ? ORDER BY ts", (since or 0,))]


def resets():
    """[(epoch, "5h"|"7d")] of every window reset a stored poll named as its next reset, rounded to the minute: the
    API's microseconds jitter between polls, so one reset is named by several values."""
    seen = set()
    with closing(_open()) as conn:
        for col, win in (("resets5", "5h"), ("resets7", "7d")):
            for (raw,) in conn.execute(f"SELECT DISTINCT {col} FROM usage WHERE {col} IS NOT NULL"):
                seen.add((round(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() / 60) * 60, win))
    return sorted(seen)


def version():
    """(row count, newest ts): changes whenever a poll is appended or pruned, so readers can skip re-reading."""
    with closing(_open()) as conn:
        return conn.execute("SELECT count(*), max(ts) FROM usage").fetchone()
