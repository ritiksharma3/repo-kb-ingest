"""Convention detectors: per pattern -> majority, counts, representative file:line, outliers."""
from __future__ import annotations
import posixpath
import re

from ..model import Facts
from .base import FileCtx, call_args, ln, txt, walk
from .logs import is_log_call

NAME = "conventions"
VALIDATION = {"joi", "@hapi/joi", "yup", "zod", "express-validator", "ajv", "celebrate", "class-validator"}
ORMS = {"sequelize", "typeorm", "@prisma/client", "knex", "mongoose"}


def _name_style(base: str) -> str:
    stem = base.split(".")[0]
    if "-" in stem:
        return "kebab-case"
    if "_" in stem:
        return "snake_case"
    if stem[:1].isupper():
        return "PascalCase"
    if re.search(r"[a-z][A-Z]", stem):
        return "camelCase"
    return "lowercase"


def _file_signals(ctx: FileCtx) -> dict[str, tuple[str, int]]:
    """pattern -> (label, representative line) for one file."""
    sig: dict[str, tuple[str, int]] = {}
    tries, catches, logs, resp = [], [], {}, {}
    for n in walk(ctx.root):
        if n.type == "try_statement":
            tries.append(ln(n))
        elif n.type == "call_expression":
            fn = n.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                prop = txt(fn.child_by_field_name("property"))
                if prop == "catch":
                    catches.append(ln(n))
                if prop in ("json", "send") and "res" in txt(fn.child_by_field_name("object")):
                    resp.setdefault("res." + prop, ln(n))
            hit = is_log_call(n)
            if hit:
                logs.setdefault("console" if hit[0] == "console" else "logger", ln(n))
    if tries or catches:
        label = "+".join(x for x, v in (("try/catch", tries), (".catch()", catches)) if v)
        sig["error_handling"] = (label, min(tries + catches))
    elif ctx.symbols:
        sig["error_handling"] = ("none", ctx.symbols[0].start)
    if logs:
        sig["logger"] = ("+".join(sorted(logs)), min(logs.values()))
    if resp:
        sig["response_shape"] = ("+".join(sorted(resp)), min(resp.values()))
    val = sorted(s for s in ctx.raw_specs if s in VALIDATION)
    if val:
        sig["validation_library"] = (val[0], 1)
    da = []
    if ctx.has_import("mssql"):
        da.append("raw-sql(mssql)")
    da += sorted(f"orm({s})" for s in ctx.raw_specs if s in ORMS)
    if da:
        sig["data_access"] = ("+".join(da), 1)
    return sig


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    per: dict[str, list[tuple[str, str, int]]] = {}  # pattern -> [(file, label, line)]
    for path in sorted(ctxs):
        ctx = ctxs[path]
        if ctx.rec.cls != "source":
            continue
        for pat, (label, line) in _file_signals(ctx).items():
            per.setdefault(pat, []).append((path, label, line))
        per.setdefault("file_naming", []).append((path, _name_style(posixpath.basename(path)), 1))
    # data access: stored procedures vs raw statements from extractor facts
    sp_files = {r["file_path"] for r in facts.rows("db_access") if r["operation"] == "exec"}
    for i, (f, label, line) in enumerate(per.get("data_access", [])):
        if f in sp_files:
            per["data_access"][i] = (f, label + "+stored-procedures", line)
    # folder layout
    layers = {"controllers", "services", "repositories", "routes", "models", "middleware", "utils", "handlers", "config"}
    dirs: dict[str, int] = {}
    for path in ctxs:
        if ctxs[path].rec.cls != "source":
            continue
        for part in path.split("/")[:-1]:
            if part in layers:
                dirs[part] = dirs.get(part, 0) + 1
    if dirs:
        facts.add("convention_stats", pattern="folder_layout", majority="layered: " + ", ".join(sorted(dirs)),
                  counts=dict(sorted(dirs.items())),
                  representative_file=next((p for p in sorted(ctxs) if ctxs[p].rec.cls == "source"
                                            and any(d in layers for d in p.split("/")[:-1])), None),
                  representative_line=1,
                  outliers=[], origin="derived", extractor=NAME)
    for pat in sorted(per):
        rows = per[pat]
        counts: dict[str, int] = {}
        for _, label, _ in rows:
            counts[label] = counts.get(label, 0) + 1
        majority = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        rep = next((r for r in rows if r[1] == majority), None)
        outliers = [{"file": f, "line": line, "label": label} for f, label, line in rows if label != majority]
        facts.add("convention_stats", pattern=pat, majority=majority, counts=dict(sorted(counts.items())),
                  representative_file=rep[0] if rep else None, representative_line=rep[2] if rep else None,
                  outliers=outliers, origin="derived", extractor=NAME)
