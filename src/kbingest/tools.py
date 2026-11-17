"""The Phase 2 retrieval tools. Plain functions over a DB connection (read-only role); the MCP server
wraps them 1:1. Every result carries evidence (file:line) and origin straight from the stored rows."""
from __future__ import annotations
import re

from . import db
from .hashing import sha256


class ToolError(Exception):
    pass


# ---- MySQL-side text search helpers (Postgres uses pg_trgm / tsvector in SQL; MySQL has neither) ----
def kb_tokenize(t: str | None) -> str:
    """Python twin of the Postgres kb_tokenize(): camelCase / snake_case / paths -> lowercase words."""
    return re.sub(r"[_./\\-]+", " ", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", t or "")).lower()


def _trigrams(t: str) -> set[str]:
    out = set()
    for w in re.findall(r"\w+", t.lower()):
        w = "  " + w + " "
        out.update(w[i:i + 3] for i in range(len(w) - 2))
    return out


def similarity(a: str, b: str) -> float:
    """pg_trgm-style trigram similarity (Jaccard of padded word trigrams)."""
    x, y = _trigrams(a), _trigrams(b)
    return len(x & y) / len(x | y) if x and y else 0.0


def _q(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _snap(conn, repo: str, branch: str | None = None, snapshot_id: int | None = None) -> dict:
    if snapshot_id is not None:
        rows = _q(conn, "SELECT s.*, r.name AS repo FROM repo_snapshots s JOIN repos r ON r.id=s.repo_id WHERE s.id=%s",
                  (snapshot_id,))
    elif branch:
        rows = _q(conn, "SELECT s.*, r.name AS repo FROM current_snapshots s JOIN repos r ON r.id=s.repo_id "
                        "WHERE r.name=%s AND s.branch=%s", (repo, branch))
    else:
        rows = _q(conn, "SELECT s.*, r.name AS repo FROM current_snapshots s JOIN repos r ON r.id=s.repo_id "
                        "WHERE r.name=%s ORDER BY s.id DESC", (repo,))
    if not rows:
        raise ToolError(f"no published snapshot for repo {repo!r}")
    return rows[0]


def resolve_repo(conn, query: str) -> list[dict]:
    rows = _q(conn, """SELECT DISTINCT m.log_source, m.source_kind, m.repo_name AS repo, m.environment, m.branch
                       FROM log_source_map m JOIN current_snapshots s ON s.id=m.snapshot_id
                       WHERE m.log_source=%s ORDER BY 1,2""", (query,))
    out = [{**r, "origin": "document"} for r in rows]
    for r in _q(conn, "SELECT r.name, s.branch FROM repos r JOIN current_snapshots s ON s.repo_id=r.id WHERE r.name=%s ORDER BY 2",
                (query,)):
        out.append({"log_source": query, "source_kind": "repo_name", "repo": r["name"], "environment": None,
                    "branch": r["branch"], "origin": "source"})
    return out


def get_repo_overview(conn, repo: str, branch: str | None = None, snapshot_id: int | None = None) -> dict:
    s = _snap(conn, repo, branch, snapshot_id)
    d = _q(conn, "SELECT card, section_index, status FROM repo_dossiers WHERE snapshot_id=%s", (s["id"],))[0]
    return {"repo": s["repo"], "branch": s["branch"], "commit_sha": s["commit_sha"], "card": d["card"],
            "sections": d["section_index"], "coverage": s["coverage"], "output_digest": s["output_digest"],
            "published_at": str(s["created_at"])}


def get_repo_dossier(conn, repo: str, sections: list[str] | None = None, branch: str | None = None,
                     snapshot_id: int | None = None) -> dict:
    s = _snap(conn, repo, branch, snapshot_id)
    rows = _q(conn, "SELECT section_key, content, markdown, evidence, origin, status FROM dossier_sections "
                    "WHERE snapshot_id=%s ORDER BY section_key", (s["id"],))
    if sections:
        rows = [r for r in rows if r["section_key"] in sections]
    return {"repo": s["repo"], "commit_sha": s["commit_sha"], "sections": rows}


def _path_regex(path: str) -> re.Pattern:
    parts = [("[^/]+" if seg.startswith(":") or (seg.startswith("{") and seg.endswith("}")) else re.escape(seg))
             for seg in path.strip("/").split("/")]
    return re.compile("^/" + "/".join(parts) + "/?$")


def find_route(conn, repo: str, method: str, path: str, branch: str | None = None,
               snapshot_id: int | None = None) -> list[dict]:
    s = _snap(conn, repo, branch, snapshot_id)
    rows = _q(conn, "SELECT * FROM routes WHERE snapshot_id=%s AND (method=%s OR method='ALL') ORDER BY path, route_file, route_line",
              (s["id"], method.upper()))
    out = []
    for r in rows:
        if r["path"] == path or _path_regex(r["path"]).match(path) or _path_regex(path).match(r["path"]):
            out.append({"method": r["method"], "path": r["path"], "handler_file": r["handler_file"],
                        "handler_line": r["handler_line"], "handler_symbol": r["handler_symbol"],
                        "route_file": r["route_file"], "route_line": r["route_line"], "middleware": r["middleware"],
                        "status": r["status"], "origin": r["origin"],
                        "evidence": f"{r['route_file']}:{r['route_line']}"})
    return out


def _tpl_regex(template: str) -> re.Pattern:
    return re.compile("^" + ".*?".join(re.escape(p) for p in template.split("{}")) + "$", re.S)


def find_code_for_log(conn, repo: str, text: str, branch: str | None = None, snapshot_id: int | None = None) -> list[dict]:
    s = _snap(conn, repo, branch, snapshot_id)
    rows = _q(conn, "SELECT file_path, line, kind, level, template, enclosing_symbol, origin FROM log_sites WHERE snapshot_id=%s "
                    "ORDER BY file_path, line", (s["id"],))
    exact, tmpl, sub = [], [], []
    for r in rows:
        if r["template"] == text:
            exact.append(r)
        elif "{}" in r["template"] and _tpl_regex(r["template"]).match(text):
            tmpl.append(r)
        elif r["template"].strip() and (r["template"] in text or text in r["template"]):
            sub.append(r)
    out = []
    for rank, group in (("exact", exact), ("template", tmpl), ("substring", sub)):
        for r in group:
            out.append({**r, "match": rank, "evidence": f"{r['file_path']}:{r['line']}"})
    if not out and db.is_mysql():   # fuzzy via Python trigram similarity (same measure as pg_trgm)
        scored = sorted(((similarity(r["template"], text), r) for r in rows), key=lambda x: (-x[0], x[1]["file_path"], x[1]["line"]))
        for sim, r in [x for x in scored if x[0] > 0.3][:10]:
            out.append({**r, "match": f"fuzzy({sim:.2f})", "evidence": f"{r['file_path']}:{r['line']}"})
    elif not out:   # fuzzy via pg_trgm
        for r in _q(conn, "SELECT file_path, line, kind, level, template, enclosing_symbol, origin, similarity(template, %s) AS sim "
                          "FROM log_sites WHERE snapshot_id=%s AND similarity(template, %s) > 0.3 ORDER BY sim DESC, file_path, line LIMIT 10",
                    (text, s["id"], text)):
            sim = r.pop("sim")
            out.append({**r, "match": f"fuzzy({sim:.2f})", "evidence": f"{r['file_path']}:{r['line']}"})
    return out


def search_code(conn, repo: str, query: str, limit: int = 20, branch: str | None = None,
                snapshot_id: int | None = None) -> list[dict]:
    s = _snap(conn, repo, branch, snapshot_id)
    out, seen = [], set()
    if db.is_mysql():
        return _search_code_py(conn, s, query, limit)
    ts = " & ".join(w for w in re.split(r"\W+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", query).lower()) if w)
    if ts:
        for r in _q(conn, "SELECT file_path, 'file' AS kind, file_path AS name, 1 AS line FROM file_cards WHERE snapshot_id=%s "
                          "AND to_tsvector('simple', kb_tokenize(search_text)) @@ to_tsquery('simple', %s) ORDER BY file_path LIMIT %s",
                    (s["id"], ts, limit)):
            out.append({**r, "match": "keyword"}); seen.add((r["file_path"], r["name"]))
        for r in _q(conn, "SELECT file_path, kind, name, line_start AS line FROM symbols WHERE snapshot_id=%s "
                          "AND to_tsvector('simple', kb_tokenize(name)) @@ to_tsquery('simple', %s) ORDER BY file_path, line_start LIMIT %s",
                    (s["id"], ts, limit)):
            if (r["file_path"], r["name"]) not in seen:
                out.append({**r, "match": "identifier"}); seen.add((r["file_path"], r["name"]))
    if len(out) < limit:
        for r in _q(conn, "SELECT file_path, kind, name, line_start AS line, similarity(kb_tokenize(name), kb_tokenize(%s)) AS sim "
                          "FROM symbols WHERE snapshot_id=%s AND similarity(kb_tokenize(name), kb_tokenize(%s)) > 0.25 "
                          "ORDER BY sim DESC, file_path, line_start LIMIT %s", (query, s["id"], query, limit)):
            if (r["file_path"], r["name"]) not in seen:
                r.pop("sim"); out.append({**r, "match": "fuzzy"}); seen.add((r["file_path"], r["name"]))
    return [{**r, "evidence": f"{r['file_path']}:{r['line']}"} for r in out[:limit]]


def _search_code_py(conn, s: dict, query: str, limit: int) -> list[dict]:
    """MySQL: same three tiers as the Postgres query (keyword on file cards, identifier on symbols, fuzzy on symbols),
    computed in Python over the snapshot's rows. Ranking is therefore close to, but not identical with, pg_trgm."""
    words = [w for w in re.split(r"\W+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", query).lower()) if w]
    out, seen = [], set()

    def has_all(text):
        toks = set(re.findall(r"\w+", kb_tokenize(text)))
        return all(w in toks for w in words)
    syms = _q(conn, "SELECT file_path, kind, name, line_start AS line FROM symbols WHERE snapshot_id=%s ORDER BY file_path, line_start",
              (s["id"],))
    if words:
        cards = _q(conn, "SELECT file_path, search_text FROM file_cards WHERE snapshot_id=%s ORDER BY file_path", (s["id"],))
        for r in [c for c in cards if has_all(c["search_text"])][:limit]:
            out.append({"file_path": r["file_path"], "kind": "file", "name": r["file_path"], "line": 1, "match": "keyword"})
            seen.add((r["file_path"], r["file_path"]))
        for r in [x for x in syms if has_all(x["name"])][:limit]:
            if (r["file_path"], r["name"]) not in seen:
                out.append({**r, "match": "identifier"}); seen.add((r["file_path"], r["name"]))
    if len(out) < limit:
        q = kb_tokenize(query)
        scored = sorted(((similarity(kb_tokenize(x["name"]), q), x) for x in syms),
                        key=lambda t: (-t[0], t[1]["file_path"], t[1]["line"]))
        for sim, r in [t for t in scored if t[0] > 0.25][:limit]:
            if (r["file_path"], r["name"]) not in seen:
                out.append({**r, "match": "fuzzy"}); seen.add((r["file_path"], r["name"]))
    return [{**r, "evidence": f"{r['file_path']}:{r['line']}"} for r in out[:limit]]


