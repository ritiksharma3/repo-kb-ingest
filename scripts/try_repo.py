"""Ingest one or more repos into the knowledge base and print a readable report.

    python scripts/try_repo.py <repo_path> [<repo_path> ...] [--name NAME] [--db mysql|postgres] [--sample N]

Defaults to the MySQL backend (KB_DB=mysql unless already set). A folder without a .git is copied (minus
node_modules/.git) into a fresh git repo under .try_repo_work/ so the input folder is never modified.
After ingesting all paths the repos are linked and cross-repo edges are shown.
"""
from __future__ import annotations
import argparse
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kbingest import db  # noqa: E402

db.load_dotenv()
for _s in (sys.stdout, sys.stderr):
    _s.reconfigure(encoding="utf-8", errors="replace")


def prepare_root(src: Path, work: Path) -> Path:
    from kbingest.fixtures import git, _force_rm
    if (src / ".git").exists():
        return src
    dest = work / src.name
    if dest.exists():
        shutil.rmtree(dest, onexc=_force_rm)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("node_modules", ".git"))
    git(dest, "init", "-b", "main")
    git(dest, "config", "core.autocrlf", "false")
    git(dest, "add", "-A")
    git(dest, "commit", "-m", "snapshot for kbingest")
    return dest


def h(t):
    print(f"\n=== {t} ===")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--name", help="repo name (only valid with a single path)")
    ap.add_argument("--db", choices=["mysql", "postgres"])
    ap.add_argument("--sample", type=int, default=8, help="rows to show per list")
    ap.add_argument("--mapping", type=Path, default=ROOT / "config" / "mappings.yaml")
    a = ap.parse_args()
    os.environ["KB_DB"] = a.db or os.environ.get("KB_DB") or "mysql"
    if a.name and len(a.paths) > 1:
        ap.error("--name needs a single path")
    from kbingest import publish, tools
    work = ROOT / ".try_repo_work"
    work.mkdir(exist_ok=True)
    print(f"backend: {db.backend()}")
    names, bad = [], False
    for p in a.paths:
        src = p.resolve()
        root = prepare_root(src, work)
        name = a.name or src.name
        rep = publish.ingest(root, "HEAD", name, a.mapping, None, None, link_after=False)
        names.append(name)
        h(f"INGEST {name}")
        print(f"status: {rep['status']}  commit: {str(rep.get('commit'))[:12]}  branch: {rep.get('branch')}")
        if rep.get("error"):
            print("error:", rep["error"]); print(rep.get("trace", ""))
        if rep.get("reason"):
            print("reason:", rep["reason"])
        st = rep.get("selftest")
        if st:
            print(f"self-test gate: {'PASS' if st['passed'] else 'FAIL'} ({st['total']} gates)")
            for f in st["failures"]:
                print("  FAIL", f["gate"], f["detail"])
        print("coverage:", rep.get("coverage"))
        bad = bad or rep["status"] not in ("published", "unchanged")
    h("LINK (cross-repo edges)")
    print(publish.relink_others(None, a.mapping))
    for name in names:
        try:
            with db.connect("reader") as c:
                report(c, name, a.sample, tools)
        except tools.ToolError as e:
            print(f"\n[{name}] {e}")
            bad = True
    sys.exit(1 if bad else 0)


def report(c, name, n, tools):
    ov = tools.get_repo_overview(c, name)
    h(f"[{name}] OVERVIEW  @ {ov['commit_sha'][:12]} ({ov['branch']})")
    print("card:", ov["card"])
    h(f"[{name}] DOSSIER SECTIONS")
    for s in tools.get_repo_dossier(c, name)["sections"]:
        md = s["markdown"].strip()
        print(f"\n--- {s['section_key']} (origin={s['origin']}, status={s['status']}) ---")
        print(md if len(md) < 1500 else md[:1500] + "\n... [truncated]")
    h(f"[{name}] ROUTES")
    snap = tools._snap(c, name)
    rows = tools._q(c, "SELECT method, path, route_file, route_line, handler_symbol, status FROM routes "
                       "WHERE snapshot_id=%s ORDER BY path, method", (snap["id"],))
    for r in rows:
        print(f"  {r['method']:7} {r['path']:40} {r['route_file']}:{r['route_line']} -> {r['handler_symbol']} [{r['status']}]")
    print(f"  ({len(rows)} routes)")
    h(f"[{name}] FILE CARDS (sample)")
    for r in tools._q(c, "SELECT file_path, status, card FROM file_cards WHERE snapshot_id=%s ORDER BY file_path LIMIT %s",
                      (snap["id"], n)):
        card = r["card"]
        print(f"  {r['file_path']} [{r['status']}] keys={sorted(card)[:8]}")
    h(f"[{name}] FINDINGS")
    fs = tools._q(c, "SELECT file_path, line, rule_id, detail FROM findings WHERE snapshot_id=%s ORDER BY file_path, line", (snap["id"],))
    for f in fs[:max(n, 15)]:
        print(f"  {f['file_path']}:{f['line']} {f['rule_id']}: {f['detail'][:100]}")
    print(f"  ({len(fs)} findings)")
    fr = tools.get_index_freshness(c, name)
    h(f"[{name}] EXTRACTION GAPS / FRESHNESS")
    print("indexed_sha:", fr["indexed_sha"][:12], " coverage:", fr["coverage"])
    for g in fr["extraction_gaps"]:
        print("  gap:", g)
    if not fr["extraction_gaps"]:
        print("  (none)")
    h(f"[{name}] SAMPLE TOOL CALLS")
    if rows:
        r0 = rows[0]
        print(f"find_route({r0['method']}, {r0['path']}):")
        for x in tools.find_route(c, name, r0["method"], r0["path"]):
            print("  ", x["evidence"], "->", x["handler_file"], x["handler_symbol"], "mw=", x["middleware"])
    for q in ("auth", "login", "order"):
        hits = tools.search_code(c, name, q, limit=5)
        print(f"search_code({q!r}): " + ("; ".join(f"{h_['name']}@{h_['evidence']} ({h_['match']})" for h_ in hits) or "no hits"))
    dep = tools.get_dependencies(c, name)
    print(f"get_dependencies: outbound_calls={len(dep['outbound_calls'])} db_access={len(dep['db_access'])} "
          f"queues={len(dep['queues'])}")
    print(f"  cross-repo edges out: {len(dep['edges'])}, dependents (in): {len(dep['dependents'])}")
    for e in dep["edges"][:n]:
        print("  edge:", e["src_ref"], "->", e["dst_repo"], e["dst_ref"], e["kind"])
    for e in dep["dependents"][:n]:
        print("  dependent:", e["src_repo"], e["src_ref"], "->", e["dst_ref"])
    print("get_index_freshness:", {k: fr[k] for k in ("indexed_sha", "up_to_date")}, "last_run:", fr["last_run"])


if __name__ == "__main__":
    main()
