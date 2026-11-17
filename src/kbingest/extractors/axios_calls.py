"""Outbound HTTP calls: axios (incl. axios.create instances) and global fetch."""
from __future__ import annotations

from ..model import Facts
from .base import FileCtx, call_args, ln, prop_value, string_value, txt, walk

NAME = "axios_calls"
VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}
BODY_VERBS = {"post", "put", "patch"}


def _const_strings(ctx: FileCtx) -> dict[str, str]:
    out = {}
    for n in walk(ctx.root):
        if n.type == "variable_declarator":
            nm, val = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nm is not None and nm.type == "identifier" and val is not None:
                v = string_value(val)
                if v is not None:
                    out[txt(nm)] = v
    return out


def _instances(ctx: FileCtx) -> dict[str, dict]:
    """axios.create({...}) instances: name -> {timeout: bool, base: str|None}"""
    out = {}
    axios_names = {k for k, v in ctx.imports.items() if v[0] == "axios"} | {"axios"}
    for n in walk(ctx.root):
        if n.type != "variable_declarator":
            continue
        nm, val = n.child_by_field_name("name"), n.child_by_field_name("value")
        if val is None or val.type != "call_expression" or nm is None:
            continue
        f = val.child_by_field_name("function")
        if f is not None and f.type == "member_expression" and txt(f.child_by_field_name("property")) == "create" \
                and txt(f.child_by_field_name("object")) in axios_names:
            a = call_args(val)
            cfg = a[0] if a else None
            out[txt(nm)] = {"timeout": prop_value(cfg, "timeout") is not None,
                            "base": string_value(prop_value(cfg, "baseURL"))}
    return out


def _global_timeout(ctxs: dict[str, FileCtx]) -> bool:
    for ctx in ctxs.values():
        for n in walk(ctx.root):
            if n.type == "assignment_expression" and txt(n.child_by_field_name("left")) == "axios.defaults.timeout":
                return True
    return False


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    gtimeout = _global_timeout(ctxs)
    for path in sorted(ctxs):
        ctx = ctxs[path]
        uses_axios = ctx.has_import("axios")
        if not uses_axios and "fetch" not in ctx.rec.content:
            continue
        axios_names = {k for k, v in ctx.imports.items() if v[0] == "axios"} | ({"axios"} if uses_axios else set())
        inst = _instances(ctx) if uses_axios else {}
        consts = _const_strings(ctx)
        for n in walk(ctx.root):
            if n.type != "call_expression":
                continue
            fn = n.child_by_field_name("function")
            args = call_args(n)
            client = method = None
            cfg = None
            url_node = None
            has_timeout = False
            if fn.type == "member_expression":
                obj, prop = txt(fn.child_by_field_name("object")), txt(fn.child_by_field_name("property"))
                if prop in VERBS and (obj in axios_names or obj in inst):
                    client = "axios"
                    method = prop.upper()
                    if prop == "request" and args:
                        cfg = args[0]
                        url_node = prop_value(cfg, "url")
                        m = string_value(prop_value(cfg, "method"))
                        method = (m or "REQUEST").upper()
                    else:
                        url_node = args[0] if args else None
                        cfg_idx = 2 if prop in BODY_VERBS else 1
                        cfg = args[cfg_idx] if len(args) > cfg_idx else None
                    has_timeout = (prop_value(cfg, "timeout") is not None) or \
                                  (obj in inst and inst[obj]["timeout"]) or gtimeout
            elif fn.type == "identifier":
                f = txt(fn)
                if f in axios_names and args:        # axios({url, method})
                    client = "axios"
                    cfg = args[0]
                    url_node = prop_value(cfg, "url") if cfg.type == "object" else cfg
                    method = (string_value(prop_value(cfg, "method")) or "GET").upper() if cfg.type == "object" else "GET"
                    has_timeout = prop_value(cfg, "timeout") is not None or gtimeout
                elif f == "fetch" and args:
                    client = "fetch"
                    url_node = args[0]
                    cfg = args[1] if len(args) > 1 else None
                    m = string_value(prop_value(cfg, "method")) if cfg is not None else None
                    method = (m or "GET").upper()
                    has_timeout = cfg is not None and (prop_value(cfg, "signal") is not None
                                                       or prop_value(cfg, "timeout") is not None)
            if client is None:
                continue
            line = ln(n)
            expr = txt(url_node) if url_node is not None else "<no url>"
            url = string_value(url_node) if url_node is not None else None
            status = "verified"
            if url is None and url_node is not None and url_node.type == "identifier" and txt(url_node) in consts:
                url, status = consts[txt(url_node)], "derived"
            if url is None and url_node is not None and fn.type == "member_expression" \
                    and txt(fn.child_by_field_name("object")) in inst and inst[txt(fn.child_by_field_name("object"))]["base"]:
                rel = string_value(url_node)
                if rel is not None:
                    url, status = inst[txt(fn.child_by_field_name("object"))]["base"].rstrip("/") + "/" + rel.lstrip("/"), "derived"
            if url is None:
                status = "unverified"
            facts.add("outbound_calls", file_path=path, line=line, client=client, method=method, url=url,
                      expression=expr, status=status, has_timeout=bool(has_timeout),
                      enclosing_symbol=ctx.enclosing(line), line_hash=ctx.lh(line),
                      origin="source" if status == "verified" else ("derived" if status == "derived" else "source"),
                      extractor=NAME)
