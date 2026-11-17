"""Stages 4-8: assemble, verify, write the versioned snapshot, run the retrieval self-test, publish."""
from __future__ import annotations
import json
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import bundle as B
from . import db, selftest
from .dossier import render as R
from .hashing import canon, digest_rows, sha256
from .model import FACT_TABLES, Facts, Inventory
from .pipeline import analyze
from .stages import cards as cards_stage
from .stages import gitfacts, link, verify
from .stages.extract import ParseCache
from .versions import PIPELINE_VERSION, all_versions

DB_FACT_TABLES = FACT_TABLES


@dataclass
class Prepared:
    inv: Inventory
    facts: Facts
    ctxs: dict
    bundle: dict
    cards: list
    modules: list
    rendered: dict
    contents: dict
    coverage: dict
    digest: str = ""
    manifests: list = field(default_factory=list)
    edges: list = field(default_factory=list)


def load_mapping(path: Path | None, repo: str, branch: str) -> dict:
    data = {}
    if path and Path(path).exists():
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    be = data.get("branch_environment") or {}
    return {"suite": (data.get("suites") or {}).get(repo), "environment": be.get(branch),
            "branch_environment": dict(sorted(be.items()))}


def coverage_of(inv: Inventory, facts: Facts) -> dict:
    states = [f.state for f in inv.files.values()]
    from .stages.extract import FRAMEWORK_EXTRACTORS
    gaps = [r for r in facts.rows("extraction_reports") if r["kind"] in ("framework_no_extractor", "extractor_zero_results")]
    return {"files_total": len(states), "summarized": states.count("summarized"), "skipped": states.count("skipped"),
            "failed": states.count("failed"),
            "unaccounted": len(states) - sum(states.count(s) for s in ("summarized", "skipped", "failed")),
            "frameworks_detected": sorted(inv.frameworks),
            "frameworks_with_extractor": sorted(f for f in inv.frameworks if f in FRAMEWORK_EXTRACTORS),
            "extraction_gaps": len(gaps), "redactions": sum(f.redactions for f in inv.files.values())}


_CACHES: dict[str, ParseCache] = {}     # repo -> parse cache (lives as long as the process: trigger server, ingest-all)


def prepare(root: Path, commit: str, repo: str, branch: str, mapping_file: Path | None,
            link_ctx: dict | None = None, hosts: dict | None = None,
            cache: ParseCache | None = None, reusable: dict | None = None) -> Prepared:
    inv, facts, ctxs = analyze(root, commit, cache, reusable)
    mapping = load_mapping(mapping_file, repo, branch)
    files = [{"path": f.path, "class": f.cls, "language": f.language, "state": f.state, "reason": f.reason,
              "content_hash": f.content_hash, "lines": len((f.content or "").split("\n")) if f.content is not None else 0}
             for f in inv.files.values()]
    manifests = B.manifest_rows_from_inventory(inv, mapping, {})
    snapshot = {"repo": repo, "branch": branch, "commit": commit}
    edges = link.compute_edges(repo, facts.tables, hosts or {}, link_ctx) if link_ctx else []
    bundle = B.make(snapshot, mapping, files, manifests, facts.tables, edges)
    contents = {p: f.content for p, f in sorted(inv.files.items()) if f.content is not None and f.state != "failed"}
    contents.update({p: f.content for p, f in inv.files.items() if f.content is not None and p not in contents})
    cov = coverage_of(inv, facts)
    cards, mods = cards_stage.build(bundle, ctxs, contents)
    rendered = R.render_all(bundle, cov)
    prep = Prepared(inv, facts, ctxs, bundle, cards, mods, rendered, contents, cov, manifests=manifests, edges=bundle["edges"])
    prep.digest = digest_rows({
        **bundle["tables"], "edges": bundle["edges"], "files": bundle["files"], "manifests": bundle["manifests"],
        "cards": [json.loads(canon(c)) for c in cards], "modules": [json.loads(canon(m)) for m in mods],
        "sections": [rendered["sections"][k] for k in sorted(rendered["sections"])],
        "markdown": [{"k": k, "m": v} for k, v in sorted(rendered["markdown"].items())],
        "card": [rendered["card"]], "mapping": [mapping]})
    return prep


