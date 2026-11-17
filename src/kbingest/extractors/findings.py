"""Rule-based findings. Facts to look at, not judgments."""
from __future__ import annotations

from ..model import Facts
from .base import FileCtx, call_args, ln, string_value, txt, walk
from .logs import is_log_call

NAME = "findings"
RETRY_LIBS = ("axios-retry", "async-retry", "p-retry", "retry", "promise-retry", "retry-axios")


def _add(facts: Facts, ctx: FileCtx, line: int, rule: str, detail: str):
    facts.add("findings", file_path=ctx.path, line=line, rule_id=rule, detail=detail,
              enclosing_symbol=ctx.enclosing(line), line_hash=ctx.lh(line), origin="derived", extractor=NAME)


def _stmt_is_log(st) -> bool:
    if st.type != "expression_statement" or not st.named_children:
        return False
    e = st.named_children[0]
    if e.type == "await_expression" and e.named_children:
        e = e.named_children[0]
    return is_log_call(e) is not None


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    for c in sorted(facts.rows("outbound_calls"), key=lambda r: (r["file_path"], r["line"])):
        if not c["has_timeout"]:
            ctx = ctxs[c["file_path"]]
            _add(facts, ctx, c["line"], "http-call-no-timeout", f"{c['client']} {c['method']} without timeout")
    for r in sorted(facts.rows("db_access"), key=lambda r: (r["file_path"], r["line"])):
        if r["status"] == "unverified" and r["operation"] == "unknown":
            _add(facts, ctxs[r["file_path"]], r["line"], "dynamic-sql", "SQL is string-built or unparseable; unresolved")
    for path in sorted(ctxs):
        ctx = ctxs[path]
        if ctx.rec.cls == "test":
            continue
        for spec in sorted(ctx.raw_specs):
            if spec in RETRY_LIBS:
                line = next((i for i, l in enumerate(ctx.rec.lines, 1) if spec in l), 1)
                _add(facts, ctx, line, "retry-present", f"imports {spec}")
        for n in walk(ctx.root):
            if n.type == "catch_clause":
                body = n.child_by_field_name("body")
                stmts = body.named_children if body is not None else []
                stmts = [s for s in stmts if s.type != "comment"]
                if not stmts:
                    _add(facts, ctx, ln(n), "empty-catch", "catch block has no statements")
                elif all(_stmt_is_log(s) for s in stmts):
                    _add(facts, ctx, ln(n), "catch-only-logs", "catch block only logs; does not rethrow or return")
            elif n.type == "call_expression":
                fn = n.child_by_field_name("function")
                if fn is not None and txt(fn) == "process.on":
                    a = call_args(n)
                    if a and string_value(a[0]) == "unhandledRejection":
                        _add(facts, ctx, ln(n), "unhandled-rejection-handler", "process.on('unhandledRejection') registered")
