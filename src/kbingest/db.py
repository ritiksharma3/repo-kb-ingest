"""Database access. Postgres (psycopg3) is the default; KB_DB=mysql switches to MySQL 8.x via PyMySQL.

The MySQL path exposes the same small surface the rest of the code uses (connect / cursor / execute / fetch*,
commit / rollback, `with` blocks, insert_rows, fetch_bundle_tables, Jsonb) and papers over the differences:
JSON columns are decoded to Python objects on read and encoded on write, uuid values are plain strings,
TINYINT(1) boolean columns come back as bool, the internal `key_hash` primary-key helper column is hidden,
and the reserved words `change` / `procedure` / `lines` are back-quoted in SQL text.
"""
from __future__ import annotations
import functools
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb as _PgJsonb

PG_DEFAULTS = {
    "owner": "postgresql://kb_owner:kb_owner_dev@localhost:54329/kb",
    "writer": "postgresql://kb_writer:kb_writer_dev@localhost:54329/kb",
    "reader": "postgresql://kb_reader:kb_reader_dev@localhost:54329/kb",
}
MYSQL_DEFAULTS = {
    "owner": "mysql://root:@127.0.0.1:3306/kb",
    "writer": "mysql://kb_writer:kb_writer_dev@127.0.0.1:3306/kb",
    "reader": "mysql://kb_reader:kb_reader_dev@127.0.0.1:3306/kb",
}
DEFAULTS = PG_DEFAULTS      # kept for backward compatibility

BOOL_COLS = {"exported", "has_timeout"}       # TINYINT(1) columns that must read back as bool
HIDDEN_COLS = {"key_hash"}                    # MySQL-only generated primary-key helper


def load_dotenv(path: Path | None = None) -> None:
    """Tiny .env loader (KEY=VALUE lines); never overrides variables that are already set."""
    p = path or Path(os.environ.get("KB_PROJECT_ROOT", Path(__file__).resolve().parents[2])) / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def backend() -> str:
    return os.environ.get("KB_DB", "postgres").strip().lower()


def is_mysql() -> bool:
    return backend() == "mysql"


def url(role: str) -> str:
    my = is_mysql()
    v = os.environ.get(f"KB_{role.upper()}_URL")
    if v and v.startswith("mysql") != my:      # a leftover URL for the other backend (e.g. Postgres URLs in .env)
        v = None
    return v or (MYSQL_DEFAULTS if my else PG_DEFAULTS)[role]


def parse_mysql_url(u: str) -> dict:
    p = urlsplit(u)
    return {"host": p.hostname or "127.0.0.1", "port": p.port or 3306, "user": unquote(p.username or "root"),
            "password": unquote(p.password or ""), "database": (p.path or "/").lstrip("/") or None}


# ---------------- MySQL adapter ----------------

_RESERVED = re.compile(r"(?<![`\w])(change|procedure|lines)(?![`\w])")


@functools.lru_cache(maxsize=512)
def _rewrite(sql: str) -> str:
    return _RESERVED.sub(r"`\1`", sql)


def _conv_param(v):
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(timezone.utc).replace(tzinfo=None)
    return v


def _conv_params(params):
    if params is None:
        return None
    if isinstance(params, dict):
        return {k: _conv_param(v) for k, v in params.items()}
    params = tuple(_conv_param(v) for v in params)
    return params or None


def _fix_row(row):
    if row is None:
        return None
    for c in HIDDEN_COLS:
        row.pop(c, None)
    for c in BOOL_COLS:
        if row.get(c) is not None:
            row[c] = bool(row[c])
    return row


class _MyCursor:
    def __init__(self, cur):
        self._c = cur

    def execute(self, sql, params=None):
        self._c.execute(_rewrite(sql), _conv_params(params))
        return self

    def executemany(self, sql, seq):
        self._c.executemany(_rewrite(sql), [_conv_params(p) for p in seq])
        return self

    def fetchone(self):
        return _fix_row(self._c.fetchone())

    def fetchall(self):
        return [_fix_row(r) for r in self._c.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self):
        return self._c.lastrowid

    @property
    def rowcount(self):
        return self._c.rowcount

    def close(self):
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self._c.close()


class _MyConn:
    """psycopg-like wrapper: `with conn:` commits on success / rolls back on error, then closes."""

    def __init__(self, raw):
        self._raw = raw

    def cursor(self):
        return _MyCursor(self._raw.cursor())

    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                self._raw.commit()
            else:
                self._raw.rollback()
        finally:
            self._raw.close()


def mysql_connect(u: str, autocommit: bool = False, database: bool = True) -> _MyConn:
    import pymysql
    from pymysql.constants import FIELD_TYPE
    from pymysql.converters import conversions
    conv = dict(conversions)
    conv[FIELD_TYPE.JSON] = json.loads
    a = parse_mysql_url(u)
    raw = pymysql.connect(host=a["host"], port=a["port"], user=a["user"], password=a["password"],
                          database=a["database"] if database else None, charset="utf8mb4",
                          cursorclass=pymysql.cursors.DictCursor, conv=conv, autocommit=autocommit,
                          init_command="SET time_zone='+00:00', sql_mode='STRICT_TRANS_TABLES,NO_ENGINE_SUBSTITUTION', "
                                       "NAMES utf8mb4 COLLATE utf8mb4_0900_bin")
    return _MyConn(raw)


def connect(role: str, autocommit: bool = False):
    if is_mysql():
        return mysql_connect(url(role), autocommit)
    return psycopg.connect(url(role), row_factory=dict_row, autocommit=autocommit)


def Jsonb(v):
    """JSON parameter value for the active backend."""
    return json.dumps(v, ensure_ascii=False, default=str) if is_mysql() else _PgJsonb(v)


def _wrap(v):
    return Jsonb(v) if isinstance(v, (dict, list)) else v


def insert_rows(conn, table: str, rows: list[dict], **common) -> None:
    if not rows:
        return
    cols = sorted(rows[0].keys()) + sorted(common)
    if is_mysql():
        sql = f"INSERT INTO {table} ({', '.join('`' + c + '`' for c in cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
        data = [tuple(r[c] for c in sorted(r)) + tuple(common[c] for c in sorted(common)) for r in rows]
        with conn.cursor() as cur:
            for i in range(0, len(data), 200):
                cur.executemany(sql, data[i:i + 200])
        return
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})"
    with conn.cursor() as cur:
        for r in rows:
            vals = [_wrap(r[c]) for c in sorted(r)] + [common[c] for c in sorted(common)]
            cur.execute(sql, vals)


def fetch_bundle_tables(conn, snapshot_id: int, names: list[str]) -> dict[str, list[dict]]:
    out = {}
    with conn.cursor() as cur:
        for n in names:
            cur.execute(f"SELECT * FROM {n} WHERE snapshot_id = %s", (snapshot_id,))
            out[n] = list(cur.fetchall())
    return out
