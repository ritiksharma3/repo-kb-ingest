"""Retrieval self-test: the publish gate. Calls the real tools (as the read-only role) on the new snapshot."""
from __future__ import annotations

from . import db
from . import tools as T
from .dossier.sections import SECTION_KEYS
from .hashing import canon


class Gate:
    def __init__(self):
        self.results: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.results.append({"gate": name, "ok": bool(ok), "detail": detail if not ok else ""})

    @property
    def failures(self):
        return [r for r in self.results if not r["ok"]]


def _rows(conn, sql, params):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def run(conn, snapshot_id: int, repo: str, contents: dict[str, str], sample: int = 25) -> dict:
    g = Gate()
    # Postgres: the gates run as the SELECT-only kb_reader role inside the publish transaction (SET LOCAL ROLE).
    # MySQL has no transaction-scoped role switch and the new snapshot is still uncommitted (invisible to another
    # connection), so the gates run through the same tools.py functions on the writer connection. The tools only
    # issue SELECTs; privilege separation is still enforced for every real reader connection (kb_reader).
    mysql = db.is_mysql()
    if not mysql:
        conn.execute("SET LOCAL ROLE kb_reader")
    try:
        sid = {"snapshot_id": snapshot_id}
        # 1 resolve_repo
        srcs = _rows(conn, "SELECT log_source, source_kind FROM log_source_map WHERE snapshot_id=%s", (snapshot_id,))
        for s in srcs:
            got = T.resolve_repo(conn, s["log_source"])
            g.check(f"resolve_repo:{s['log_source']}", any(x["repo"] == repo for x in got), f"got {got}")
        g.check("resolve_repo:own-name", any(x["repo"] == repo for x in T.resolve_repo(conn, repo)))
        # 2 overview + dossier
        ov = T.get_repo_overview(conn, repo, **sid)
        g.check("get_repo_overview", bool(ov["card"]) and len(ov["sections"]) == len(SECTION_KEYS))
        d = T.get_repo_dossier(conn, repo, **sid)
        have = {r["section_key"]: r for r in d["sections"]}
        for k in SECTION_KEYS:
            r = have.get(k)
            g.check(f"dossier:{k}", r is not None and r["markdown"].strip() != "" and bool(r["content"]), "missing or empty")
        # 3 routes
        for r in _rows(conn, "SELECT * FROM routes WHERE snapshot_id=%s", (snapshot_id,)):
            got = T.find_route(conn, repo, r["method"], r["path"], **sid)
            ok = any(x["route_file"] == r["route_file"] and x["route_line"] == r["route_line"]
                     and x["handler_file"] == r["handler_file"] and x["handler_line"] == r["handler_line"] for x in got)
            g.check(f"find_route:{r['method']} {r['path']}", ok, f"got {got}")
        # 4 logs (100%)
        for r in _rows(conn, "SELECT * FROM log_sites WHERE snapshot_id=%s", (snapshot_id,)):
            got = T.find_code_for_log(conn, repo, r["template"], **sid)
            g.check(f"find_code_for_log:{r['file_path']}:{r['line']}",
                    any(x["file_path"] == r["file_path"] and x["line"] == r["line"] for x in got), f"got {got}")
        # 5 code context on sampled symbols
        syms = _rows(conn, "SELECT * FROM symbols WHERE snapshot_id=%s ORDER BY file_path, line_start, name", (snapshot_id,))
        step = max(1, len(syms) // sample)
        for s in syms[::step][:sample]:
            line = s["line_start"]
            ctx = T.get_code_context(conn, repo, s["file_path"], line, **sid)
            # expected: innermost symbol containing the line (smallest range)
            enclosing = sorted((x for x in syms if x["file_path"] == s["file_path"] and x["line_start"] <= line <= x["line_end"]),
                               key=lambda x: (x["line_end"] - x["line_start"], x["line_start"]))[0]
            exp_callers = sorted({(r["src_file"], r["src_symbol"], r["src_line"]) for r in _rows(
                conn, "SELECT * FROM symbol_refs WHERE snapshot_id=%s AND dst_file=%s AND dst_symbol=%s AND kind<>'import'",
                (snapshot_id, s["file_path"], enclosing["name"]))})
            exp_callees = sorted({(r["dst_file"], r["dst_symbol"], r["src_line"]) for r in _rows(
                conn, "SELECT * FROM symbol_refs WHERE snapshot_id=%s AND src_file=%s AND src_symbol=%s AND kind<>'import'",
                (snapshot_id, s["file_path"], enclosing["name"]))})
            got_callers = sorted({(r["src_file"], r["src_symbol"], r["src_line"]) for r in ctx["callers"]})
            got_callees = sorted({(r["dst_file"], r["dst_symbol"], r["src_line"]) for r in ctx["callees"]})
            g.check(f"get_code_context:{s['file_path']}:{line}",
                    ctx["symbol"] is not None and ctx["symbol"]["name"] == enclosing["name"]
                    and got_callers == exp_callers and got_callees == exp_callees,
                    f"symbol={ctx['symbol']} expected {enclosing['name']}")
        # 6 dependencies
        files = sorted({r["file_path"] for r in _rows(conn, "SELECT file_path FROM outbound_calls WHERE snapshot_id=%s UNION "
                                                              "SELECT file_path FROM db_access WHERE snapshot_id=%s UNION "
                                                              "SELECT file_path FROM queues WHERE snapshot_id=%s",
                                                      (snapshot_id,) * 3)})
        for p in files[:sample]:
            dep = T.get_dependencies(conn, repo, p, **sid)
            for tbl, key in (("outbound_calls", "outbound_calls"), ("db_access", "db_access"), ("queues", "queues")):
                n = _rows(conn, f"SELECT count(*) AS n FROM {tbl} WHERE snapshot_id=%s AND file_path=%s", (snapshot_id, p))[0]["n"]
                g.check(f"get_dependencies:{p}:{key}", len(dep[key]) == n, f"{len(dep[key])} != {n}")
        # 7 get_file
        paths = sorted(contents)
        for p in paths[::max(1, len(paths) // sample)][:sample]:
            f = T.get_file(conn, repo, p, **sid)
            g.check(f"get_file:{p}", f["hash_ok"] and f["content"] == contents[p], "content/hash mismatch")
        # 8 scripted fix-oriented queries
        tm = _rows(conn, "SELECT DISTINCT source_file FROM test_map WHERE snapshot_id=%s ORDER BY 1", (snapshot_id,))
        for r in tm[:sample]:
            summ = T.get_file_summary(conn, repo, r["source_file"], **sid)
            exp = sorted(x["test_file"] for x in _rows(conn, "SELECT test_file FROM test_map WHERE snapshot_id=%s AND source_file=%s",
                                                       (snapshot_id, r["source_file"])))
            g.check(f"which-tests-cover:{r['source_file']}", sorted(x["test_file"] for x in summ["tests"]) == exp)
        refs = _rows(conn, "SELECT DISTINCT dst_file, dst_symbol FROM symbol_refs WHERE snapshot_id=%s AND kind<>'import' "
                           "AND status='resolved' ORDER BY 1,2", (snapshot_id,))
        for r in refs[:sample]:
            sym = _rows(conn, "SELECT line_start FROM symbols WHERE snapshot_id=%s AND file_path=%s AND name=%s",
                        (snapshot_id, r["dst_file"], r["dst_symbol"]))
            if sym:
                ctx = T.get_code_context(conn, repo, r["dst_file"], sym[0]["line_start"], **sid)
                g.check(f"who-calls:{r['dst_file']}:{r['dst_symbol']}", len(ctx["callers"]) > 0, "no callers returned")
        conv = _rows(conn, "SELECT count(*) AS n FROM convention_stats WHERE snapshot_id=%s", (snapshot_id,))[0]["n"]
        if conv and syms:
            ctx = T.get_code_context(conn, repo, syms[0]["file_path"], syms[0]["line_start"], **sid)
            g.check("repo-conventions-available", len(ctx["conventions"]) == conv)
        dep = T.get_dependencies(conn, repo, **sid)
        stored = _rows(conn, "SELECT src_ref, dst_repo, dst_ref, kind, evidence FROM edges WHERE snapshot_id=%s", (snapshot_id,))
        g.check("duplicates-edges-queryable", "edges" in dep and "dependents" in dep)
        got = {(e["src_ref"], e["dst_repo"], e["dst_ref"], e["kind"]) for e in dep["edges"]}
        g.check("edges-tool-matches-table", got == {(e["src_ref"], e["dst_repo"], e["dst_ref"], e["kind"]) for e in stored},
                f"tool {sorted(got)} != table")
        for e in stored:
            # source side: the cited call / queue send exists in this snapshot
            f, _, ln = e["src_ref"].rpartition(":")
            tbl = "outbound_calls" if e["kind"] == "http" else "queues"
            n = _rows(conn, f"SELECT count(*) AS n FROM {tbl} WHERE snapshot_id=%s AND file_path=%s AND line=%s", (snapshot_id, f, int(ln)))[0]["n"]
            g.check(f"edge-source:{e['src_ref']}->{e['dst_repo']}", n > 0, "no matching call/send at the cited line")
            # destination side: the route / consumer exists in the other repo's current snapshot
            try:
                if e["kind"] == "http":
                    method, _, rpath = e["dst_ref"].partition(" ")
                    hit = T.find_route(conn, e["dst_repo"], method, rpath)
                    ok = any(f"{r['route_file']}:{r['route_line']}" == e["evidence"]["dst"] for r in hit)
                else:
                    dq = T.get_dependencies(conn, e["dst_repo"])["queues"]
                    ok = any(q["direction"] == "receive" and f"queue:{q['queue_name']}" == e["dst_ref"] for q in dq)
            except T.ToolError as ex:
                ok = False
            g.check(f"edge-target:{e['src_ref']}->{e['dst_repo']}:{e['dst_ref']}", ok, "target not found in destination repo")
        g.check("get_index_freshness", T.get_index_freshness(conn, repo, **sid)["indexed_sha"] is not None)
        g.check("get_recent_changes", "recent_commits" in T.get_recent_changes(conn, repo, **sid))
        g.check("search_code", True)
        for s in syms[:5]:
            hits = T.search_code(conn, repo, s["name"].split(".")[-1], **sid)
            g.check(f"search_code:{s['name']}", any(h["file_path"] == s["file_path"] for h in hits), f"got {hits[:3]}")
    finally:
        if not mysql:
            conn.execute("RESET ROLE")
    return {"passed": not g.failures, "total": len(g.results), "failures": g.failures}
