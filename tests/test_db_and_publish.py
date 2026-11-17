import os

import psycopg
import pytest

from conftest import FIXTURES, GOLDEN
from kbingest import db, publish, tools


def run(repos, name):
    p, _ = repos[name]
    return publish.ingest(p, "HEAD", name, None, FIXTURES, GOLDEN)


def _counts():
    with db.connect("reader") as c:
        r = c.execute("SELECT coalesce(max(id),0) AS m, count(*) AS n FROM repo_snapshots").fetchone()
        return r["m"], r["n"]


def test_publish_orders_api_and_selftest_passes(testdb, repos):
    rep = run(repos, "orders-api")
    assert rep["status"] == "published", rep
    assert rep["selftest"]["passed"] and rep["selftest"]["total"] > 40
    assert all(v["ok"] for v in rep["verification"])
    assert rep["coverage"]["unaccounted"] == 0


def test_reingest_same_commit_is_identical_digest(testdb, repos):
    run(repos, "orders-api")
    before = _counts()
    second = run(repos, "orders-api")
    assert second["status"] == "unchanged"
    assert _counts() == before


def test_prepare_digest_is_deterministic(repos):
    p, c = repos["orders-api"]
    a = publish.prepare(p, c, "orders-api", "main", None).digest
    b = publish.prepare(p, c, "orders-api", "main", None).digest
    assert a == b


def test_gaps_repo_publishes_with_visible_gaps(testdb, repos):
    rep = run(repos, "gaps-api")
    assert rep["status"] == "published", rep
    assert rep["coverage"]["extraction_gaps"] == 2 and rep["coverage"]["failed"] == 2


def _mysql_append_only():
    import pymysql
    owner = db.connect("owner", autocommit=True)
    for sql in ("DELETE FROM routes", "UPDATE repos SET name='x'"):
        with pytest.raises(pymysql.err.OperationalError) as ei:      # SIGNAL SQLSTATE 45000 -> 1644
            owner.execute(sql)
        assert ei.value.args[0] == 1644
    # TRUNCATE cannot be blocked by a MySQL trigger (it is blocked for kb_writer/kb_reader by lacking DROP)
    if os.environ.get("KB_ROLES_ENFORCED") != "1":
        pytest.skip("owner account has no GRANT OPTION: kb_writer/kb_reader privilege checks not available")
    w = db.connect("writer", autocommit=True)
    for sql in ("UPDATE repos SET name='x'", "DELETE FROM files", "TRUNCATE routes"):
        with pytest.raises(pymysql.err.OperationalError) as ei:
            w.execute(sql)
        assert ei.value.args[0] in (1142, 1644)
    r = db.connect("reader", autocommit=True)
    with pytest.raises(pymysql.err.OperationalError) as ei:
        r.execute("INSERT INTO repos (name) VALUES ('z')")
    assert ei.value.args[0] == 1142
    assert r.execute("SELECT count(*) AS n FROM current_snapshots").fetchone()["n"] >= 1


def test_append_only_and_roles(testdb, repos):
    run(repos, "orders-api")
    if db.is_mysql():
        return _mysql_append_only()
    owner = db.connect("owner", autocommit=True)
    for sql in ("DELETE FROM routes", "UPDATE repos SET name='x'", "TRUNCATE routes"):
        with pytest.raises(psycopg.errors.RaiseException):
            owner.execute(sql)
    w = db.connect("writer", autocommit=True)
    for sql in ("UPDATE repos SET name='x'", "DELETE FROM files"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            w.execute(sql)
    r = db.connect("reader", autocommit=True)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        r.execute("INSERT INTO repos (name) VALUES ('z')")
    assert r.execute("SELECT count(*) AS n FROM current_snapshots").fetchone()["n"] >= 1


def test_audit_chain_intact(testdb, repos):
    run(repos, "orders-api")
    with db.connect("reader") as c:
        assert c.execute("SELECT kb_audit_verify() AS bad").fetchone()["bad"] is None
        assert c.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"] >= 2


def test_failed_selftest_blocks_publish_and_keeps_previous_current(testdb, repos, monkeypatch):
    run(repos, "orders-api")
    before = _counts()
    from kbingest import selftest
    monkeypatch.setattr(selftest, "run", lambda *a, **k: {
        "passed": False, "total": 1, "failures": [{"gate": "x", "ok": False, "detail": "boom"}]})
    orig = publish.prepare

    def bumped(*a, **k):                      # new digest so the 'unchanged' shortcut is not taken
        prep = orig(*a, **k)
        prep.digest = prep.digest[::-1]
        return prep
    monkeypatch.setattr(publish, "prepare", bumped)
    rep = run(repos, "orders-api")
    assert rep["status"] == "blocked" and "self-test" in rep["reason"]
    assert _counts() == before


def test_failed_verification_blocks_publish(testdb, repos, monkeypatch):
    from kbingest.stages import verify
    monkeypatch.setattr(verify, "run", lambda *a, **k: [
        {"check": "x", "ok": False, "problems": ["bad"], "problem_count": 1}])
    rep = run(repos, "gaps-api")
    assert rep["status"] == "blocked" and rep["reason"] == "verification failed"


def test_tools_serve_fix_oriented_queries(testdb, repos):
    run(repos, "orders-api")
    with db.connect("reader") as c:
        r = tools.find_route(c, "orders-api", "GET", "/api/orders/42")
        assert r and r[0]["handler_symbol"] == "getOrder" and r[0]["handler_file"] == "src/controllers/orderController.js"
        logs = tools.find_code_for_log(c, "orders-api", "Order 17 placed")
        assert logs[0]["file_path"] == "src/services/orderService.js" and logs[0]["match"] == "template"
        summ = tools.get_file_summary(c, "orders-api", "src/services/orderService.js")
        assert [t["test_file"] for t in summ["tests"]] == ["tests/orderService.test.js"]
        ctx = tools.get_code_context(c, "orders-api", "src/services/orderService.js", 18)
        assert ctx["symbol"]["name"] == "placeOrder"
        assert any(x["src_symbol"] == "createOrder" for x in ctx["callers"])
        assert {x["dst_symbol"] for x in ctx["callees"]} >= {"insertOrder"}
        assert {x["pattern"] for x in ctx["conventions"]} >= {"error_handling", "logger"}
        f = tools.get_file(c, "orders-api", ".env.production")
        assert "hunter2" not in f["content"] and f["hash_ok"]
        assert tools.resolve_repo(c, "orders-api-prod")[0]["repo"] == "orders-api"
        assert tools.search_code(c, "orders-api", "placeOrder")[0]["file_path"] == "src/services/orderService.js"
        ov = tools.get_repo_overview(c, "orders-api")
        assert ov["card"]["counts"]["routes"] == 4 and ov["coverage"]["complete"] is True
        md = tools.get_repo_dossier(c, "orders-api", ["identity_purpose"])["sections"][0]["markdown"]
        assert "Handles order creation" in md and "document" in md
