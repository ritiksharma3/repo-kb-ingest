"""Stage: cross-repo links. Pure matching of one repo's outbound calls / queue sends against the routes and queue
consumers of the other repos' current snapshots. Produces `edges` rows (origin=linker evidence); anything that
does not resolve (external hosts, unknown queues, unverified URLs) simply yields no edge."""
from __future__ import annotations
import re
from urllib.parse import urlsplit

import yaml
from pathlib import Path

from .. import db

ORIGIN = "derived"


def load_hosts(path: Path | None) -> dict[str, str]:
    """Host -> repo mapping from config/mappings.yaml (`hosts:` key)."""
    if not path or not Path(path).exists():
        return {}
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return {str(h).lower(): str(r) for h, r in sorted((data.get("hosts") or {}).items())}


def split_url(url: str | None) -> tuple[str, str] | None:
    """-> (host, path) for an absolute http(s) URL, else None (relative / dynamic URLs are not linked)."""
    if not url:
        return None
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    return u.hostname.lower(), (u.path or "/")


def _segments(path: str) -> list[str]:
    return [s for s in path.split("?")[0].strip("/").split("/") if s != ""]


def _is_param(seg: str) -> bool:
    return seg.startswith(":") or (seg.startswith("{") and seg.endswith("}")) or seg == "{}"


def path_matches(route_path: str, call_path: str) -> bool:
    """Segment-wise match; `:id` / `{}` on either side matches any single segment (params are normalised)."""
    r, c = _segments(route_path), _segments(call_path)
    if len(r) != len(c):
        return False
    return all(a == b or _is_param(a) or _is_param(b) for a, b in zip(r, c))


def method_matches(route_method: str, call_method: str) -> bool:
    return route_method in ("ALL", "USE") or call_method in ("REQUEST", "ALL") or route_method == call_method


def build_context(repos: dict[str, dict]) -> dict:
    """repos: name -> {"routes": [...], "queues": [...]} (other repos' current-snapshot facts), sorted deterministically."""
    return {name: {"routes": sorted(v["routes"], key=lambda r: (r["path"], r["method"], r["route_file"], r["route_line"])),
                   "queues": sorted(v["queues"], key=lambda q: (q["file_path"], q["line"]))}
            for name, v in sorted(repos.items())}


def compute_edges(repo: str, tables: dict, hosts: dict[str, str], context: dict) -> list[dict]:
    edges: dict[tuple, dict] = {}
    for c in sorted(tables["outbound_calls"], key=lambda c: (c["file_path"], c["line"], c["expression"])):
        hp = split_url(c["url"])
        if not hp or c["status"] == "unverified":
            continue
        host, path = hp
        dst = hosts.get(host)
        if dst is None and host in context:
            dst = host
        if dst is None or dst == repo or dst not in context:
            continue
        for r in context[dst]["routes"]:
            if method_matches(r["method"], c["method"]) and path_matches(r["path"], path):
                src_ref = f"{c['file_path']}:{c['line']}"
                dst_ref = f"{r['method']} {r['path']}"
                edges.setdefault((src_ref, dst, dst_ref, "http"), {
                    "src_repo": repo, "src_ref": src_ref, "dst_repo": dst, "dst_ref": dst_ref, "kind": "http",
                    "evidence": {"src": src_ref, "dst": f"{r['route_file']}:{r['route_line']}",
                                 "method": c["method"], "url": c["url"]}, "origin": ORIGIN})
    for q in sorted(tables["queues"], key=lambda q: (q["file_path"], q["line"])):
        if q["direction"] != "send" or not q["queue_name"] or q["status"] == "unverified":
            continue
        for dst, ctx in context.items():
            if dst == repo:
                continue
            for rq in ctx["queues"]:
                if rq["direction"] == "receive" and rq["queue_name"] == q["queue_name"]:
                    src_ref = f"{q['file_path']}:{q['line']}"
                    dst_ref = f"queue:{q['queue_name']}"
                    edges.setdefault((src_ref, dst, dst_ref, "queue"), {
                        "src_repo": repo, "src_ref": src_ref, "dst_repo": dst, "dst_ref": dst_ref, "kind": "queue",
                        "evidence": {"src": src_ref, "dst": f"{rq['file_path']}:{rq['line']}", "queue": q["queue_name"]},
                        "origin": ORIGIN})
    return [edges[k] for k in sorted(edges)]


