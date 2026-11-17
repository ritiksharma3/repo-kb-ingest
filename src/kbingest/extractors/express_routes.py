"""Express routes: app/router verbs, router.route() chains, app.use() mounts (cross-file prefix resolution)."""
from __future__ import annotations
import posixpath

from ..model import Facts
from .base import FileCtx, call_args, ln, resolve_module, string_value, template_pattern, txt, walk

NAME = "express_routes"
VERBS = {"get", "post", "put", "patch", "delete", "all", "options", "head"}


def _router_vars(ctx: FileCtx) -> dict[str, str]:
    """var name -> 'app' | 'router' for variables created by express() / express.Router() / Router()."""
    out = {}
    for n in walk(ctx.root):
        if n.type != "variable_declarator":
            continue
        val, nm = n.child_by_field_name("value"), n.child_by_field_name("name")
        if val is None or nm is None or val.type != "call_expression" or nm.type != "identifier":
            continue
        f = txt(val.child_by_field_name("function"))
        if f in ("express", "express.Router", "Router"):
            out[txt(nm)] = "router" if "Router" in f else "app"
        elif f in ctx.imports and ctx.imports[f][0] == "express" and ctx.imports[f][1] in ("Router", "default", "*"):
            out[txt(nm)] = "router" if ctx.imports[f][1] == "Router" else "app"
    return out


def _handler(ctx: FileCtx, ctxs: dict[str, FileCtx], arg):
    """-> (handler_file, handler_symbol, handler_line, label)"""
    if arg.type in ("arrow_function", "function_expression", "function"):
        return ctx.path, "<inline>", ln(arg), "<inline>"
    if arg.type == "identifier":
        name = txt(arg)
        for s in ctx.symbols:
            if s.name == name:
                return ctx.path, name, s.start, name
        imp = ctx.imports.get(name)
        if imp and imp[2] in ctxs:
            t = ctxs[imp[2]]
            local = t.export_map.get(imp[1], imp[1]) if imp[1] not in ("*", "default") else (t.default_export or name)
            for s in t.symbols:
                if s.name == local:
                    return t.path, local, s.start, name
        return None, name, None, name
    if arg.type == "member_expression":
        obj, prop = txt(arg.child_by_field_name("object")), txt(arg.child_by_field_name("property"))
        imp = ctx.imports.get(obj)
        if imp and imp[2] in ctxs:
            t = ctxs[imp[2]]
            for cand in (t.export_map.get(prop, prop), f"{t.default_export}.{prop}" if t.default_export else ""):
                for s in t.symbols:
                    if s.name == cand:
                        return t.path, cand, s.start, txt(arg)
        return None, txt(arg), None, txt(arg)
    return None, txt(arg), None, txt(arg)


