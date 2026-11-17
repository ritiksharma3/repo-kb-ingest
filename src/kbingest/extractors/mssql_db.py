"""SQL Server access via the `mssql` library; SQL parsed with sqlglot (tsql dialect)."""
from __future__ import annotations
import re

import sqlglot
from sqlglot import exp

from ..model import Facts
from .base import FileCtx, call_args, ln, string_value, txt, walk

NAME = "mssql_db"
METHODS = {"query", "execute", "batch"}
EXEC_RE = re.compile(r"^\s*EXEC(?:UTE)?\s+\[?([\w.\]\[]+)", re.I)


def _clean(s: str) -> str:
    return s.replace("[", "").replace("]", "")


def _stmt_rows(sql: str) -> list[tuple[str, str, str]] | None:
    """-> [(operation, schema, table)] or None if unparseable. Procedures use operation 'exec'."""
    rows: list[tuple[str, str, str]] = []
    try:
        stmts = sqlglot.parse(sql, read="tsql")
    except Exception:
        return None
    for st in stmts:
        if st is None:
            continue
        if isinstance(st, exp.Command):
            m = EXEC_RE.match(sql)
            if m and str(st.this).upper().startswith("EXEC"):
                rows.append(("exec", "", _clean(m.group(1))))
                continue
            return None
        ctes = {c.alias for c in st.find_all(exp.CTE)}
        target = None
        op = "select"
        if isinstance(st, exp.Insert):
            op, target = "insert", st.this
        elif isinstance(st, exp.Update):
            op, target = "update", st.this
        elif isinstance(st, exp.Delete):
            op, target = "delete", st.this
        elif isinstance(st, exp.Merge):
            op, target = "merge", st.this
        tgt_tables = [target] if isinstance(target, exp.Table) else list(target.find_all(exp.Table)) if target is not None else []
        tgt_ids = {id(t) for t in tgt_tables}
        for t in tgt_tables:
            rows.append((op, t.db or "", t.name))
        for t in st.find_all(exp.Table):
            if id(t) in tgt_ids or t.name in ctes or not t.name:
                continue
            rows.append(("select", t.db or "", t.name))
    return sorted(set(rows))


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    for path in sorted(ctxs):
        ctx = ctxs[path]
        if not ctx.has_import("mssql"):
            continue
        for n in walk(ctx.root):
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                continue
            method = txt(fn.child_by_field_name("property"))
            if method not in METHODS:
                continue
            args = call_args(n)
            if not args:
                continue
            a0, line = args[0], ln(n)
            enc = ctx.enclosing(line)
            expr = txt(a0)
            common = dict(file_path=path, line=line, library="mssql", expression=expr,
                          enclosing_symbol=enc, line_hash=ctx.lh(line), extractor=NAME)
            val = string_value(a0)
            if method == "execute":
                if val is not None:
                    schema, _, proc = _clean(val).rpartition(".")
                    facts.add("db_access", operation="exec", schema_name=schema, table_name="", procedure=proc,
                              status="verified", origin="source", **common)
                else:
                    facts.add("db_access", operation="exec", schema_name="", table_name="", procedure="",
                              status="unverified", origin="source", **common)
                continue
            if val is None:
                if a0.type in ("identifier", "member_expression", "call_expression", "template_string",
                               "binary_expression", "string"):
                    facts.add("db_access", operation="unknown", schema_name="", table_name="", procedure="",
                              status="unverified", origin="source", **common)
                continue
            rows = _stmt_rows(val)
            if not rows:
                facts.add("db_access", operation="unknown", schema_name="", table_name="", procedure="",
                          status="unverified", origin="source", **common)
                continue
            for op, schema, name in rows:
                if op == "exec":
                    facts.add("db_access", operation="exec", schema_name=schema, table_name="", procedure=name,
                              status="verified", origin="source", **common)
                else:
                    facts.add("db_access", operation=op, schema_name=schema, table_name=name, procedure="",
                              status="verified", origin="source", **common)
