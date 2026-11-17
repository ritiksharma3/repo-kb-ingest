from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path

import typer

from . import db, golden, publish

db.load_dotenv()
app = typer.Typer(help="Deterministic repo knowledge-base ingestion (Phase 1)", no_args_is_help=True)
ROOT = Path(os.environ.get("KB_PROJECT_ROOT", Path(__file__).resolve().parents[2]))


@app.command()
def ingest(repo_path: Path, ref: str = "HEAD", name: str = typer.Option(None, help="repo name (default: folder name)"),
           mapping: Path = typer.Option(ROOT / "config" / "mappings.yaml"),
           fixtures: Path = typer.Option(ROOT / "tests" / "fixtures"),
           golden_dir: Path = typer.Option(ROOT / "tests" / "golden"),
           json_out: bool = False,
           full: bool = typer.Option(False, help="disable incremental parse reuse"),
           no_link: bool = typer.Option(False, help="skip re-linking the other repos afterwards")):
    """Analyze one repo at one commit and publish it if verification and the retrieval self-test pass."""
    rep = publish.ingest(repo_path, ref, name, mapping, fixtures, golden_dir, incremental=not full, link_after=not no_link)
    if json_out:
        typer.echo(json.dumps(rep, indent=1, default=str))
    else:
        typer.echo(f"status: {rep['status']}  repo: {rep.get('repo')}  commit: {str(rep.get('commit'))[:12]}")
        cov = rep.get("coverage") or {}
        typer.echo(f"coverage: {cov}")
        for v in rep.get("verification", []):
            typer.echo(f"  verify {'OK ' if v['ok'] else 'FAIL'} {v['check']} {v['problems'] if not v['ok'] else ''}")
        inc = rep.get("incremental") or {}
        if inc:
            typer.echo(f"  mode: {inc['mode']}" + (f" (reused {inc['reused_parses']} parses, {len(inc['changed_files'])} changed files)"
                                                   if inc["mode"] == "incremental" else f" ({inc['reason']})"))
        typer.echo(f"  edges: {rep.get('edges')}  relinked: {rep.get('relinked', {})}")
        st = rep.get("selftest")
        if st:
            typer.echo(f"  selftest: {'PASS' if st['passed'] else 'FAIL'} ({st['total']} gates)")
            for f in st["failures"]:
                typer.echo(f"    {f['gate']}: {f['detail']}")
        if rep.get("error"):
            typer.echo(rep["error"])
    raise typer.Exit(0 if rep["status"] in ("published", "unchanged") else 1)


@app.command("ingest-all")
def ingest_all(repos_dir: Path, mapping: Path = typer.Option(ROOT / "config" / "mappings.yaml"),
               fixtures: Path = typer.Option(ROOT / "tests" / "fixtures"),
               golden_dir: Path = typer.Option(ROOT / "tests" / "golden"),
               workdir: Path = typer.Option(None, help="where fixture folders are turned into git repos (default: temp dir)"),
               skip_golden: bool = False):
    """Ingest every repo under REPOS_DIR, then link them. Folders without a .git (e.g. tests/fixtures) are first
    copied into a fresh git repo under --workdir. Goldens are checked once (first repo), then edges are resolved."""
    from .fixtures import make_repo
    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="kb-all-"))
    work.mkdir(parents=True, exist_ok=True)
    names = sorted(d.name for d in Path(repos_dir).iterdir() if d.is_dir() and not d.name.startswith((".", "_")))
    worst = False
    for i, n in enumerate(names):
        src = Path(repos_dir) / n
        if (src / ".git").exists():
            root = src
        else:
            make_repo(src, work / n)
            root = work / n
        check = not (skip_golden or i)
        rep = publish.ingest(root, "HEAD", n, mapping, fixtures if check else None, golden_dir if check else None,
                             link_after=False)
        extra = "" if rep["status"] in ("published", "unchanged") else f"  {rep.get('reason') or rep.get('error') or ''}"
        typer.echo(f"{n}: {rep['status']}  edges={rep.get('edges')}{extra}")
        worst = worst or rep["status"] not in ("published", "unchanged")
    for n, st in publish.relink_others(None, mapping).items():
        typer.echo(f"link {n}: {st}")
        worst = worst or st not in ("published", "unchanged")
    raise typer.Exit(1 if worst else 0)


