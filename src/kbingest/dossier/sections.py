"""The 12 dossier sections as pure functions: bundle -> JSON-able dict. No generated prose."""
from __future__ import annotations
import posixpath
import re

from ..bundle import manifest

SECTION_KEYS = ["identity_purpose", "deployment_environments", "build_test_run", "architecture_modules",
                "entry_points", "conventions", "data_access", "dependencies", "observability",
                "error_handling", "change_profile", "unknowns_gaps"]
TITLES = {
    "identity_purpose": "Identity and purpose", "deployment_environments": "Deployment and environments",
    "build_test_run": "Build, test and run", "architecture_modules": "Architecture and modules",
    "entry_points": "Entry points", "conventions": "Conventions and patterns", "data_access": "Data access",
    "dependencies": "Dependencies", "observability": "Observability",
    "error_handling": "Error handling and failure modes", "change_profile": "Change profile",
    "unknowns_gaps": "Unknowns and gaps",
}
TEST_FRAMEWORKS = ["jest", "mocha", "vitest", "jasmine", "ava", "supertest", "tap"]
LOGGER_LIBS = ["winston", "pino", "bunyan", "log4js", "morgan"]
LAYERS = ["controllers", "services", "repositories", "routes", "models", "middleware", "utils", "handlers", "config"]


def item(origin: str, evidence: list[str], **fields) -> dict:
    return {"fields": dict(sorted(fields.items())), "evidence": sorted(set(evidence)), "origin": origin}


def section(key: str, items: list[dict], searched: list[str], summary: dict | None = None) -> dict:
    ev = sorted({e for it in items for e in it["evidence"]})
    origins = sorted({it["origin"] for it in items})
    return {"key": key, "title": TITLES[key], "status": "present" if items else "not_found",
            "origin": origins[0] if len(origins) == 1 else ("mixed" if origins else "none"),
            "summary": summary or {}, "items": items,
            "not_found": None if items else {"searched": searched}, "evidence": ev}


def _loc(f, l):
    return f"{f}:{l}"


def identity_purpose(b):
    items, m = [], manifest(b, "package_json").get("package.json")
    if m:
        items.append(item("source", ["package.json:1"], kind="package", name=m["name"], version=m["version"],
                          main=m["main"], engines=m["engines"]))
        items.append(item("source", ["package.json:1"], kind="dependencies", runtime=m["dependencies"],
                          dev=m["devDependencies"]))
    langs = {}
    for f in b["files"]:
        if f["language"] and f["class"] in ("source", "test") and f["state"] != "skipped":
            langs[f["language"]] = langs.get(f["language"], 0) + 1
    if langs:
        items.append(item("derived", [f"{sorted(f['path'] for f in b['files'] if f['language'] in langs)[0]}:1"],
                          kind="languages", files_per_language=dict(sorted(langs.items()))))
    fw = manifest(b, "frameworks").get("", {})
    if fw:
        items.append(item("derived", [e.split(" ")[0] for v in fw.values() for e in v["evidence"]],
                          kind="frameworks", names=sorted(fw)))
    rd = manifest(b, "readme").get("README.md")
    if rd and rd["first_paragraph"]:
        items.append(item("document", [_loc("README.md", rd["line"])], kind="purpose_quote", text=rd["first_paragraph"]))
    suite = b["mapping"].get("suite")
    if suite:
        items.append(item("document", ["mapping:suites"], kind="suite", name=suite))
    return section("identity_purpose", items, ["package.json", "README.md", "mapping file"])


def deployment_environments(b):
    items = []
    for p, d in manifest(b, "dockerfile").items():
        items.append(item("source", [_loc(p, d["from"][0]["line"] if d["from"] else 1)], kind="dockerfile", path=p,
                          base_images=[x["image"] for x in d["from"]], expose=d["expose"], cmd=d["cmd"],
                          entrypoint=d["entrypoint"], env_keys=d["env_keys"]))
    for p, d in manifest(b, "pm2").items():
        items.append(item("source", [_loc(p, a["line"]) for a in d["apps"]] or [f"{p}:1"], kind="pm2", path=p,
                          apps=[a["name"] for a in d["apps"]], scripts=d["scripts"]))
    for p, d in manifest(b, "jenkinsfile").items():
        items.append(item("source", [_loc(p, s["line"]) for s in d["stages"]] or [f"{p}:1"], kind="jenkinsfile", path=p,
                          stages=[s["name"] for s in d["stages"]], commands=[c["cmd"] for c in d["commands"]]))
    for p, d in manifest(b, "ci").items():
        items.append(item("source", [f"{p}:1"], kind="ci", path=p, jobs=d["jobs"]))
    for p, d in manifest(b, "env_keys").items():
        items.append(item("source", [f"{p}:1"], kind="env_keys", path=p, key_names=d["keys"]))
    be = b["mapping"].get("branch_environment") or {}
    if be:
        items.append(item("document", ["mapping:branch_environment"], kind="branch_environment", mapping=dict(sorted(be.items())),
                          this_branch=b["snapshot"]["branch"], this_environment=b["mapping"].get("environment")))
    return section("deployment_environments", items, ["Dockerfile", "ecosystem.config.js", "Jenkinsfile", ".github/workflows", ".env*"])


