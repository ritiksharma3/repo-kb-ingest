"""Stage 4: file cards and module summaries, assembled by rules. No generated prose."""
from __future__ import annotations
import posixpath
import re

from ..extractors.base import FileCtx, txt, walk
from ..versions import EXTRACTOR_VERSIONS, PIPELINE_VERSION

ROLE_DIRS = [("controllers", "controller"), ("controller", "controller"), ("handlers", "controller"),
             ("services", "service"), ("service", "service"),
             ("repositories", "repository"), ("repository", "repository"), ("dal", "repository"), ("models", "repository"),
             ("routes", "router"), ("middleware", "middleware"), ("utils", "util"), ("helpers", "util"),
             ("lib", "util"), ("config", "config")]
WRITE_OPS = {"insert", "update", "delete", "merge", "exec"}
FS_WRITES = {"writeFile", "writeFileSync", "appendFile", "appendFileSync", "unlink", "unlinkSync", "rm", "rmSync"}


def header_comment(ctx: FileCtx) -> dict | None:
    for c in ctx.root.children:
        if c.type == "comment":
            t = txt(c)
            if t.startswith("#!"):
                continue
            body = re.sub(r"^/\*+|\*+/$", "", t.strip())
            body = "\n".join(re.sub(r"^\s*(\*|//)\s?", "", l) for l in body.split("\n")).strip()
            return {"text": body, "line": c.start_point[0] + 1} if body else None
        break
    return None


def role_of(rec, has_routes: bool, uses_db: bool) -> str:
    if rec.cls == "test":
        return "test"
    if rec.cls in ("config", "docs"):
        return rec.cls
    parts = rec.path.split("/")[:-1]
    for d, role in ROLE_DIRS:
        if d in parts:
            return role
    if uses_db:
        return "repository"
    if has_routes:
        return "controller"
    return "module"


def _by_file(rows, key):
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(r)
    return out