@app.command("link")
def link_cmd(mapping: Path = typer.Option(ROOT / "config" / "mappings.yaml")):
    """Re-resolve cross-repo edges for every known repo (a repo whose edges changed gets a new snapshot)."""
    res = publish.relink_others(None, mapping)
    for n, st in res.items():
        typer.echo(f"{n}: {st}")
    raise typer.Exit(0 if all(v in ("published", "unchanged") for v in res.values()) else 1)


@app.command("changes")
def changes(repo: str, json_out: bool = False):
    """Change report for the current snapshot of REPO: changed dossier sections and downstream affected repos."""
    from . import tools
    with db.connect("reader") as c:
        try:
            r = tools.get_recent_changes(c, repo)
        except tools.ToolError as e:
            typer.echo(str(e))
            raise typer.Exit(1)
    if json_out:
        typer.echo(json.dumps(r["snapshot_changes"], indent=1, default=str))
    else:
        typer.echo(f"{repo} @ {r['commit_sha'][:12]}: {len(r['snapshot_changes'])} changed section(s)")
        for ch in r["snapshot_changes"]:
            c_ = ch["change"]
            typer.echo(f"  {ch['section']}: +{c_['added']} -{c_['removed']}")
            for d in c_["downstream_affected"]:
                typer.echo(f"    downstream: {d['repo']} ({d['kind']} {d['src_ref']} -> {d['dst_ref']})")


@app.command("trigger")
def trigger(host: str = "127.0.0.1", port: int = 8765, mapping: Path = typer.Option(ROOT / "config" / "mappings.yaml"),
            repos_dir: Path = typer.Option(None, help="folder holding <repo>/ git repos; else paths of earlier ingests are used")):
    """Local Jenkins-style webhook: POST /trigger {"repo": "...", "sha": "..."} -> ingest + change report (loopback only)."""
    from .trigger import make_server
    srv = make_server(host, port, mapping, repos_dir)
    typer.echo(f"listening on http://{host}:{srv.server_address[1]}/trigger")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


@app.command("golden-update")
def golden_update(fixtures: Path = ROOT / "tests" / "fixtures", golden_dir: Path = ROOT / "tests" / "golden"):
    """Regenerate golden outputs (review the diff before committing)."""
    golden.update(fixtures, golden_dir)
    typer.echo("golden files updated")


@app.command("golden-check")
def golden_check(fixtures: Path = ROOT / "tests" / "fixtures", golden_dir: Path = ROOT / "tests" / "golden"):
    fails = golden.check(fixtures, golden_dir)
    for f in fails:
        typer.echo(f)
    raise typer.Exit(1 if fails else 0)


@app.command("audit-verify")
def audit_verify():
    """Verify the audit hash chain. Prints the latest hash (export it outside the DB on a schedule)."""
    with db.connect("reader") as c:
        bad = c.execute("SELECT kb_audit_verify() AS bad").fetchone()["bad"]
        last = c.execute("SELECT id, row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    typer.echo(f"chain: {'INTACT' if bad is None else f'BROKEN at id {bad}'}  latest: {last}")
    raise typer.Exit(0 if bad is None else 1)


@app.command("init-db")
def init_db(check: bool = typer.Option(False, "--check", help="only report server version / settings, change nothing")):
    """MySQL only (KB_DB=mysql): create the database, schema, append-only triggers and the kb_writer/kb_reader users.
    Postgres uses `alembic upgrade head` instead."""
    if not db.is_mysql():
        typer.echo("init-db is for KB_DB=mysql; for Postgres run: alembic upgrade head")
        raise typer.Exit(2)
    from . import mysql_schema
    try:
        info = mysql_schema.check_server() if check else mysql_schema.init_db()
    except Exception as e:
        typer.echo(f"init-db failed: {type(e).__name__}: {e}")
        raise typer.Exit(1)
    typer.echo(json.dumps(info, indent=1))
    if not check:
        typer.echo(("schema created" if info["created_schema"] else "schema already present")
                   + ("; users/grants applied" if info.get("grants_applied") else "; users/grants NOT applied (see warnings)"))


@app.command("serve-mcp")
def serve_mcp():
    """Run the MCP server (stdio) exposing the retrieval tools."""
    from .mcp_server.server import main
    main()


if __name__ == "__main__":
    app()