# ---------------- database side ----------------

def load_context(conn, exclude_repo: str) -> dict:
    """Routes + queue sites of every other repo's current snapshot (latest published snapshot per repo)."""
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        if db.is_mysql():
            cur.execute("SELECT r.name, MAX(s.id) AS id FROM current_snapshots s JOIN repos r ON r.id=s.repo_id "
                        "WHERE r.name<>%s GROUP BY r.name ORDER BY r.name", (exclude_repo,))
        else:
            cur.execute("SELECT DISTINCT ON (r.name) r.name, s.id FROM current_snapshots s JOIN repos r ON r.id=s.repo_id "
                        "WHERE r.name<>%s ORDER BY r.name, s.id DESC", (exclude_repo,))
        snaps = list(cur.fetchall())
        for s in snaps:
            cur.execute("SELECT method, path, route_file, route_line FROM routes WHERE snapshot_id=%s", (s["id"],))
            routes = list(cur.fetchall())
            cur.execute("SELECT file_path, line, direction, queue_name, status FROM queues WHERE snapshot_id=%s", (s["id"],))
            out[s["name"]] = {"routes": routes, "queues": list(cur.fetchall())}
    return build_context(out)


def known_roots(conn) -> dict[str, dict]:
    """repo -> {root, commit, branch} from each repo's latest published ingest run that recorded a root path."""
    with conn.cursor() as cur:
        if db.is_mysql():
            cur.execute("SELECT repo_name, report FROM ingest_runs WHERE status='published' "
                        "AND JSON_CONTAINS_PATH(report, 'one', '$.root') ORDER BY finished_at DESC, started_at DESC")
            runs = {}
            for r in cur.fetchall():
                runs.setdefault(r["repo_name"], r["report"])      # newest first -> keep the first per repo
            cur.execute("SELECT r.name, s.commit_sha, s.branch, s.id FROM current_snapshots s "
                        "JOIN repos r ON r.id=s.repo_id ORDER BY s.id")
            cur_snap = {r["name"]: r for r in cur.fetchall()}     # ascending id -> latest wins
        else:
            cur.execute("SELECT DISTINCT ON (repo_name) repo_name, report FROM ingest_runs WHERE status='published' "
                        "AND report ? 'root' ORDER BY repo_name, finished_at DESC, started_at DESC")
            runs = {r["repo_name"]: r["report"] for r in cur.fetchall()}
            cur.execute("SELECT DISTINCT ON (r.name) r.name, s.commit_sha, s.branch FROM current_snapshots s "
                        "JOIN repos r ON r.id=s.repo_id ORDER BY r.name, s.id DESC")
            cur_snap = {r["name"]: r for r in cur.fetchall()}
    return {n: {"root": rep["root"], "commit": cur_snap[n]["commit_sha"], "branch": cur_snap[n]["branch"]}
            for n, rep in sorted(runs.items()) if n in cur_snap}


def dependents(conn, repo: str) -> list[dict]:
    """Edges pointing INTO `repo`, from the current snapshot of every other repo."""
    with conn.cursor() as cur:
        cur.execute("SELECT e.src_repo, e.src_ref, e.dst_repo, e.dst_ref, e.kind, e.evidence FROM edges e "
                    "JOIN current_snapshots s ON s.id=e.snapshot_id WHERE e.dst_repo=%s AND e.src_repo<>%s "
                    "ORDER BY 1,2,3,4,5", (repo, repo))
        return list(cur.fetchall())


def downstream_for(conn, repo: str, keys: set[str]) -> list[dict]:
    """Dependent repos whose edges target any of the changed route/queue keys (e.g. 'GET /api/orders/:id')."""
    if not keys:
        return []
    return [{"repo": e["src_repo"], "src_ref": e["src_ref"], "dst_ref": e["dst_ref"], "kind": e["kind"]}
            for e in dependents(conn, repo) if e["dst_ref"] in keys]
