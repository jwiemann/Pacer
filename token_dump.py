"""Claude token usage of the Paperclip heartbeat runs of the last 8 days, for the dashboard (token-usage.json).

Reads the Paperclip database directly with pg8000 (pure Python): the database is PostgreSQL inside WSL, reachable on
127.0.0.1 from Windows, so no WSL process and no Node runtime is started. Any failure (database not reachable, no
Paperclip) raises; the caller decides how quiet that is.
"""
import math, time
from contextlib import closing

import pg8000.dbapi

import fsutil

SQL = """select coalesce(finished_at, updated_at) as t,
      (usage_json->>'inputTokens')::bigint as i, (usage_json->>'outputTokens')::bigint as o,
      (usage_json->>'cachedInputTokens')::bigint as c, (usage_json->>'costUsd')::float8 as usd
    from heartbeat_runs
    where usage_json->>'provider' = 'anthropic' and usage_json->>'inputTokens' is not null
      and coalesce(finished_at, updated_at) > now() - interval '8 days' order by 1"""
CONNECT_TIMEOUT_S = 20


def dump(out_path, db):
    """Writes {"at": epoch, "rows": [[epoch, input, output, cached, usd], ...]} to out_path (atomically).
    `db` is the connection dict of pacer_config (host, port, user, password, database)."""
    with closing(pg8000.dbapi.connect(host=db["host"], port=db["port"], user=db["user"], password=db["password"],
                                      database=db["database"], timeout=CONNECT_TIMEOUT_S)) as conn:
        cur = conn.cursor()
        cur.execute(SQL)
        rows = [[0 if t is None else math.floor(t.timestamp()), i or 0, o or 0, c or 0, float(usd or 0)]
                for t, i, o, c, usd in cur.fetchall()]
    fsutil.write_json_atomic(out_path, {"at": math.floor(time.time()), "rows": rows})