def build_test_run(b):
    items = []
    pj = manifest(b, "package_json").get("package.json")
    if pj:
        if pj["scripts"]:
            items.append(item("source", ["package.json:1"], kind="scripts", scripts=pj["scripts"]))
        deps = {**pj["dependencies"], **pj["devDependencies"]}
        tf = sorted(t for t in TEST_FRAMEWORKS if t in deps)
        if tf:
            items.append(item("derived", ["package.json:1"], kind="test_framework", names=tf))
    for p, d in manifest(b, "makefile").items():
        items.append(item("source", [f"{p}:1"], kind="makefile", path=p, targets=d["targets"]))
    for p, d in manifest(b, "ci").items():
        items.append(item("source", [f"{p}:1"], kind="ci_commands", path=p, jobs=d["jobs"]))
    covered = {r["source_file"] for r in b["tables"]["test_map"]}
    src = sorted(f["path"] for f in b["files"] if f["class"] == "source" and f["state"] == "summarized")
    untested = [p for p in src if p not in covered]
    tests = sorted(f["path"] for f in b["files"] if f["class"] == "test")
    items.append(item("derived", [f"{p}:1" for p in untested[:50]] or ["package.json:1"], kind="test_coverage_map",
                      source_files=len(src), test_files=len(tests), untested_source_files=untested))
    return section("build_test_run", [i for i in items if i["fields"].get("kind") != "test_coverage_map" or src or tests],
                   ["package.json scripts", "Makefile", "CI config"])


def _sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    idx, low, stack, on, out, c = {}, {}, [], set(), [], [0]

    def go(v):
        idx[v] = low[v] = c[0]; c[0] += 1
        stack.append(v); on.add(v)
        for w in sorted(graph.get(v, ())):
            if w not in idx:
                go(w); low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp = []
            while True:
                w = stack.pop(); on.discard(w); comp.append(w)
                if w == v:
                    break
            if len(comp) > 1 or v in graph.get(v, ()):
                out.append(sorted(comp))
    for v in sorted(graph):
        if v not in idx:
            go(v)
    return sorted(out)


def architecture_modules(b):
    items, T = [], b["tables"]
    src = [f for f in b["files"] if f["class"] == "source" and f["state"] == "summarized"]
    layers = {}
    for f in src:
        for part in f["path"].split("/")[:-1]:
            if part in LAYERS:
                layers.setdefault(part, []).append(f["path"])
    if layers:
        items.append(item("derived", [f"{v[0]}:1" for v in layers.values()], kind="layers",
                          layers={k: sorted(v) for k, v in sorted(layers.items())}))
    tops = {}
    routes_by = {}
    for r in T["routes"]:
        routes_by.setdefault(r["route_file"], []).append(r)
    for f in src:
        top = f["path"].split("/")[0] if "/" in f["path"] else "."
        t = tops.setdefault(top, {"files": 0, "lines": 0, "routes": 0, "first": f["path"]})
        t["files"] += 1; t["lines"] += f["lines"]; t["routes"] += len(routes_by.get(f["path"], []))
    tables_by = {}
    for d in T["db_access"]:
        if d["table_name"]:
            top = d["file_path"].split("/")[0] if "/" in d["file_path"] else "."
            tables_by.setdefault(top, set()).add(d["table_name"])
    for top, t in sorted(tops.items()):
        items.append(item("derived", [f"{t['first']}:1"], kind="module", module=top,
                          files=t["files"], lines=t["lines"], routes=t["routes"], tables=sorted(tables_by.get(top, []))))
    graph: dict[str, set[str]] = {}
    ev = {}
    for r in T["symbol_refs"]:
        if r["kind"] == "import" and r["dst_file"]:
            graph.setdefault(r["src_file"], set()).add(r["dst_file"])
            ev.setdefault(r["src_file"], _loc(r["src_file"], r["src_line"]))
    cycles = _sccs(graph)
    if src:
        items.append(item("derived", [ev[f] for c in cycles for f in c if f in ev] or [f"{src[0]['path']}:1"],
                          kind="import_cycles", count=len(cycles), cycles=cycles))
    return section("architecture_modules", items, ["source files"])