def bundle_from_db(conn, snapshot_id: int, repo: str, branch: str, commit: str) -> dict:
    t = db.fetch_bundle_tables(conn, snapshot_id, DB_FACT_TABLES + ["manifests", "edges"])
    with conn.cursor() as cur:
        nl = ("char_length(content) - char_length(replace(content, CHAR(10), ''))" if db.is_mysql()
              else "length(content) - length(replace(content, E'\\n', ''))")
        cur.execute("SELECT path, class, language, coverage_state AS state, state_reason AS reason, content_hash, "
                    f"CASE WHEN content IS NULL THEN 0 ELSE {nl} + 1 END AS lines "
                    "FROM files WHERE snapshot_id=%s", (snapshot_id,))
        files = list(cur.fetchall())
    mf = t.pop("manifests")
    edges = t.pop("edges")
    mapping = next(m["data"] for m in mf if m["kind"] == "mapping")
    mf = [m for m in mf]
    return B.make({"repo": repo, "branch": branch, "commit": commit}, mapping, files, mf, t, edges)


def _audit(conn, event: str, payload: dict):
    conn.execute("INSERT INTO audit_log (event_type, payload, prev_hash, row_hash) VALUES (%s, %s, '', '')",
                 (event, db.Jsonb(payload)))


def _record_run(run_id, repo, branch, commit, status, report, started):
    with db.connect("writer") as c:
        c.execute("INSERT INTO ingest_runs (run_id, repo_name, branch, commit_sha, status, report, started_at) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s)", (run_id, repo, branch, commit, status, db.Jsonb(report), started))
        _audit(c, f"ingest_{status}", {"run_id": str(run_id), "repo": repo, "commit": commit, "branch": branch})
        c.commit()


def _change_keys(items: list[dict]) -> set[str]:
    """Route / queue-consumer keys of changed dossier items, in the same notation as edges.dst_ref."""
    keys = set()
    for it in items:
        f = it["fields"]
        if f.get("kind") == "route":
            keys.add(f"{f['method']} {f['path']}")
        elif f.get("kind") == "queue_consumer" and f.get("queue"):
            keys.add(f"queue:{f['queue']}")
    return keys


def _change_log(conn, prev_id: int | None, rendered: dict, repo: str = "") -> list[dict]:
    if prev_id is None:
        return []
    with conn.cursor() as cur:
        cur.execute("SELECT section_key, content FROM dossier_sections WHERE snapshot_id=%s", (prev_id,))
        prev = {r["section_key"]: r["content"] for r in cur.fetchall()}
    out = []
    for k, sec in sorted(rendered["sections"].items()):
        old = {canon(i) for i in (prev.get(k) or {}).get("items", [])}
        new = {canon(i) for i in sec["items"]}
        if old != new:
            changed = _change_keys([json.loads(x) for x in new ^ old]) if k in ("entry_points", "dependencies") else set()
            affected = link.downstream_for(conn, repo, changed)
            out.append({"section": k, "prev_snapshot_id": prev_id, "change": {
                "added": len(new - old), "removed": len(old - new),
                "added_items": sorted(json.loads(x)["evidence"][0] for x in new - old if json.loads(x)["evidence"])[:20],
                "removed_items": sorted(json.loads(x)["evidence"][0] for x in old - new if json.loads(x)["evidence"])[:20],
                "downstream_affected": affected}})
    return out


