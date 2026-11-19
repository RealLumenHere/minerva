"""fm_db.py — shared connection + write helpers for the canonical fleet_memory DB (wq#3647).

The password is NEVER stored here. It is read at call time from the LiteLLM
DATABASE_URL in /home/lumen/agent/.env, and only the host/port/dbname are
overridden so we target the fleet_memory DB on the host-exposed port (5433)
rather than litellm on the docker-internal hostname.
"""
import json
import os
import re
from urllib.parse import urlsplit

ENV_FILE = os.environ.get("FM_ENV_FILE", "/home/lumen/agent/.env")
FM_HOST = os.environ.get("FM_HOST", "127.0.0.1")
FM_PORT = int(os.environ.get("FM_PORT", "5433"))
FM_DBNAME = os.environ.get("FM_DBNAME", "fleet_memory")


def _read_database_url(env_file=ENV_FILE):
    with open(env_file, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = re.match(r"\s*DATABASE_URL\s*=\s*(.+?)\s*$", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    raise RuntimeError("DATABASE_URL not found in %s" % env_file)


def connect():
    """Return a psycopg2 connection to the fleet_memory DB (autocommit off)."""
    import psycopg2  # local import so --help etc. work without the driver
    parts = urlsplit(_read_database_url())
    conn = psycopg2.connect(
        host=FM_HOST,
        port=FM_PORT,
        dbname=FM_DBNAME,
        user=parts.username,
        password=parts.password,
    )
    return conn


def log_event(cur, actor, operation, object_type, object_id, before, after, reason):
    """Append a row to memory_events. before/after are dicts (or None)."""
    cur.execute(
        """INSERT INTO memory_events
             (actor, operation, object_type, object_id, before, after, reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (
            actor,
            operation,
            object_type,
            object_id,
            json.dumps(before) if before is not None else None,
            json.dumps(after) if after is not None else None,
            reason,
        ),
    )
