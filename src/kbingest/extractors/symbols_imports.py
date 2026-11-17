"""Symbols, imports/exports, intra-repo caller/callee links (tree-sitter, JavaScript)."""
from __future__ import annotations

from ..model import Facts
from .base import (CODE_EXTS, FileCtx, Sym, call_args, ln, parse, resolve_module, string_value, txt, walk)

NAME = "symbols_imports"
FUNC_TYPES = {"arrow_function", "function_expression", "function", "generator_function"}


def build_ctx(rec, known: set[str]) -> FileCtx | None:
    """Parse one file. Returns None when it can't be parsed (caller marks it failed)."""
    tree = parse(rec.content)
    if tree.root_node.has_error:
        return None
    ctx = FileCtx(rec=rec, tree=tree, root=tree.root_node)
    ctx.export_map = {}
    ctx.import_edges = []
    _scan_imports(ctx, known)
    _scan_symbols(ctx)
    _scan_exports(ctx)
    for s in ctx.symbols:
        s.exported = (s.name in ctx.export_map.values()) or (s.name == ctx.default_export)
    return ctx


def _bind(ctx: FileCtx, pattern, spec: str, resolved, member: str | None = None):
    if resolved:
        ctx.import_edges.append((ln(pattern), resolved))
    if pattern.type == "identifier":
        ctx.imports[txt(pattern)] = (spec, member or "*", resolved)
    elif pattern.type == "object_pattern":
        for c in pattern.named_children:
            if c.type == "shorthand_property_identifier_pattern":
                ctx.imports[txt(c)] = (spec, txt(c), resolved)
            elif c.type == "pair_pattern":
                k, v = c.child_by_field_name("key"), c.child_by_field_name("value")
                if v is not None and v.type == "identifier":
                    ctx.imports[txt(v)] = (spec, txt(k), resolved)


def _scan_imports(ctx: FileCtx, known: set[str]) -> None:
    ctx.raw_specs = set()
    for n in walk(ctx.root):
        if n.type == "call_expression":
            f = n.child_by_field_name("function")
            if f is not None and txt(f) == "require":
                a = call_args(n)
                spec = string_value(a[0]) if a else None
                if spec:
                    ctx.raw_specs.add(spec)
        elif n.type == "variable_declarator":
            val = n.child_by_field_name("value")
            name = n.child_by_field_name("name")
            member = None
            if val is not None and val.type == "member_expression":
                member = txt(val.child_by_field_name("property"))
                val = val.child_by_field_name("object")
            if val is not None and val.type == "call_expression" and txt(val.child_by_field_name("function")) == "require":
                a = call_args(val)
                spec = string_value(a[0]) if a else None
                if spec and name is not None:
                    _bind(ctx, name, spec, resolve_module(ctx.path, spec, known), member)
        elif n.type == "import_statement":
            src = n.child_by_field_name("source")
            spec = string_value(src)
            if not spec:
                continue
            ctx.raw_specs.add(spec)
            res = resolve_module(ctx.path, spec, known)
            if res:
                ctx.import_edges.append((ln(n), res))
            for c in n.named_children:
                if c.type == "import_clause":
                    for cc in c.named_children:
                        if cc.type == "identifier":
                            ctx.imports[txt(cc)] = (spec, "default", res)
                        elif cc.type == "namespace_import":
                            ctx.imports[txt(cc.named_children[0])] = (spec, "*", res)
                        elif cc.type == "named_imports":
                            for sp in cc.named_children:
                                nm = sp.child_by_field_name("name")
                                al = sp.child_by_field_name("alias")
                                ctx.imports[txt(al or nm)] = (spec, txt(nm), res)


def _add(ctx: FileCtx, name: str, kind: str, node, parent: str | None = None):
    ctx.symbols.append(Sym(name=f"{parent}.{name}" if parent else name, kind=kind,
                           start=ln(node), end=node.end_point[0] + 1, parent=parent, node=node))


def _scan_symbols(ctx: FileCtx) -> None:
    for n in walk(ctx.root):
        t = n.type
        if t in ("function_declaration", "generator_function_declaration"):
            nm = n.child_by_field_name("name")
            if nm is not None:
                _add(ctx, txt(nm), "function", n)
        elif t == "class_declaration":
            nm = n.child_by_field_name("name")
            if nm is None:
                continue
            cname = txt(nm)
            _add(ctx, cname, "class", n)
            body = n.child_by_field_name("body")
            for m in (body.named_children if body is not None else []):
                if m.type == "method_definition":
                    mn = m.child_by_field_name("name")
                    if mn is not None:
                        _add(ctx, txt(mn), "method", m, parent=cname)
        elif t == "variable_declarator":
            nm, val = n.child_by_field_name("name"), n.child_by_field_name("value")
            if nm is not None and nm.type == "identifier" and val is not None and val.type in FUNC_TYPES:
                _add(ctx, txt(nm), "function", n)
        elif t == "assignment_expression":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is not None and right is not None and left.type == "member_expression" and right.type in FUNC_TYPES:
                obj = txt(left.child_by_field_name("object"))
                if obj in ("exports", "module.exports"):
                    _add(ctx, txt(left.child_by_field_name("property")), "function", n)
    ctx.symbols.sort(key=lambda s: (s.start, s.name))