def entry_points(b):
    T, items = b["tables"], []
    for r in sorted(T["routes"], key=lambda r: (r["path"], r["method"], r["route_file"], r["route_line"])):
        items.append(item("source", [_loc(r["route_file"], r["route_line"])], kind="route", method=r["method"], path=r["path"],
                          handler=f"{r['handler_file']}:{r['handler_line']}" if r["handler_file"] else None,
                          handler_symbol=r["handler_symbol"], middleware=r["middleware"], status=r["status"]))
    for q in sorted(T["queues"], key=lambda q: (q["file_path"], q["line"])):
        if q["direction"] == "receive":
            items.append(item("source", [_loc(q["file_path"], q["line"])], kind="queue_consumer", queue=q["queue_name"],
                              library=q["library"], status=q["status"]))
    return section("entry_points", items, ["express routes", "SQS consumers",
                                           "scheduled jobs (no scheduler extractor exists)"])


def conventions(b):
    items = [item("derived", [_loc(c["representative_file"], c["representative_line"])] if c["representative_file"] else [],
                  kind="convention", pattern=c["pattern"], majority=c["majority"], counts=c["counts"],
                  representative=f"{c['representative_file']}:{c['representative_line']}" if c["representative_file"] else None,
                  outliers=c["outliers"]) for c in b["tables"]["convention_stats"]]
    for it, c in zip(items, b["tables"]["convention_stats"]):
        it["evidence"] = sorted({_loc(o["file"], o["line"]) for o in c["outliers"]} | set(it["evidence"]))
    return section("conventions", items, ["source files"])


def data_access(b):
    agg = {}
    for d in b["tables"]["db_access"]:
        key = (d["schema_name"], d["table_name"], d["procedure"], d["operation"], d["status"])
        agg.setdefault(key, []).append(_loc(d["file_path"], d["line"]))
    items = [item("source", locs, kind="db_access", schema=k[0] or None, table=k[1] or None, procedure=k[2] or None,
                  operation=k[3], status=k[4], sites=len(locs)) for k, locs in sorted(agg.items())]
    mig = sorted(f["path"] for f in b["files"] if f["state"] == "summarized" and
                 (re.search(r"(^|/)migrations?/", f["path"]) or f["path"].endswith(".sql")))
    if mig:
        items.append(item("derived", [f"{p}:1" for p in mig], kind="migrations", paths=mig))
    return section("data_access", items, ["mssql query/execute calls", "migration folders", "*.sql"])


def dependencies(b):
    items, T = [], b["tables"]
    for c in sorted(T["outbound_calls"], key=lambda c: (c["file_path"], c["line"], c["expression"])):
        items.append(item("source", [_loc(c["file_path"], c["line"])], kind="outbound_http", client=c["client"],
                          method=c["method"], url=c["url"], expression=c["expression"], status=c["status"]))
    for q in sorted(T["queues"], key=lambda q: (q["file_path"], q["line"], q["direction"])):
        items.append(item("source", [_loc(q["file_path"], q["line"])], kind="queue", direction=q["direction"],
                          queue=q["queue_name"], status=q["status"]))
    for e in sorted(b.get("edges", []), key=lambda e: (e["src_ref"], e["dst_repo"], e["dst_ref"], e["kind"])):
        items.append(item("derived", [e["src_ref"]], kind="cross_repo_edge", edge_kind=e["kind"], dst_repo=e["dst_repo"],
                          dst_ref=e["dst_ref"], dst_evidence=e["evidence"].get("dst")))
    return section("dependencies", items, ["outbound HTTP calls", "queues", "cross-repo edges (stages/link.py)"])


def observability(b):
    items, T = [], b["tables"]
    pj = manifest(b, "package_json").get("package.json")
    deps = {**pj["dependencies"], **pj["devDependencies"]} if pj else {}
    libs = sorted(l for l in LOGGER_LIBS if l in deps)
    if libs:
        items.append(item("source", ["package.json:1"], kind="logger_libraries", names=libs))
    for c in T["convention_stats"]:
        if c["pattern"] == "logger":
            items.append(item("derived", [_loc(c["representative_file"], c["representative_line"])] if c["representative_file"] else [],
                              kind="logger_usage", majority=c["majority"], counts=c["counts"]))
    for l in sorted(T["log_sites"], key=lambda l: (l["file_path"], l["line"], l["template"])):
        items.append(item("source", [_loc(l["file_path"], l["line"])], kind="log_template", log_kind=l["kind"],
                          level=l["level"], template=l["template"]))
    return section("observability", items, ["logger/console calls", "throw new Error(...)", "package.json logger libraries"])