def build(bundle: dict, ctxs: dict[str, FileCtx], contents: dict[str, str]) -> tuple[list[dict], list[dict]]:
    T = bundle["tables"]
    snap = bundle["snapshot"]
    files = bundle["files"]
    sym = _by_file(T["symbols"], "file_path")
    refs_out = _by_file(T["symbol_refs"], "src_file")
    refs_in = _by_file([r for r in T["symbol_refs"] if r["kind"] != "import"], "dst_file")
    routes = _by_file(T["routes"], "route_file")
    calls = _by_file(T["outbound_calls"], "file_path")
    dbs = _by_file(T["db_access"], "file_path")
    queues = _by_file(T["queues"], "file_path")
    logs_ = _by_file(T["log_sites"], "file_path")
    finds = _by_file(T["findings"], "file_path")
    tests_for = _by_file(T["test_map"], "source_file")
    unresolved = {r["file_path"] for r in T["extraction_reports"] if r["file_path"]}
    git = {r["file_path"]: r["data"] for r in T["git_facts"] if r["kind"] == "file"}
    cards, mod_acc = [], {}

    for f in files:
        if f["state"] != "summarized":
            continue
        p = f["path"]
        ctx = ctxs.get(p)
        desc = header_comment(ctx) if ctx else None
        my_dbs = dbs.get(p, [])
        role = role_of_file = role_of(type("R", (), {"cls": f["class"], "path": p}), bool(routes.get(p)),
                                      bool(my_dbs))
        callers = sorted({(r["src_file"], r["src_symbol"], r["src_line"]) for r in refs_in.get(p, []) if r["src_file"] != p})
        callees = sorted({(r["dst_file"], r["dst_symbol"], r["src_line"]) for r in refs_out.get(p, [])
                          if r["kind"] != "import" and r["dst_file"] != p})
        side = set()
        for d in my_dbs:
            if d["operation"] in WRITE_OPS:
                side.add(f"db-write:{d['table_name'] or d['procedure']}:{d['operation']}")
        for q in queues.get(p, []):
            if q["direction"] == "send":
                side.add(f"queue-send:{q['queue_name'] or '<unresolved>'}")
        for c in calls.get(p, []):
            side.add(f"http-{'read' if c['method'] == 'GET' else 'write'}:{c['method']}")
        if ctx:
            for n in walk(ctx.root):
                if n.type == "call_expression":
                    fn = n.child_by_field_name("function")
                    if fn is not None and fn.type == "member_expression" and \
                            txt(fn.child_by_field_name("object")) == "fs" and \
                            txt(fn.child_by_field_name("property")) in FS_WRITES:
                        side.add("fs-write")
        status = "verified"
        if p in unresolved or any(r["status"] == "unverified" for r in routes.get(p, []) + calls.get(p, []) + my_dbs + queues.get(p, [])):
            status = "unverified"
        card = {
            "identity": {"repo": snap["repo"], "path": p, "commit_sha": snap["commit"],
                         "content_hash": f["content_hash"], "language": f["language"], "origin": "source"},
            "role": {"value": role, "origin": "derived"},
            "description": {"text": desc["text"], "evidence": f"{p}:{desc['line']}", "origin": "document"} if desc else None,
            "interface": {
                "exports": [{"name": s["name"], "kind": s["kind"], "line": s["line_start"]}
                            for s in sorted(sym.get(p, []), key=lambda s: (s["line_start"], s["name"])) if s["exported"]],
                "routes": [{"method": r["method"], "path": r["path"], "line": r["route_line"]}
                           for r in sorted(routes.get(p, []), key=lambda r: (r["route_line"], r["method"], r["path"]))],
                "origin": "source"},
            "calls": {
                "callers": [{"file": a, "symbol": b, "line": c} for a, b, c in callers],
                "callees": [{"file": a, "symbol": b, "line": c} for a, b, c in callees],
                "outbound_http": [{"method": c["method"], "url": c["url"], "line": c["line"], "status": c["status"]}
                                  for c in sorted(calls.get(p, []), key=lambda c: (c["line"], c["expression"]))],
                "queues": [{"direction": q["direction"], "queue": q["queue_name"], "line": q["line"]}
                           for q in sorted(queues.get(p, []), key=lambda q: (q["line"], q["direction"]))],
                "tables": sorted({f"{d['schema_name'] + '.' if d['schema_name'] else ''}{d['table_name'] or d['procedure']}:{d['operation']}"
                                  for d in my_dbs if d["table_name"] or d["procedure"]}),
                "origin": "source"},
            "logging": {"sites": [{"line": l["line"], "kind": l["kind"], "level": l["level"], "template": l["template"]}
                                  for l in sorted(logs_.get(p, []), key=lambda l: (l["line"], l["template"]))],
                        "origin": "source"},
            "findings": {"items": [{"rule_id": x["rule_id"], "line": x["line"], "detail": x["detail"]}
                                   for x in sorted(finds.get(p, []), key=lambda x: (x["line"], x["rule_id"]))],
                         "origin": "derived"},
            "side_effects": {"items": sorted(side), "origin": "derived"},
            "tests": {"files": sorted({t["test_file"] for t in tests_for.get(p, [])}), "origin": "derived"},
            "churn": {**git[p], "origin": "source"} if p in git else None,
            "status": {"value": status, "pipeline_version": PIPELINE_VERSION,
                       "extractor_versions": dict(sorted(EXTRACTOR_VERSIONS.items()))},
        }
        words = [p] + [s["name"] for s in sym.get(p, [])] + [r["path"] for r in routes.get(p, [])] \
            + [l["template"] for l in logs_.get(p, [])] + ([desc["text"]] if desc else [])
        cards.append({"file_path": p, "card": card, "status": status, "search_text": " ".join(words)})
        # module accumulation (each ancestor directory, incl. root '.')
        d = posixpath.dirname(p) or "."
        chain = [d]
        while d != ".":
            d = posixpath.dirname(d) or "."
            chain.append(d)
        for m in chain:
            a = mod_acc.setdefault(m, {"files": 0, "lines": 0, "classes": {}, "languages": set(), "routes": [], "tables": set(),
                                      "exports": 0, "children": set()})
            a["files"] += 1
            a["lines"] += f["lines"]
            a["classes"][f["class"]] = a["classes"].get(f["class"], 0) + 1
            if f["language"]:
                a["languages"].add(f["language"])
            a["routes"] += [f"{r['method']} {r['path']}" for r in routes.get(p, [])]
            a["tables"].update(card["calls"]["tables"])
            a["exports"] += len(card["interface"]["exports"])
    mods = []
    for m in sorted(mod_acc):
        a = mod_acc[m]
        mods.append({"module_path": m, "summary": {
            "files": a["files"], "lines": a["lines"], "classes": dict(sorted(a["classes"].items())),
            "languages": sorted(a["languages"]), "routes": sorted(a["routes"]), "tables": sorted(a["tables"]),
            "exported_symbols": a["exports"], "origin": "derived"}})
    return cards, mods