def get_file_summary(conn, repo: str, path: str, branch: str | None = None, snapshot_id: int | None = None) -> dict:
    s = _snap(conn, repo, branch, snapshot_id)
    rows = _q(conn, "SELECT card, status FROM file_cards WHERE snapshot_id=%s AND file_path=%s", (s["id"], path))
    if not rows:
        raise ToolError(f"no card for {path} at {s['commit_sha'][:12]}")
    tests = _q(conn, "SELECT test_file, how FROM test_map WHERE snapshot_id=%s AND source_file=%s ORDER BY 1", (s["id"], path))
    finds = _q(conn, "SELECT rule_id, line, detail FROM findings WHERE snapshot_id=%s AND file_path=%s ORDER BY line, rule_id",
               (s["id"], path))
    return {"commit_sha": s["commit_sha"], "card": rows[0]["card"], "status": rows[0]["status"], "tests": tests, "findings": finds}


def get_code_context(conn, repo: str, path: str, line: int, branch: str | None = None, snapshot_id: int | None = None) -> dict:
    s = _snap(conn, repo, branch, snapshot_id)
    syms = _q(conn, "SELECT name, kind, line_start, line_end, parent FROM symbols WHERE snapshot_id=%s AND file_path=%s "
                    "AND line_start<=%s AND line_end>=%s ORDER BY (line_end-line_start), line_start", (s["id"], path, line, line))
    f = _q(conn, "SELECT content FROM files WHERE snapshot_id=%s AND path=%s", (s["id"], path))
    if not f or f[0]["content"] is None:
        raise ToolError(f"no stored content for {path}")
    sym = syms[0] if syms else None
    lines = f[0]["content"].split("\n")
    lo, hi = (sym["line_start"], sym["line_end"]) if sym else (max(1, line - 10), min(len(lines), line + 10))
    callers = callees = []
    if sym:
        callers = _q(conn, "SELECT src_file, src_symbol, src_line, kind, status FROM symbol_refs WHERE snapshot_id=%s AND dst_file=%s "
                           "AND dst_symbol=%s AND kind<>'import' ORDER BY src_file, src_line", (s["id"], path, sym["name"]))
        callees = _q(conn, "SELECT dst_file, dst_symbol, src_line, kind, status FROM symbol_refs WHERE snapshot_id=%s AND src_file=%s "
                           "AND src_symbol=%s AND kind<>'import' ORDER BY src_line, dst_symbol", (s["id"], path, sym["name"]))
    return {
        "file": path, "line": line, "symbol": sym, "source": "\n".join(lines[lo - 1:hi]), "source_range": [lo, hi],
        "callers": callers, "callees": callees,
        "tests": _q(conn, "SELECT test_file, how FROM test_map WHERE snapshot_id=%s AND source_file=%s ORDER BY 1", (s["id"], path)),
        "findings": _q(conn, "SELECT rule_id, line, detail FROM findings WHERE snapshot_id=%s AND file_path=%s ORDER BY line, rule_id",
                       (s["id"], path)),
        "conventions": _q(conn, "SELECT pattern, majority, counts, representative_file, representative_line FROM convention_stats "
                                "WHERE snapshot_id=%s ORDER BY pattern", (s["id"],)),
        "evidence": f"{path}:{line}", "commit_sha": s["commit_sha"]}