def error_handling(b):
    items = [item("derived", [_loc(f["file_path"], f["line"])], kind="finding", rule_id=f["rule_id"], detail=f["detail"],
                  symbol=f["enclosing_symbol"])
             for f in sorted(b["tables"]["findings"], key=lambda f: (f["rule_id"], f["file_path"], f["line"]))]
    return section("error_handling", items, ["rule detectors: http-call-no-timeout, empty-catch, catch-only-logs, retry-present, unhandled-rejection-handler, dynamic-sql"])


def change_profile(b):
    repo = next((g for g in b["tables"]["git_facts"] if g["kind"] == "repo"), None)
    items = []
    if repo:
        d = repo["data"]
        items.append(item("source", ["package.json:1" if any(f["path"] == "package.json" for f in b["files"]) else b["files"][0]["path"] + ":1"],
                          kind="history", total_commits=d["total_commits"], churn_ranking=d["churn_ranking"],
                          recent_commits=d["recent_commits"]))
        if d["codeowners"]:
            items.append(item("source", [_loc(d["codeowners_path"], r["line"]) for r in d["codeowners"]],
                              kind="codeowners", rules=d["codeowners"]))
        fixes = sorted(((g["file_path"], g["data"]["fix_commits"]) for g in b["tables"]["git_facts"]
                        if g["kind"] == "file" and g["data"]["fix_commits"]), key=lambda kv: (-kv[1], kv[0]))
        if fixes:
            items.append(item("source", [f"{f}:1" for f, _ in fixes[:10]], kind="fix_style_commits",
                              per_file=[{"file": f, "fix_commits": n} for f, n in fixes]))
    return section("change_profile", items, ["git history"])


def unknowns_gaps(b):
    items = []
    by_kind = {}
    for r in b["tables"]["extraction_reports"]:
        by_kind.setdefault(r["kind"], []).append(r)
    for kind, rows in sorted(by_kind.items()):
        for r in sorted(rows, key=lambda r: (r["subject"], r["file_path"], r["line"])):
            ev = [_loc(r["file_path"], r["line"])] if r["file_path"] and r["line"] else (
                [f"{r['file_path']}:1"] if r["file_path"] else [])
            if not ev:   # repo-level gap: cite where the framework was detected
                fwev = manifest(b, "frameworks").get("", {}).get(r["subject"], {}).get("evidence", [])
                ev = [e.split(" ")[0] for e in fwev[:3]] or [f"{b['files'][0]['path']}:1"]
            items.append(item("derived", ev, kind=kind, subject=r["subject"], detail=r["detail"]))
    for f in b["files"]:
        if f["state"] == "failed" and not any(i["fields"]["subject"] == f["path"] for i in items if "subject" in i["fields"]):
            items.append(item("derived", [f"{f['path']}:1"], kind="failed_file", subject=f["path"], detail=f["reason"] or ""))
    return section("unknowns_gaps", items, ["extraction reports"])


BUILDERS = {k: globals()[k] for k in SECTION_KEYS}


def build_all(bundle: dict) -> dict[str, dict]:
    return {k: BUILDERS[k](bundle) for k in SECTION_KEYS}


def repo_card(bundle: dict, sections: dict[str, dict], coverage: dict) -> dict:
    T = bundle["tables"]
    idn = {i["fields"]["kind"]: i for i in sections["identity_purpose"]["items"]}
    pkg = idn.get("package", {"fields": {}})["fields"]
    return {
        "repo": bundle["snapshot"]["repo"], "branch": bundle["snapshot"]["branch"], "commit": bundle["snapshot"]["commit"],
        "name": pkg.get("name"), "version": pkg.get("version"),
        "purpose_quote": idn.get("purpose_quote", {"fields": {}})["fields"].get("text"),
        "frameworks": idn.get("frameworks", {"fields": {}})["fields"].get("names", []),
        "counts": {"routes": len(T["routes"]), "outbound_calls": len(T["outbound_calls"]),
                   "db_access": len(T["db_access"]), "queues": len(T["queues"]),
                   "log_sites": len(T["log_sites"]), "findings": len(T["findings"]),
                   "extraction_gaps": len(T["extraction_reports"])},
        "coverage": coverage,
        "sections": [{"key": k, "title": TITLES[k], "status": sections[k]["status"]} for k in SECTION_KEYS],
    }
