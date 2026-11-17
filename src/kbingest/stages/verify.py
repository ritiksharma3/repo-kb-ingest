"""Stage 6: deterministic verification. Any failure blocks publishing."""
from __future__ import annotations
import json
from pathlib import Path

from .. import bundle as B
from ..dossier import render as R
from ..hashing import canon, line_hash

LINE_COLS = {"symbols": ("file_path", "line_start"), "symbol_refs": ("src_file", "src_line"),
             "routes": ("route_file", "route_line"), "outbound_calls": ("file_path", "line"),
             "db_access": ("file_path", "line"), "queues": ("file_path", "line"),
             "log_sites": ("file_path", "line"), "findings": ("file_path", "line")}


def _check(results: list, name: str, problems: list[str]):
    results.append({"check": name, "ok": not problems, "problems": problems[:20], "problem_count": len(problems)})


def run(prep, fixtures_dir: Path | None = None, golden_dir: Path | None = None) -> list[dict]:
    res: list[dict] = []
    bundle, rendered, T = prep.bundle, prep.rendered, prep.bundle["tables"]
    lines = {p: c.split("\n") for p, c in prep.contents.items()}

    # 1 cited file:line exists, and matches the stored text hash
    probs = []
    for table, (fc, lc) in LINE_COLS.items():
        for r in T[table]:
            ls = lines.get(r[fc])
            if ls is None or not (1 <= r[lc] <= len(ls)):
                probs.append(f"{table} {r[fc]}:{r[lc]} does not exist")
            elif line_hash(ls[r[lc] - 1]) != r["line_hash"]:
                probs.append(f"{table} {r[fc]}:{r[lc]} text hash mismatch")
    for key, sec in rendered["sections"].items():
        for ev in sec["evidence"]:
            if ev.startswith("mapping:"):
                continue
            f, _, l = ev.rpartition(":")
            ls = lines.get(f)
            if ls is None or not l.isdigit() or not (1 <= int(l) <= max(len(ls), 1)):
                probs.append(f"dossier {key} cites {ev} which does not exist")
    _check(res, "evidence_file_line_exists_and_hash_matches", probs)

    # 2 dossier JSON validates against its schema
    probs = [f"{k}: {e}" for k, s in rendered["sections"].items() for e in R.validate(s)]
    _check(res, "dossier_json_schema", probs)

    # 3 re-render from the fact tables and compare
    b2 = B.make(bundle["snapshot"], bundle["mapping"], bundle["files"], bundle["manifests"],
                json.loads(canon(T)), bundle.get("edges"))
    r2 = R.render_all(b2, rendered["card"]["coverage"])
    _check(res, "dossier_rerender_identical", [] if R.fingerprint(r2) == R.fingerprint(rendered) else ["re-render differs"])

    # 4 dossier contents equal extractor tables exactly
    secs = rendered["sections"]
    probs = []

    def kinds(sec, kind):
        return [i for i in secs[sec]["items"] if i["fields"]["kind"] == kind]
    exp_routes = sorted((r["method"], r["path"], f"{r['route_file']}:{r['route_line']}") for r in T["routes"])
    got_routes = sorted((i["fields"]["method"], i["fields"]["path"], i["evidence"][0]) for i in kinds("entry_points", "route"))
    if exp_routes != got_routes:
        probs.append("routes differ from extractor table")
    if sum(i["fields"]["sites"] for i in kinds("data_access", "db_access")) != len(T["db_access"]):
        probs.append("data_access sites differ from db_access table")
    if len(kinds("dependencies", "queue")) != len(T["queues"]) or len(kinds("dependencies", "outbound_http")) != len(T["outbound_calls"]):
        probs.append("dependencies differ from queues/outbound_calls tables")
    exp_logs = sorted((l["template"], f"{l['file_path']}:{l['line']}") for l in T["log_sites"])
    got_logs = sorted((i["fields"]["template"], i["evidence"][0]) for i in kinds("observability", "log_template"))
    if exp_logs != got_logs:
        probs.append("log strings differ from log_sites table")
    if len(secs["error_handling"]["items"]) != len(T["findings"]):
        probs.append("findings differ")
    _check(res, "dossier_equals_extractor_tables", probs)

    # 5 every in-scope file has a card; every module has a summary
    probs = []
    carded = {c["file_path"] for c in prep.cards}
    for f in bundle["files"]:
        if f["state"] == "summarized" and f["path"] not in carded:
            probs.append(f"no card for {f['path']}")
    dirs = set()
    for f in bundle["files"]:
        if f["state"] == "summarized":
            d = f["path"].rsplit("/", 1)[0] if "/" in f["path"] else "."
            while True:
                dirs.add(d)
                if d == ".":
                    break
                d = d.rsplit("/", 1)[0] if "/" in d else "."
    have = {m["module_path"] for m in prep.modules}
    probs += [f"no module summary for {d}" for d in sorted(dirs - have)]
    _check(res, "cards_and_modules_complete", probs)

    # 6 every detected framework has an extractor; zero-result extractors explained
    from .extract import FRAMEWORK_EXTRACTORS
    probs = []
    reported = {(r["kind"], r["subject"]) for r in T["extraction_reports"]}
    for fw in prep.inv.frameworks:
        if fw not in FRAMEWORK_EXTRACTORS:
            if ("framework_no_extractor", fw) not in reported:
                probs.append(f"{fw}: no extractor and not reported as a gap")
        elif not T[FRAMEWORK_EXTRACTORS[fw][1]] and ("extractor_zero_results", fw) not in reported:
            probs.append(f"{fw}: zero results not explained")
    _check(res, "framework_extractor_coverage", probs)

    # 7 coverage ledger: every file in exactly one terminal state
    probs = [f"{f['path']}: state {f['state']!r}" for f in bundle["files"] if f["state"] not in ("summarized", "skipped", "failed")]
    probs += [f"{f['path']}: skipped without a reason" for f in bundle["files"] if f["state"] == "skipped" and not f["reason"]]
    probs += [f"{f['path']}: failed without an error" for f in bundle["files"] if f["state"] == "failed" and not f["reason"]]
    _check(res, "coverage_ledger_terminal_states", probs)

    # 8 extractor golden fixtures pass for the pinned versions
    if fixtures_dir and golden_dir and fixtures_dir.exists():
        from .. import golden
        _check(res, "extractor_golden_fixtures", golden.check(fixtures_dir, golden_dir))
    else:
        res.append({"check": "extractor_golden_fixtures", "ok": True, "problems": [], "problem_count": 0,
                    "skipped": "fixtures directory not available"})
    return res