def get_dependencies(conn, repo: str, path: str | None = None, branch: str | None = None, snapshot_id: int | None = None) -> dict:
    s = _snap(conn, repo, branch, snapshot_id)
    flt, p = ("AND file_path=%s", (path,)) if path else ("", ())
    sid = (s["id"],)
    sw = "LEFT(src_ref, CHAR_LENGTH(%s)) = %s" if db.is_mysql() else "starts_with(src_ref, %s)"
    eflt, ep = (f"AND {sw}", (path + ":",) * sw.count("%s")) if path else ("", ())
    return {
        "edges": _q(conn, f"SELECT src_repo, src_ref, dst_repo, dst_ref, kind, evidence, origin FROM edges WHERE snapshot_id=%s {eflt} ORDER BY 1,2,3,4,5", sid + ep),
        # repos whose current snapshot calls into this repo (incoming edges), whole-repo only
        "dependents": [] if path else _q(
            conn, "SELECT e.src_repo, e.src_ref, e.dst_repo, e.dst_ref, e.kind, e.evidence, e.origin FROM edges e "
                  "JOIN current_snapshots cs ON cs.id=e.snapshot_id WHERE e.dst_repo=%s AND e.src_repo<>%s ORDER BY 1,2,3,4,5",
            (s["repo"], s["repo"])),
        "outbound_calls": _q(conn, f"SELECT file_path, line, client, method, url, expression, status FROM outbound_calls WHERE snapshot_id=%s {flt} ORDER BY file_path, line", sid + p),
        "queues": _q(conn, f"SELECT file_path, line, direction, queue_name, status FROM queues WHERE snapshot_id=%s {flt} ORDER BY file_path, line", sid + p),
        "db_access": _q(conn, f"SELECT file_path, line, operation, schema_name, table_name, procedure, status FROM db_access WHERE snapshot_id=%s {flt} ORDER BY file_path, line, table_name", sid + p),
        "note": "edges = cross-repo links from this snapshot (outgoing); dependents = other repos' current edges into this repo; "
                "unresolved/external calls produce no edge"}


