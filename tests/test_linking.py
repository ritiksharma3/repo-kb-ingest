"""Step 5 (cross-repo edges) and step 6 (incremental, change reports, trigger)."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from conftest import FIXTURES, MAPPINGS
from kbingest import db, publish, tools
from kbingest.fixtures import commit_change, make_repo
from kbingest.stages import link
from kbingest.trigger import make_server

OK = ("published", "unchanged")


def ing(path, name, **kw):
    return publish.ingest(path, "HEAD", name, MAPPINGS, None, None, **kw)


def edge_set(repo):
    with db.connect("reader") as c:
        d = tools.get_dependencies(c, repo)
    return {(e["src_ref"], e["dst_repo"], e["dst_ref"], e["kind"]) for e in d["edges"]}


# ---------- pure linker ----------

def test_path_matching_normalises_params():
    assert link.path_matches("/api/orders/:id", "/api/orders/42")
    assert link.path_matches("/api/orders/:id", "/api/orders/{}")
    assert link.path_matches("/api/orders/:id/", "/api/orders/7")
    assert not link.path_matches("/api/orders/:id", "/api/orders")
    assert not link.path_matches("/auth/verify", "/auth/login")


def test_split_url_and_hosts_mapping():
    assert link.split_url("https://login.internal/auth/verify?x=1") == ("login.internal", "/auth/verify")
    assert link.split_url("/relative") is None and link.split_url(None) is None
    hosts = link.load_hosts(MAPPINGS)
    assert hosts["login.internal"] == "login-api" and hosts["orders.internal"] == "orders-api"


def test_compute_edges_unit_unresolved_stay_unlinked():
    ctx = link.build_context({"login-api": {"routes": [{"method": "GET", "path": "/auth/verify", "route_file": "r.js", "route_line": 3}],
                                            "queues": []}})
    calls = [{"file_path": "a.js", "line": 1, "expression": "x", "url": "https://login.internal/auth/verify", "method": "GET", "status": "verified"},
             {"file_path": "a.js", "line": 2, "expression": "x", "url": "https://pricing.internal/api/price", "method": "GET", "status": "verified"},
             {"file_path": "a.js", "line": 3, "expression": "x", "url": None, "method": "GET", "status": "unverified"},
             {"file_path": "a.js", "line": 4, "expression": "x", "url": "https://login.internal/auth/nope", "method": "GET", "status": "verified"}]
    edges = link.compute_edges("orders-api", {"outbound_calls": calls, "queues": []}, {"login.internal": "login-api"}, ctx)
    assert [(e["src_ref"], e["dst_ref"]) for e in edges] == [("a.js:1", "GET /auth/verify")]


# ---------- end to end ----------

@pytest.fixture(scope="module")
def linked(testdb, tmp_path_factory):
    """Ingest order matters on purpose: orders-api first (login-api unknown), so edges only appear via the re-link."""
    base = tmp_path_factory.mktemp("linked")
    roots = {}
    for n in ("orders-api", "login-api", "mock-api"):
        make_repo(FIXTURES / n, base / n)
        roots[n] = base / n
    reports = {}
    for n in ("orders-api", "login-api", "mock-api"):
        reports[n] = ing(roots[n], n)
    return roots, reports


def test_ingest_order_independent_edges_via_relink(linked):
    roots, reports = linked
    assert all(r["status"] in OK for r in reports.values()), reports
    assert reports["orders-api"]["edges"] == 0                      # login-api was unknown at that time
    assert reports["login-api"]["relinked"]["orders-api"] == "published"   # re-linked: new snapshot with the edge
    assert ("src/utils/auth.js:11", "login-api", "GET /auth/verify", "http") in edge_set("orders-api")


def test_edges_mock_api_to_login_and_orders(linked):
    e = edge_set("mock-api")
    assert ("src/jobs/gateway.js:8", "login-api", "GET /auth/verify", "http") in e
    assert ("src/jobs/gateway.js:9", "orders-api", "GET /api/orders/:id", "http") in e
    assert ("src/jobs/gateway.js:15", "orders-api", "queue:order-updates", "queue") in e


def test_external_hosts_are_not_linked(linked):
    refs = {x[1] for x in edge_set("orders-api")}
    assert refs == {"login-api"}                                     # pricing.internal / shipping.internal stay unresolved
    with db.connect("reader") as c:
        d = tools.get_dependencies(c, "orders-api")
    assert any("pricing.internal" in (o["url"] or "") for o in d["outbound_calls"])


def test_get_dependencies_dependents_and_dossier(linked):
    with db.connect("reader") as c:
        d = tools.get_dependencies(c, "login-api")
        assert d["edges"] == [] and {x["src_repo"] for x in d["dependents"]} == {"orders-api", "mock-api"}
        assert "empty until" not in d["note"]
        only = tools.get_dependencies(c, "mock-api", "src/jobs/gateway.js")
        assert len(only["edges"]) == 3
        assert tools.get_dependencies(c, "mock-api", "src/app.js")["edges"] == []
        md = tools.get_repo_dossier(c, "mock-api", ["dependencies"])["sections"][0]
    items = [i for i in md["content"]["items"] if i["fields"]["kind"] == "cross_repo_edge"]
    assert len(items) == 3 and "cross_repo_edge" in md["markdown"] and "login-api" in md["markdown"]


def test_log_source_map_includes_new_repos(linked):
    with db.connect("reader") as c:
        assert tools.resolve_repo(c, "login-api-prod")[0]["repo"] == "login-api"
        assert tools.resolve_repo(c, "mock-api-prod")[0]["repo"] == "mock-api"


def test_selftest_gate_blocks_edge_with_missing_target(linked, monkeypatch, tmp_path):
    p = tmp_path / "mock-api"
    make_repo(FIXTURES / "mock-api", p)
    commit_change(p, "README.md", "# Mock API\n\nchanged\n", "docs", "2024-02-01T00:00:00+00:00")
    orig = link.compute_edges

    def bogus(*a, **k):
        return orig(*a, **k) + [{"src_repo": "mock-api", "src_ref": "src/jobs/gateway.js:8", "dst_repo": "login-api",
                                 "dst_ref": "GET /auth/does-not-exist", "kind": "http",
                                 "evidence": {"dst": "x.js:1"}, "origin": "derived"}]
    monkeypatch.setattr(link, "compute_edges", bogus)
    rep = ing(p, "mock-api", link_after=False)
    assert rep["status"] == "blocked" and "self-test" in rep["reason"]
    assert any(f["gate"].startswith("edge-target") for f in rep["selftest"]["failures"])


def test_incremental_reuses_unchanged_files_and_matches_full_digest(linked, tmp_path):
    p = tmp_path / "inc-api"      # login-api content: no outbound calls, so it never gains edges when re-linked later
    make_repo(FIXTURES / "login-api", p)
    first = ing(p, "inc-api", link_after=False)
    assert first["status"] == "published" and first["incremental"]["mode"] == "full"
    again = ing(p, "inc-api", link_after=False)
    assert again["status"] == "unchanged" and again["incremental"]["changed_files"] == []
    c2 = commit_change(p, "src/services/authService.js",
                       (p / "src/services/authService.js").read_text(encoding="utf-8").replace("'abc'", "'xyz'"),
                       "tweak", "2024-03-01T00:00:00+00:00")
    rep = ing(p, "inc-api", link_after=False)
    inc = rep["incremental"]
    assert rep["status"] == "published" and inc["mode"] == "incremental"
    assert inc["changed_files"] == ["src/services/authService.js"] and inc["unchanged_files"] > 5
    assert inc["parsed"] == 1 and inc["reused_parses"] == 3        # every other JS file came from the cache
    # an incremental run is byte-identical to a cold full run on the same commit
    cold = publish.prepare(p, c2, "inc-api", "main", MAPPINGS)
    assert cold.digest == rep["output_digest"]


def test_incremental_falls_back_to_full_on_version_change(linked, tmp_path, monkeypatch):
    p = tmp_path / "ver-api"
    make_repo(FIXTURES / "login-api", p)
    ing(p, "ver-api", link_after=False)
    commit_change(p, "README.md", "# Login\n\nedited\n", "docs", "2024-03-02T00:00:00+00:00")
    real = publish.all_versions
    monkeypatch.setattr(publish, "all_versions", lambda: {**real(), "templates": "999"})
    rep = ing(p, "ver-api", link_after=False)
    assert rep["status"] == "published" and rep["incremental"]["mode"] == "full" and "versions" in rep["incremental"]["reason"]
    rep2 = publish.ingest(p, "HEAD", "ver-api", MAPPINGS, None, None, incremental=False, link_after=False)
    assert rep2["incremental"]["mode"] == "full" and rep2["status"] == "unchanged"


def test_downstream_affected_populated_on_route_change(linked):
    roots, _ = linked
    login = roots["login-api"]
    path = "src/routes/auth.js"
    orig = (login / path).read_text(encoding="utf-8")
    commit_change(login, path, orig.replace("'/verify'", "'/check'"), "rename verify route", "2024-04-01T00:00:00+00:00")
    rep = ing(login, "login-api")
    assert rep["status"] == "published"
    ep = next(c for c in rep["change_log"] if c["section"] == "entry_points")
    affected = {(d["repo"], d["dst_ref"]) for d in ep["downstream_affected"]}
    assert affected == {("orders-api", "GET /auth/verify"), ("mock-api", "GET /auth/verify")}
    with db.connect("reader") as c:
        ch = tools.get_recent_changes(c, "login-api")["snapshot_changes"]
        assert any(x["section"] == "entry_points" and x["change"]["downstream_affected"] for x in ch)
    # the consumers were re-linked: their stale edge to the removed route is gone
    assert rep["relinked"]["orders-api"] == "published" and rep["relinked"]["mock-api"] == "published"
    assert not any(x[2] == "GET /auth/verify" for x in edge_set("orders-api"))
    # restore the route: edges come back
    commit_change(login, path, orig, "restore", "2024-04-02T00:00:00+00:00")
    ing(login, "login-api")
    assert any(x[2] == "GET /auth/verify" for x in edge_set("orders-api"))


def _post(url, body, raw=False):
    data = body if raw else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_trigger_endpoint_returns_change_report(linked):
    roots, _ = linked
    login = roots["login-api"]
    sha = commit_change(login, "src/services/authService.js",
                        (login / "src/services/authService.js").read_text(encoding="utf-8") + "\n// touched\n",
                        "touch", "2024-05-01T00:00:00+00:00")
    srv = make_server("127.0.0.1", 0, MAPPINGS, login.parent)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        code, body = _post(base + "/trigger", {"repo": "login-api", "sha": sha})
        assert code == 200 and body["status"] == "published" and body["commit"] == sha
        assert body["incremental"]["mode"] == "incremental" and body["incremental"]["changed_files"] == ["src/services/authService.js"]
        assert isinstance(body["changes"], list) and "relinked" in body
        code, body = _post(base + "/trigger", {"repo": "login-api", "sha": sha})
        assert code == 200 and body["status"] == "unchanged"
        assert _post(base + "/trigger", {"repo": "nope-api"})[0] == 404
        assert _post(base + "/trigger", {"repo": "../etc"})[0] == 400
        assert _post(base + "/trigger", b"not json", raw=True)[0] == 400
        assert _post(base + "/other", {})[0] == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_trigger_refuses_non_loopback_bind():
    with pytest.raises(ValueError):
        make_server("0.0.0.0", 0)


def test_audit_chain_intact_after_linking(linked):
    with db.connect("reader") as c:
        assert c.execute("SELECT kb_audit_verify() AS bad").fetchone()["bad"] is None
