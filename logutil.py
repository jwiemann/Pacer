"""Rotating log files in the Pacer data dir. Every line starts with a UTC timestamp such as 2026-11-09T12:00:00.123Z, the
format the governor, watchdog and updater logs always had: dashboard.py parses claude-governor.log, so that line format
and the `.1`, `.2` backup names must not change."""
import logging, logging.handlers, os, time
from datetime import datetime, timezone

import pacer_config

MAX_BYTES, BACKUPS = 1_000_000, 2


def iso_now(epoch=None):
    """UTC time like JS toISOString(): 2026-11-08T12:00:00.123Z"""
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


class _Formatter(logging.Formatter):
    def format(self, record):
        return f"{iso_now(record.created)} {record.getMessage()}"


class _FileHandler(logging.handlers.RotatingFileHandler):
    """Opens the file for each record and closes it again. Several Pacer processes append to the same logs, and Windows
    cannot rename a file another process holds open, so no handle may stay open between records. A rotation that
    another process blocks is retried with the next record."""

    def emit(self, record):
        try:
            super().emit(record)
        finally:
            self.close()

    def doRollover(self):
        try:
            super().doRollover()
        except PermissionError:
            pass


def get_logger(name, filename):
    """Logger that appends to DIR/<filename> (rotated at 1 MB, two backups). Asking again returns the same logger."""
    logger = logging.getLogger("pacer." + name)
    if not logger.handlers:
        handler = _FileHandler(os.path.join(pacer_config.DIR, filename), maxBytes=MAX_BYTES, backupCount=BACKUPS,
                               encoding="utf-8", delay=True)
        handler.setFormatter(_Formatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
