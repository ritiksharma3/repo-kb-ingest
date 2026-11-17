"""Stage 2: run extractors, build extraction reports (gaps, unresolved items)."""
from __future__ import annotations

from ..extractors import (axios_calls, conventions, express_routes, findings, logs, mssql_db, sqs,
                          symbols_imports, tests_map)
from ..extractors.base import CODE_EXTS, FileCtx, resolve_module
from ..model import Facts, Inventory

# framework -> (extractor module name, fact table it must populate)
FRAMEWORK_EXTRACTORS = {
    "express": ("express_routes", "routes"),
    "axios": ("axios_calls", "outbound_calls"),
    "mssql": ("mssql_db", "db_access"),
    "aws-sqs": ("sqs", "queues"),
}


class ParseCache:
    """In-process cache of parsed files (tree-sitter tree + import/symbol scan), keyed by path and content hash.
    An entry is only reused when the file is unchanged versus the previous snapshot AND every relative import
    still resolves to the same target (the one cross-file input of a parse). Stats feed the incremental report."""

    def __init__(self):
        self.entries: dict[str, tuple[str, dict, FileCtx]] = {}
        self.stats = {"reused": 0, "parsed": 0}

    def reset_stats(self):
        self.stats = {"reused": 0, "parsed": 0}

    @staticmethod
    def _resolutions(ctx: FileCtx, known: set[str]) -> dict:
        return {s: resolve_module(ctx.path, s, known) for s in sorted(ctx.raw_specs) if s.startswith(".")}

    def get(self, rec, known: set[str]):
        hit = self.entries.get(rec.path)
        if hit and hit[0] == rec.content_hash and hit[1] == self._resolutions(hit[2], known):
            ctx = hit[2]
            ctx.rec = rec
            self.stats["reused"] += 1
            return ctx
        return None

    def put(self, rec, ctx: FileCtx, known: set[str]):
        self.entries[rec.path] = (rec.content_hash, self._resolutions(ctx, known), ctx)
        self.stats["parsed"] += 1


def run(inv: Inventory, cache: ParseCache | None = None, reusable: dict[str, str] | None = None) -> tuple[Facts, dict[str, FileCtx]]:
    facts = Facts()
    known = {p for p, f in inv.files.items() if f.language == "javascript" and f.state == "summarized"
             and f.cls in ("source", "test")}
    ctxs: dict[str, FileCtx] = {}
    for path in sorted(inv.files):
        rec = inv.files[path]
        if rec.cls not in ("source", "test") or rec.state != "summarized":
            continue
        if rec.language == "typescript":
            rec.state, rec.reason = "failed", "no parser grammar for typescript"
            facts.add("extraction_reports", kind="no_grammar", subject="typescript", detail=rec.reason,
                      file_path=path, line=0)
            continue
        ctx = cache.get(rec, known) if (cache is not None and reusable is not None and reusable.get(path) == rec.content_hash) else None
        if ctx is None:
            ctx = symbols_imports.build_ctx(rec, known)
            if ctx is not None and cache is not None:
                cache.put(rec, ctx, known)
        if ctx is None:
            rec.state, rec.reason = "failed", "tree-sitter reported a syntax error"
            facts.add("extraction_reports", kind="parse_failure", subject=path, detail=rec.reason,
                      file_path=path, line=0)
            continue
        ctxs[path] = ctx

    symbols_imports.extract(ctxs, facts)
    express_routes.extract(ctxs, facts)
    axios_calls.extract(ctxs, facts)
    mssql_db.extract(ctxs, facts)
    sqs.extract(ctxs, facts)
    logs.extract(ctxs, facts)
    tests_map.extract(ctxs, facts)
    findings.extract(ctxs, facts)
    conventions.extract(ctxs, facts)

    for fw in sorted(inv.frameworks):
        ev = "; ".join(inv.frameworks[fw]["evidence"][:3])
        if fw not in FRAMEWORK_EXTRACTORS:
            facts.add("extraction_reports", kind="framework_no_extractor", subject=fw,
                      detail=f"detected ({ev}) but no extractor exists", file_path="", line=0)
        else:
            ext, table = FRAMEWORK_EXTRACTORS[fw]
            if not facts.rows(table):
                facts.add("extraction_reports", kind="extractor_zero_results", subject=fw,
                          detail=f"extractor {ext} returned no rows in {table} though {fw} is used ({ev})",
                          file_path="", line=0)
    for r in facts.rows("routes"):
        if r["status"] == "unverified":
            facts.add("extraction_reports", kind="unresolved_route", subject=f"{r['method']} {r['path']}",
                      detail=r["expression"], file_path=r["route_file"], line=r["route_line"])
    for r in facts.rows("outbound_calls"):
        if r["status"] == "unverified":
            facts.add("extraction_reports", kind="unresolved_url", subject=r["client"],
                      detail=r["expression"], file_path=r["file_path"], line=r["line"])
    for r in facts.rows("db_access"):
        if r["status"] == "unverified":
            facts.add("extraction_reports", kind="unresolved_sql", subject=r["library"],
                      detail=r["expression"], file_path=r["file_path"], line=r["line"])
    for r in facts.rows("queues"):
        if r["status"] == "unverified":
            facts.add("extraction_reports", kind="unresolved_queue", subject=r["library"],
                      detail=r["expression"], file_path=r["file_path"], line=r["line"])
    # dedupe reports on PK
    seen, out = set(), []
    for r in facts.tables["extraction_reports"]:
        k = (r["kind"], r["subject"], r["file_path"], r["line"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    facts.tables["extraction_reports"] = out
    return facts, ctxs