def _scan_exports(ctx: FileCtx) -> None:
    em = ctx.export_map
    for n in walk(ctx.root):
        if n.type == "assignment_expression":
            left, right = n.child_by_field_name("left"), n.child_by_field_name("right")
            if left is None or right is None:
                continue
            lt = txt(left)
            if lt == "module.exports":
                if right.type == "identifier":
                    ctx.default_export = txt(right)
                elif right.type == "object":
                    for p in right.named_children:
                        if p.type == "shorthand_property_identifier":
                            em[txt(p)] = txt(p)
                        elif p.type == "pair":
                            v = p.child_by_field_name("value")
                            if v is not None and v.type == "identifier":
                                em[txt(p.child_by_field_name("key"))] = txt(v)
                elif right.type == "class":
                    pass
            elif left.type == "member_expression" and txt(left.child_by_field_name("object")) in ("exports", "module.exports"):
                key = txt(left.child_by_field_name("property"))
                em[key] = txt(right) if right.type == "identifier" else key
        elif n.type == "export_statement":
            decl = n.child_by_field_name("declaration")
            if decl is not None:
                nm = decl.child_by_field_name("name")
                if nm is not None:
                    em[txt(nm)] = txt(nm)
                elif decl.type in ("lexical_declaration", "variable_declaration"):
                    for d in decl.named_children:
                        if d.type == "variable_declarator":
                            em[txt(d.child_by_field_name("name"))] = txt(d.child_by_field_name("name"))
            val = n.child_by_field_name("value")
            if val is not None and val.type == "identifier":
                ctx.default_export = txt(val)
            for c in n.named_children:
                if c.type == "export_clause":
                    for sp in c.named_children:
                        nm = sp.child_by_field_name("name")
                        al = sp.child_by_field_name("alias")
                        em[txt(al or nm)] = txt(nm)


def _find_symbol(ctx: FileCtx, name: str) -> Sym | None:
    for s in ctx.symbols:
        if s.name == name:
            return s
    return None


def _target_symbol(target: FileCtx, imported: str, member: str | None) -> str | None:
    """Name of the symbol in `target` a call resolves to, or None."""
    if imported in ("*", "default"):
        base = target.default_export
        if member is None:
            return base
        if base and _find_symbol(target, f"{base}.{member}"):
            return f"{base}.{member}"
        local = target.export_map.get(member, member)
        return local if _find_symbol(target, local) else None
    local = target.export_map.get(imported, imported)
    if member is not None and _find_symbol(target, f"{local}.{member}"):
        return f"{local}.{member}"
    return local if _find_symbol(target, local) else None


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    for path in sorted(ctxs):
        ctx = ctxs[path]
        for s in ctx.symbols:
            facts.add("symbols", file_path=path, name=s.name, kind=s.kind, line_start=s.start, line_end=s.end,
                      exported=s.exported, parent=s.parent, line_hash=ctx.lh(s.start),
                      origin="source", extractor=NAME)
        for line, res in sorted(set(ctx.import_edges)):
            facts.add("symbol_refs", src_file=path, src_symbol="<module>", src_line=line, dst_file=res,
                      dst_symbol="*", kind="import", status="resolved", line_hash=ctx.lh(line),
                      origin="source", extractor=NAME)
        seen = set()
        for n in walk(ctx.root):
            if n.type not in ("call_expression", "new_expression"):
                continue
            fn = n.child_by_field_name("function") if n.type == "call_expression" else n.child_by_field_name("constructor")
            if fn is None:
                continue
            line = ln(n)
            src = ctx.enclosing(line) or "<module>"
            kind = "new" if n.type == "new_expression" else "call"
            dst_file = dst_sym = None
            if fn.type == "identifier":
                f = txt(fn)
                local = _find_symbol(ctx, f)
                if local is not None and local.parent is None:
                    dst_file, dst_sym = path, f
                elif f in ctx.imports and ctx.imports[f][2] in ctxs:
                    spec, imported, res = ctx.imports[f]
                    tgt = ctxs[res]
                    dst_sym = _target_symbol(tgt, imported, None) or (f if imported in ("*", "default") else imported)
                    dst_file = res
            elif fn.type == "member_expression":
                obj, prop = fn.child_by_field_name("object"), fn.child_by_field_name("property")
                if obj is None or prop is None:
                    continue
                member = txt(prop)
                if obj.type == "this":
                    enc = _find_symbol(ctx, src) if src != "<module>" else None
                    cls = enc.parent if enc is not None else None
                    if cls and _find_symbol(ctx, f"{cls}.{member}"):
                        dst_file, dst_sym = path, f"{cls}.{member}"
                elif obj.type == "identifier":
                    o = txt(obj)
                    if o in ctx.imports and ctx.imports[o][2] in ctxs:
                        spec, imported, res = ctx.imports[o]
                        dst_sym = _target_symbol(ctxs[res], imported, member) or member
                        dst_file = res
                    elif _find_symbol(ctx, f"{o}.{member}"):
                        dst_file, dst_sym = path, f"{o}.{member}"
            if dst_file is None:
                continue
            tgt_ctx = ctxs.get(dst_file)
            status = "resolved" if tgt_ctx is not None and _find_symbol(tgt_ctx, dst_sym) else "unverified"
            key = (src, line, dst_sym, kind)
            if key in seen:
                continue
            seen.add(key)
            facts.add("symbol_refs", src_file=path, src_symbol=src, src_line=line, dst_file=dst_file,
                      dst_symbol=dst_sym, kind=kind, status=status, line_hash=ctx.lh(line),
                      origin="source", extractor=NAME)