def get_file(conn, repo: str, path: str, branch: str | None = None, snapshot_id: int | None = None) -> dict:
    s = _snap(conn, repo, branch, snapshot_id)
    rows = _q(conn, "SELECT content, content_hash, class, language, coverage_state FROM files WHERE snapshot_id=%s AND path=%s",
              (s["id"], path))
    if not rows or rows[0]["content"] is None:
        raise ToolError(f"no stored content for {path}")
    r = rows[0]
    return {"path": path, "commit_sha": s["commit_sha"], "content": r["content"], "content_hash": r["content_hash"],
            "hash_ok": sha256(r["content"]) == r["content_hash"], "class": r["class"], "language": r["language"]}


def get_recent_changes(conn, repo: str, branch: str | None = None, snapshot_id: int | None = None, limit: int = 20) -> dict:
    s = _snap(conn, repo, branch, snapshot_id)
    git = _q(conn, "SELECT data FROM git_facts WHERE snapshot_id=%s AND kind='repo'", (s["id"],))
    ch = _q(conn, "SELECT section, change, prev_snapshot_id FROM change_log WHERE snapshot_id=%s ORDER BY section", (s["id"],))
    return {"commit_sha": s["commit_sha"], "recent_commits": (git[0]["data"]["recent_commits"] if git else [])[:limit],
            "snapshot_changes": ch}


def get_index_freshness(conn, repo: str, head_sha: str | None = None, branch: str | None = None,
                        snapshot_id: int | None = None) -> dict:
    s = _snap(conn, repo, branch, snapshot_id)
    runs = _q(conn, "SELECT run_id, status, finished_at FROM ingest_runs WHERE repo_name=%s ORDER BY finished_at DESC LIMIT 1", (repo,))
    gaps = _q(conn, "SELECT kind, subject, detail, file_path, line FROM extraction_reports WHERE snapshot_id=%s ORDER BY 1,2,3,4,5", (s["id"],))
    return {"indexed_sha": s["commit_sha"], "head_sha": head_sha,
            "up_to_date": (head_sha == s["commit_sha"]) if head_sha else None,
            "coverage": s["coverage"], "extraction_gaps": gaps,
            "last_run": {**runs[0], "run_id": str(runs[0]["run_id"]), "finished_at": str(runs[0]["finished_at"])} if runs else None}


TOOLS = [resolve_repo, get_repo_overview, get_repo_dossier, find_route, find_code_for_log, search_code,
         get_file_summary, get_code_context, get_dependencies, get_file, get_recent_changes, get_index_freshness]