def _join(*parts: str) -> str:
    p = "/".join(x.strip("/") for x in parts if x and x != "/")
    return "/" + p if p else "/"


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    raw = []        # (ctx, var, kind, method, path_expr, path_val, args, line)
    mounts = []     # (ctx, parent_var, prefix, child_key, line)
    rvars: dict[str, dict[str, str]] = {}
    for path in sorted(ctxs):
        ctx = ctxs[path]
        if not (ctx.has_import("express") or any(v[0] == "express" for v in ctx.imports.values())):
            continue
        rv = _router_vars(ctx)
        rvars[path] = rv
        for n in walk(ctx.root):
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            if fn is None or fn.type != "member_expression":
                continue
            prop = txt(fn.child_by_field_name("property"))
            obj = fn.child_by_field_name("object")
            args = call_args(n)
            if prop in VERBS and obj.type == "identifier" and txt(obj) in rv and args:
                a0 = args[0]
                pv = string_value(a0)
                raw.append((ctx, txt(obj), prop, a0, pv, args[1:], ln(n)))
            elif prop in VERBS and obj.type == "call_expression":
                # router.route('/x').get(h).post(h2)
                base = obj
                while base.type == "call_expression" and base.child_by_field_name("function") is not None \
                        and base.child_by_field_name("function").type == "member_expression" \
                        and txt(base.child_by_field_name("function").child_by_field_name("property")) in VERBS:
                    base = base.child_by_field_name("function").child_by_field_name("object")
                bf = base.child_by_field_name("function") if base.type == "call_expression" else None
                if bf is not None and bf.type == "member_expression" and txt(bf.child_by_field_name("property")) == "route":
                    bobj = bf.child_by_field_name("object")
                    bargs = call_args(base)
                    if bobj.type == "identifier" and txt(bobj) in rv and bargs:
                        raw.append((ctx, txt(bobj), prop, bargs[0], string_value(bargs[0]), args, ln(n)))
            elif prop == "use" and obj.type == "identifier" and txt(obj) in rv and args:
                prefix, child = ("", args[0])
                if len(args) >= 2:
                    prefix_v = string_value(args[0])
                    prefix, child = (prefix_v if prefix_v is not None else None), args[-1]
                mounts.append((ctx, txt(obj), prefix, child, ln(n)))

    # router key = (file, var); resolve each mount's child to a router key
    def child_key(ctx: FileCtx, child):
        if child.type == "identifier":
            nm = txt(child)
            if nm in rvars.get(ctx.path, {}):
                return (ctx.path, nm)
            imp = ctx.imports.get(nm)
            if imp and imp[2] in ctxs:
                t = ctxs[imp[2]]
                if t.default_export in rvars.get(t.path, {}):
                    return (t.path, t.default_export)
        if child.type == "call_expression" and txt(child.child_by_field_name("function")) == "require":
            a = call_args(child)
            spec = string_value(a[0]) if a else None
            if spec:
                res = resolve_module(ctx.path, spec, set(ctxs))
                if res and ctxs[res].default_export in rvars.get(res, {}):
                    return (res, ctxs[res].default_export)
        return None

    parent_of: dict[tuple, tuple] = {}   # child router key -> (parent key, prefix|None)
    for ctx, pvar, prefix, child, line in mounts:
        ck = child_key(ctx, child)
        if ck:
            parent_of[ck] = ((ctx.path, pvar), prefix)

    def full_prefix(key):
        parts, seen = [], set()
        while key in parent_of and key not in seen:
            seen.add(key)
            pk, pre = parent_of[key]
            if pre is None:
                return None, False
            parts.append(pre)
            key = pk
        kind = rvars.get(key[0], {}).get(key[1])
        root_is_app = kind == "app"
        return "/".join(reversed([p.strip("/") for p in parts if p.strip("/")])), root_is_app

    for ctx, var, verb, a0, pv, rest, line in raw:
        key = (ctx.path, var)
        prefix, root_is_app = full_prefix(key)
        method = verb.upper()
        expr = txt(a0)
        status, path_val = "verified", None
        if pv is None:
            status, path_val = "unverified", template_pattern(a0) or expr
        else:
            path_val = pv
        if prefix is None:
            status = "unverified"
            full = path_val
        else:
            full = _join(prefix, path_val)
        if status == "verified" and not root_is_app:
            status = "unverified"   # router never mounted on an app we can see
            expr = f"{expr} (router '{var}' not mounted on an app in this repo)"
        handlers = [a for a in rest]
        hfile = hsym = hline = None
        mw = []
        if handlers:
            *mws, last = handlers
            hfile, hsym, hline, _ = _handler(ctx, ctxs, last)
            mw = [txt(m) if m.type != "arrow_function" else "<inline>" for m in mws]
        facts.add("routes", method=method, path=full, route_file=ctx.path, route_line=line,
                  handler_file=hfile, handler_symbol=hsym, handler_line=hline, middleware=mw,
                  framework="express", status=status, expression=expr, line_hash=ctx.lh(line),
                  origin="source", extractor=NAME)
