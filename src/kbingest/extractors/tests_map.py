"""Which source files each test file covers: resolved imports + naming convention."""
from __future__ import annotations
import posixpath

from ..model import Facts
from .base import FileCtx

NAME = "tests_map"


def _strip_test(name: str) -> str:
    for suf in (".test", ".spec"):
        if suf in name:
            return name.replace(suf, "")
    return name


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    sources = {p for p, c in ctxs.items() if c.rec.cls == "source"}
    by_base: dict[str, list[str]] = {}
    for s in sources:
        by_base.setdefault(posixpath.basename(s), []).append(s)
    seen = set()
    for tpath in sorted(p for p, c in ctxs.items() if c.rec.cls == "test"):
        ctx = ctxs[tpath]
        for spec, imported, res in sorted(ctx.imports.values(), key=lambda x: str(x)):
            if res in sources and (tpath, res) not in seen:
                seen.add((tpath, res))
                facts.add("test_map", test_file=tpath, source_file=res, how="import", origin="derived", extractor=NAME)
        base = _strip_test(posixpath.basename(tpath))
        for cand in sorted(by_base.get(base, [])):
            if (tpath, cand) not in seen:
                seen.add((tpath, cand))
                facts.add("test_map", test_file=tpath, source_file=cand, how="naming", origin="derived", extractor=NAME)
