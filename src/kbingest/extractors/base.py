"""Shared tree-sitter helpers and the per-file parse context."""
from __future__ import annotations
import posixpath
from dataclasses import dataclass, field

import tree_sitter_javascript as tsjs
from tree_sitter import Language, Parser

from ..hashing import line_hash
from ..model import FileRec

_LANG = Language(tsjs.language())
_PARSER = Parser(_LANG)
CODE_EXTS = (".js", ".mjs", ".cjs", ".jsx")
LOG_LEVELS = {"trace", "debug", "info", "warn", "warning", "error", "fatal", "log"}


def parse(src: str):
    return _PARSER.parse(src.encode("utf-8"))


def walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def txt(n) -> str:
    return n.text.decode("utf-8") if n is not None else ""


def ln(n) -> int:
    return n.start_point[0] + 1


def string_value(n) -> str | None:
    """Value of a plain string / substitution-free template literal, else None."""
    if n is None:
        return None
    if n.type == "string":
        return "".join(txt(c) for c in n.children if c.type in ("string_fragment", "escape_sequence"))
    if n.type == "template_string":
        if any(c.type == "template_substitution" for c in n.children):
            return None
        return "".join(txt(c) for c in n.children if c.type in ("string_fragment", "escape_sequence"))
    return None


def template_pattern(n) -> str | None:
    """Template with ${} replaced by {}; concatenations flattened. None if no literal text."""
    if n.type in ("string", "template_string"):
        out = []
        for c in n.children:
            if c.type in ("string_fragment", "escape_sequence"):
                out.append(txt(c))
            elif c.type == "template_substitution":
                out.append("{}")
        return "".join(out)
    if n.type == "binary_expression" and n.child_by_field_name("operator") is not None \
            and txt(n.child_by_field_name("operator")) == "+":
        parts = [template_pattern(n.child_by_field_name("left")), template_pattern(n.child_by_field_name("right"))]
        parts = [p if p is not None else "{}" for p in parts]
        return "".join(parts)
    if n.type == "parenthesized_expression" and n.named_child_count:
        return template_pattern(n.named_children[0])
    return None


def call_args(call) -> list:
    a = call.child_by_field_name("arguments")
    if a is None:
        return []
    if a.type == "template_string":
        return [a]
    return list(a.named_children)


def prop_value(obj, key: str):
    if obj is None or obj.type != "object":
        return None
    for p in obj.named_children:
        if p.type == "pair":
            k = p.child_by_field_name("key")
            if k is not None and (txt(k).strip("'\"`") == key):
                return p.child_by_field_name("value")
        elif p.type == "shorthand_property_identifier" and txt(p) == key:
            return p
    return None


@dataclass
class Sym:
    name: str
    kind: str
    start: int
    end: int
    parent: str | None
    node: object = None
    exported: bool = False


@dataclass
class FileCtx:
    rec: FileRec
    tree: object
    root: object
    imports: dict = field(default_factory=dict)   # local name -> (spec, imported name|None|'*', resolved path|None)
    symbols: list[Sym] = field(default_factory=list)
    exports: set[str] = field(default_factory=set)
    default_export: str | None = None

    @property
    def path(self):
        return self.rec.path

    def lh(self, line: int) -> str:
        ls = self.rec.lines
        return line_hash(ls[line - 1]) if 0 < line <= len(ls) else line_hash("")

    def enclosing(self, line: int) -> str | None:
        best = None
        for s in self.symbols:
            if s.start <= line <= s.end and (best is None or (s.end - s.start) <= (best.end - best.start)):
                best = s
        return best.name if best is not None else None

    def has_import(self, *mods: str) -> bool:
        return any(sp == m or sp.startswith(m + "/") for sp in self.raw_specs for m in mods)

    raw_specs: set = field(default_factory=set)


def resolve_module(from_path: str, spec: str, known: set[str]) -> str | None:
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(from_path), spec))
    for cand in (base, base + ".js", base + ".mjs", base + ".cjs", base + ".jsx",
                 base + "/index.js", base + "/index.mjs"):
        if cand in known:
            return cand
    return None