def write_snapshot(conn, prep: Prepared, repo: str, branch: str, commit: str, run_id) -> int:
    with conn.cursor() as cur:
        if db.is_mysql():     # INSERT IGNORE (not ON DUPLICATE KEY UPDATE): the append-only UPDATE trigger must never fire
            cur.execute("INSERT IGNORE INTO repos (name) VALUES (%s)", (repo,))
        else:
            cur.execute("INSERT INTO repos (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (repo,))
        cur.execute("SELECT id FROM repos WHERE name=%s", (repo,))
        repo_id = cur.fetchone()["id"]
        cur.execute("SELECT id FROM repo_snapshots WHERE repo_id=%s AND branch=%s ORDER BY id DESC LIMIT 1", (repo_id, branch))
        prev = cur.fetchone()
        coverage = {**prep.coverage, "verified": True,
                    "complete": prep.coverage["unaccounted"] == 0 and prep.coverage["extraction_gaps"] == 0}
        cur.execute("INSERT INTO repo_snapshots (repo_id, branch, commit_sha, run_id, pipeline_version, versions, output_digest, "
                    "coverage, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'published')" + ("" if db.is_mysql() else " RETURNING id"),
                    (repo_id, branch, commit, run_id, PIPELINE_VERSION, db.Jsonb(all_versions()), prep.digest, db.Jsonb(coverage)))
        sid = cur.lastrowid if db.is_mysql() else cur.fetchone()["id"]
    common = {"snapshot_id": sid, "run_id": run_id}
    fr = [{"path": f.path, "content": f.content, "content_hash": f.content_hash, "class": f.cls, "language": f.language,
           "coverage_state": f.state, "state_reason": f.reason, "line_hashes": None}
          for f in sorted(prep.inv.files.values(), key=lambda f: f.path)]
    db.insert_rows(conn, "files", fr, **common)
    for t in DB_FACT_TABLES:
        db.insert_rows(conn, t, sorted(prep.facts.rows(t), key=canon), **common)
    db.insert_rows(conn, "edges", prep.edges, **common)
    db.insert_rows(conn, "manifests", [{k: v for k, v in m.items()} for m in prep.manifests], **common)
    db.insert_rows(conn, "file_cards", prep.cards, **common)
    db.insert_rows(conn, "module_summaries", prep.modules, **common)
    rd = prep.rendered
    db.insert_rows(conn, "repo_dossiers", [{"card": rd["card"], "section_index": rd["section_index"], "status": "published"}], **common)
    db.insert_rows(conn, "dossier_sections", [
        {"section_key": k, "content": s, "markdown": rd["markdown"][k], "evidence": s["evidence"], "origin": s["origin"],
         "status": s["status"]} for k, s in sorted(rd["sections"].items())], **common)
    env = prep.bundle["mapping"].get("environment")
    srcs = [("repo_name", repo), ("package", ((prep.bundle["manifests"] and
                                              next((m["data"].get("name") for m in prep.bundle["manifests"] if m["kind"] == "package_json"), None)) or None))]
    for m in prep.bundle["manifests"]:
        if m["kind"] == "pm2":
            srcs += [("pm2", a["name"]) for a in m["data"]["apps"]]
    seen = set()
    lm = []
    for kind, name in srcs:
        if name and (name, kind) not in seen and kind != "repo_name":
            seen.add((name, kind))
            lm.append({"log_source": name, "source_kind": kind, "repo_name": repo, "environment": env, "branch": branch,
                       "origin": "source" if kind == "pm2" else "document"})
    db.insert_rows(conn, "log_source_map", lm, **common)
    cl = _change_log(conn, prev["id"] if prev else None, rd, repo)
    db.insert_rows(conn, "change_log", [{"section": c["section"], "prev_snapshot_id": c["prev_snapshot_id"], "change": c["change"]}
                                        for c in cl], **common)
    return sid


def _previous(conn, repo: str, branch: str) -> dict | None:
    """Latest published snapshot of (repo, branch): id, commit, versions and path -> content_hash."""
    with conn.cursor() as cur:
        cur.execute("SELECT s.id, s.commit_sha, s.versions FROM repo_snapshots s JOIN repos r ON r.id=s.repo_id "
                    "WHERE r.name=%s AND s.branch=%s ORDER BY s.id DESC LIMIT 1", (repo, branch))
        s = cur.fetchone()
        if not s:
            return None
        cur.execute("SELECT path, content_hash FROM files WHERE snapshot_id=%s", (s["id"],))
        return {**s, "hashes": {r["path"]: r["content_hash"] for r in cur.fetchall()}}


def _incremental_plan(prev: dict | None, incremental: bool) -> tuple[dict | None, dict]:
    """-> (reusable path->hash map or None, report). Falls back to a full run when there is no usable baseline."""
    if not incremental:
        return None, {"mode": "full", "reason": "incremental disabled"}
    if prev is None:
        return None, {"mode": "full", "reason": "no previous snapshot"}
    if prev["versions"] != all_versions():
        return None, {"mode": "full", "reason": "pipeline/extractor/template versions changed since previous snapshot"}
    return prev["hashes"], {"mode": "incremental", "previous_snapshot_id": prev["id"], "previous_commit": prev["commit_sha"]}


def ingest(root: Path, ref: str = "HEAD", repo: str | None = None, mapping_file: Path | None = None,
           fixtures_dir: Path | None = None, golden_dir: Path | None = None, *, incremental: bool = True,
           link_after: bool = True) -> dict:
    started = datetime.now(timezone.utc)
    run_id = uuid.uuid4()
    root = Path(root).resolve()
    repo = repo or root.name
    report: dict = {"run_id": str(run_id), "repo": repo, "root": str(root)}
    commit = branch = None
    try:
        commit = gitfacts.resolve_commit(root, ref)
        branch = gitfacts.current_branch(root, commit)
        report.update(commit=commit, branch=branch)
        with db.connect("reader") as rc:
            link_ctx = link.load_context(rc, repo)
            prev = _previous(rc, repo, branch)
        reusable, inc = _incremental_plan(prev, incremental)
        cache = _CACHES.setdefault(repo, ParseCache())
        cache.reset_stats()
        prep = prepare(root, commit, repo, branch, mapping_file, link_ctx, link.load_hosts(mapping_file), cache, reusable)
        if reusable is not None:
            cur = {p: f.content_hash for p, f in prep.inv.files.items()}
            inc.update(changed_files=sorted(p for p in cur if reusable.get(p) != cur[p]),
                       removed_files=sorted(set(reusable) - set(cur)),
                       unchanged_files=sum(1 for p in cur if reusable.get(p) == cur[p]),
                       reused_parses=cache.stats["reused"], parsed=cache.stats["parsed"])
        report["incremental"] = inc
        vres = verify.run(prep, fixtures_dir, golden_dir)
        report["verification"] = vres
        report["coverage"] = prep.coverage
        report["edges"] = len(prep.edges)
        if any(not v["ok"] for v in vres):
            report["status"] = "blocked"
            report["reason"] = "verification failed"
            _record_run(run_id, repo, branch, commit, "blocked", report, started)
            return report
        with db.connect("writer") as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT s.id, s.commit_sha, s.output_digest FROM repo_snapshots s JOIN repos r ON r.id=s.repo_id "
                            "WHERE r.name=%s AND s.branch=%s ORDER BY s.id DESC LIMIT 1", (repo, branch))
                latest = cur.fetchone()      # only the CURRENT snapshot counts: an older identical one would not be current
                same = latest if latest and latest["commit_sha"] == commit and latest["output_digest"] == prep.digest else None
            if same:
                report.update(status="unchanged", snapshot_id=same["id"], output_digest=prep.digest)
                _record_run(run_id, repo, branch, commit, "published", report, started)
                return report
            sid = write_snapshot(conn, prep, repo, branch, commit, run_id)
            st = selftest.run(conn, sid, repo, prep.contents)
            db_bundle = bundle_from_db(conn, sid, repo, branch, commit)
            r2 = R.render_all(db_bundle, prep.coverage)
            if R.fingerprint(r2) != R.fingerprint(prep.rendered):
                st["failures"].append({"gate": "dossier_rerender_from_db", "ok": False, "detail": "differs"})
                st["passed"] = False
            report["selftest"] = {"passed": st["passed"], "total": st["total"] + 1, "failures": st["failures"][:25]}
            if not st["passed"]:
                conn.rollback()
                report.update(status="blocked", reason="retrieval self-test failed")
                _record_run(run_id, repo, branch, commit, "blocked", report, started)
                return report
            _audit(conn, "snapshot_published", {"snapshot_id": sid, "repo": repo, "commit": commit, "digest": prep.digest})
            report["change_log"] = [{"section": c["section"], **c["change"]} for c in
                                    _change_log_rows(conn, sid)]
            conn.commit()
        report.update(status="published", snapshot_id=sid, output_digest=prep.digest)
        if link_after:
            report["relinked"] = relink_others(repo, mapping_file)
        _record_run(run_id, repo, branch, commit, "published", report, started)
        return report
    except Exception as e:  # recorded, never silent
        report.update(status="failed", error=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-1500:])
        try:
            _record_run(run_id, repo, branch, commit, "failed", report, started)
        except Exception:
            pass
        return report


def _change_log_rows(conn, snapshot_id: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT section, change FROM change_log WHERE snapshot_id=%s ORDER BY section", (snapshot_id,))
        return [{"section": r["section"], "change": r["change"]} for r in cur.fetchall()]


def relink_others(skip: str | None, mapping_file: Path | None, incremental: bool = True) -> dict:
    """Re-ingest every other known repo at its current commit so its edges reflect the latest routes/queues.
    A repo whose edge set did not change comes back `unchanged` (identical digest) and writes nothing."""
    with db.connect("reader") as rc:
        roots = link.known_roots(rc)
    out = {}
    for name, info in roots.items():
        if name == skip:
            continue
        if not Path(info["root"]).exists():
            out[name] = "root-missing"
            continue
        r = ingest(Path(info["root"]), info["commit"], name, mapping_file, None, None, incremental=incremental, link_after=False)
        out[name] = r["status"]
    return out
