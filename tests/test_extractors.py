import tempfile
from pathlib import Path

from conftest import FIXTURES, GOLDEN
from kbingest import golden
from kbingest.fixtures import make_repo
from kbingest.pipeline import analyze


def test_golden_fixtures_match():
    assert golden.check(FIXTURES, GOLDEN) == []


def _facts(name):
    with tempfile.TemporaryDirectory() as td:
        c = make_repo(FIXTURES / name, Path(td) / name)
        inv, facts, ctxs = analyze(Path(td) / name, c)
    return inv, facts


def test_routes_resolve_mount_prefix_and_handlers():
    _, f = _facts("orders-api")
    routes = {(r["method"], r["path"]): r for r in f.rows("routes")}
    assert ("GET", "/api/orders/:id") in routes and ("POST", "/api/orders/:id/ship") in routes
    assert routes[("GET", "/api/orders/:id")]["handler_symbol"] == "getOrder"
    assert routes[("GET", "/api/orders/:id")]["middleware"] == ["requireAuth"]
    assert routes[("GET", "/health")]["handler_symbol"] == "<inline>"


def test_sql_tables_and_unverified_string_built_sql():
    _, f = _facts("orders-api")
    reads = {r["table_name"] for r in f.rows("db_access") if r["operation"] == "select"}
    assert {"Orders", "Customers"} <= reads
    assert any(r["operation"] == "insert" and r["table_name"] == "Orders" for r in f.rows("db_access"))
    assert any(r["procedure"] == "usp_AuditOrder" for r in f.rows("db_access"))
    unv = [r for r in f.rows("db_access") if r["status"] == "unverified"]
    assert len(unv) == 1 and "${term}" in unv[0]["expression"]


def test_http_timeout_and_queues():
    _, f = _facts("orders-api")
    by_url = {c["url"]: c for c in f.rows("outbound_calls")}
    assert by_url["https://pricing.internal/api/price"]["has_timeout"] is True
    assert by_url["https://shipping.internal/api/notify"]["has_timeout"] is False
    assert {(q["direction"], q["queue_name"]) for q in f.rows("queues")} == {
        ("send", "shipment-events"), ("receive", "order-updates")}
    assert any(x["rule_id"] == "http-call-no-timeout" for x in f.rows("findings"))


def test_gaps_are_reported_never_silent():
    inv, f = _facts("gaps-api")
    kinds = {(r["kind"], r["subject"]) for r in f.rows("extraction_reports")}
    assert ("framework_no_extractor", "fastify") in kinds
    assert ("extractor_zero_results", "express") in kinds
    assert ("no_grammar", "typescript") in kinds
    assert ("parse_failure", "src/broken.js") in kinds
    assert inv.files["src/broken.js"].state == "failed"
    assert any(r["rule_id"] == "empty-catch" for r in f.rows("findings"))


def test_secrets_redacted_before_storage():
    inv, _ = _facts("orders-api")
    c = inv.files[".env.production"].content
    assert "hunter2" not in c and "DB_PASSWORD=[REDACTED]" in c and c.count("\n") == 2


def test_log_templates_and_tests_map():
    _, f = _facts("orders-api")
    assert "Order {} placed" in {l["template"] for l in f.rows("log_sites")}
    assert {(t["test_file"], t["source_file"]) for t in f.rows("test_map")} == {
        ("tests/orderService.test.js", "src/services/orderService.js")}
