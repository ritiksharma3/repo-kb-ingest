"""Log and error strings: logger.<level>(...), console.<level>(...), throw new XError('...')."""
from __future__ import annotations

from ..model import Facts
from .base import LOG_LEVELS, FileCtx, call_args, ln, template_pattern, txt, walk

NAME = "logs"
LOGGER_NAMES = ("logger", "log", "console", "winston", "pino", "bunyan")


def is_log_call(n) -> tuple[str, str] | None:
    """-> (object name, level) if this call node looks like a log call."""
    if n.type != "call_expression":
        return None
    fn = n.child_by_field_name("function")
    if fn is None or fn.type != "member_expression":
        return None
    obj = fn.child_by_field_name("object")
    prop = txt(fn.child_by_field_name("property"))
    if prop not in LOG_LEVELS:
        return None
    o = txt(obj).split(".")[-1]
    if o.lower() in LOGGER_NAMES or o.lower().endswith(("logger", "log")):
        return o, prop
    return None


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    for path in sorted(ctxs):
        ctx = ctxs[path]
        if ctx.rec.cls == "test":
            continue
        for n in walk(ctx.root):
            hit = is_log_call(n)
            if hit:
                args = call_args(n)
                tpl = None
                for a in args:
                    tpl = template_pattern(a)
                    if tpl is not None:
                        break
                if tpl is None or not tpl.strip("{} "):
                    continue
                line = ln(n)
                facts.add("log_sites", file_path=path, line=line, kind="log", level=hit[1], template=tpl,
                          enclosing_symbol=ctx.enclosing(line), line_hash=ctx.lh(line),
                          origin="source", extractor=NAME)
            elif n.type == "throw_statement":
                e = n.named_children[0] if n.named_children else None
                if e is not None and e.type == "new_expression":
                    args = call_args(e)
                    tpl = template_pattern(args[0]) if args else None
                    if tpl is None or not tpl.strip("{} "):
                        continue
                    line = ln(n)
                    facts.add("log_sites", file_path=path, line=line, kind="error", level=None, template=tpl,
                              enclosing_symbol=ctx.enclosing(line), line_hash=ctx.lh(line),
                              origin="source", extractor=NAME)
