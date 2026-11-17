"""MySQL 8.x schema (the counterpart of alembic/versions/0001-0004 for Postgres) and `kbingest init-db`.

Design notes
- InnoDB, utf8mb4 with the binary collation utf8mb4_0900_bin (Postgres text comparison is case-sensitive).
- Long text keys cannot be indexed directly (3072-byte key limit), so each fact table's primary key is
  (snapshot_id, key_hash) where key_hash is a STORED generated SHA2 of the natural key columns. The column is
  hidden from query results by db.py.
- Append-only: separate BEFORE UPDATE / BEFORE DELETE triggers per table (SIGNAL 1644) + GRANTs (kb_writer:
  SELECT, INSERT, EXECUTE; kb_reader: SELECT, EXECUTE). TRUNCATE cannot be blocked by a trigger in MySQL; it is
  prevented for kb_writer / kb_reader by their lack of the DROP privilege, but the owner account can still do it.
- Audit hash chain via BEFORE INSERT trigger; kb_audit_verify() is a stored function (same call as Postgres).
"""
from __future__ import annotations
import os
import re

from . import db

P = "snapshot_id BIGINT NOT NULL, run_id CHAR(36) NOT NULL"

# name -> (columns, natural key columns hashed into key_hash | None for tables with a plain key)
FACT_TABLES: dict[str, tuple[str, list[str]]] = {
    "files": ("path TEXT NOT NULL, content LONGTEXT, content_hash VARCHAR(128) NOT NULL, class VARCHAR(64) NOT NULL, "
              "language VARCHAR(64), coverage_state VARCHAR(16) NOT NULL, state_reason TEXT, line_hashes JSON, "
              "CHECK (coverage_state IN ('summarized','skipped','failed'))", ["path"]),
    "symbols": ("file_path TEXT NOT NULL, name TEXT NOT NULL, kind VARCHAR(64) NOT NULL, line_start INT NOT NULL, "
                "line_end INT NOT NULL, exported BOOLEAN NOT NULL, parent TEXT, line_hash VARCHAR(64) NOT NULL, "
                "origin VARCHAR(32) NOT NULL, extractor VARCHAR(128) NOT NULL", ["file_path", "name", "line_start"]),
    "symbol_refs": ("src_file TEXT NOT NULL, src_symbol TEXT NOT NULL, src_line INT NOT NULL, dst_file TEXT, "
                    "dst_symbol TEXT NOT NULL, kind VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL, "
                    "line_hash VARCHAR(64) NOT NULL, origin VARCHAR(32) NOT NULL, extractor VARCHAR(128) NOT NULL",
                    ["src_file", "src_line", "dst_symbol", "kind"]),
    "routes": ("method VARCHAR(16) NOT NULL, path TEXT NOT NULL, route_file TEXT NOT NULL, route_line INT NOT NULL, "
               "handler_file TEXT, handler_symbol TEXT, handler_line INT, middleware JSON NOT NULL, "
               "framework VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL, expression LONGTEXT, "
               "line_hash VARCHAR(64) NOT NULL, origin VARCHAR(32) NOT NULL, extractor VARCHAR(128) NOT NULL",
               ["method", "path", "route_file", "route_line"]),
    "outbound_calls": ("file_path TEXT NOT NULL, line INT NOT NULL, client VARCHAR(128) NOT NULL, method VARCHAR(32), "
                       "url TEXT, expression LONGTEXT NOT NULL, status VARCHAR(32) NOT NULL, has_timeout BOOLEAN NOT NULL, "
                       "enclosing_symbol TEXT, line_hash VARCHAR(64) NOT NULL, origin VARCHAR(32) NOT NULL, "
                       "extractor VARCHAR(128) NOT NULL", ["file_path", "line", "expression"]),
    "db_access": ("file_path TEXT NOT NULL, line INT NOT NULL, library VARCHAR(128) NOT NULL, operation VARCHAR(64) NOT NULL, "
                  "schema_name VARCHAR(512) NOT NULL DEFAULT '', table_name VARCHAR(512) NOT NULL DEFAULT '', "
                  "`procedure` VARCHAR(512) NOT NULL DEFAULT '', status VARCHAR(32) NOT NULL, expression LONGTEXT NOT NULL, "
                  "enclosing_symbol TEXT, line_hash VARCHAR(64) NOT NULL, origin VARCHAR(32) NOT NULL, "
                  "extractor VARCHAR(128) NOT NULL",
                  ["file_path", "line", "operation", "table_name", "procedure", "expression"]),
    "queues": ("file_path TEXT NOT NULL, line INT NOT NULL, direction VARCHAR(16) NOT NULL, queue_name TEXT, "
               "library VARCHAR(128) NOT NULL, status VARCHAR(32) NOT NULL, expression LONGTEXT NOT NULL, "
               "enclosing_symbol TEXT, line_hash VARCHAR(64) NOT NULL, origin VARCHAR(32) NOT NULL, "
               "extractor VARCHAR(128) NOT NULL", ["file_path", "line", "direction"]),
    "log_sites": ("file_path TEXT NOT NULL, line INT NOT NULL, kind VARCHAR(64) NOT NULL, level VARCHAR(32), "
                  "template LONGTEXT NOT NULL, enclosing_symbol TEXT, line_hash VARCHAR(64) NOT NULL, "
                  "origin VARCHAR(32) NOT NULL, extractor VARCHAR(128) NOT NULL", ["file_path", "line", "template"]),
    "test_map": ("test_file TEXT NOT NULL, source_file TEXT NOT NULL, how VARCHAR(64) NOT NULL, "
                 "origin VARCHAR(32) NOT NULL, extractor VARCHAR(128) NOT NULL", ["test_file", "source_file"]),
    "findings": ("file_path TEXT NOT NULL, line INT NOT NULL, rule_id VARCHAR(128) NOT NULL, detail LONGTEXT NOT NULL, "
                 "enclosing_symbol TEXT, line_hash VARCHAR(64) NOT NULL, origin VARCHAR(32) NOT NULL, "
                 "extractor VARCHAR(128) NOT NULL", ["file_path", "line", "rule_id"]),
    "convention_stats": ("pattern VARCHAR(128) NOT NULL, majority TEXT, counts JSON NOT NULL, representative_file TEXT, "
                         "representative_line INT, outliers JSON NOT NULL, origin VARCHAR(32) NOT NULL, "
                         "extractor VARCHAR(128) NOT NULL", ["pattern"]),
    "git_facts": ("kind VARCHAR(16) NOT NULL, file_path TEXT NOT NULL, data JSON NOT NULL, origin VARCHAR(32) NOT NULL, "
                  "CHECK (kind IN ('file','repo'))", ["kind", "file_path"]),
    "extraction_reports": ("kind VARCHAR(64) NOT NULL, subject TEXT NOT NULL, detail LONGTEXT NOT NULL, "
                           "file_path VARCHAR(1024) NOT NULL DEFAULT '', line INT NOT NULL DEFAULT 0",
                           ["kind", "subject", "file_path", "line"]),
    "file_cards": ("file_path TEXT NOT NULL, card JSON NOT NULL, status VARCHAR(32) NOT NULL, search_text LONGTEXT NOT NULL",
                   ["file_path"]),
    "module_summaries": ("module_path TEXT NOT NULL, summary JSON NOT NULL", ["module_path"]),
    "dossier_sections": ("section_key VARCHAR(128) NOT NULL, content JSON NOT NULL, markdown LONGTEXT NOT NULL, "
                         "evidence JSON NOT NULL, origin VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL",
                         ["section_key"]),
    "edges": ("src_repo VARCHAR(255) NOT NULL, src_ref TEXT NOT NULL, dst_repo VARCHAR(255) NOT NULL, dst_ref TEXT NOT NULL, "
              "kind VARCHAR(32) NOT NULL, evidence JSON NOT NULL, origin VARCHAR(32) NOT NULL",
              ["src_ref", "dst_repo", "dst_ref", "kind"]),
    "log_source_map": ("log_source VARCHAR(512) NOT NULL, source_kind VARCHAR(64) NOT NULL, repo_name VARCHAR(255) NOT NULL, "
                       "environment VARCHAR(128), branch VARCHAR(255), origin VARCHAR(32) NOT NULL",
                       ["log_source", "source_kind"]),
    "change_log": ("prev_snapshot_id BIGINT, section VARCHAR(128) NOT NULL, `change` JSON NOT NULL", ["section"]),
    "manifests": ("kind VARCHAR(64) NOT NULL, path TEXT NOT NULL, data JSON NOT NULL, origin VARCHAR(32) NOT NULL",
                  ["kind", "path"]),
}
OTHER_VERSIONED = ["repos", "repo_snapshots", "repo_dossiers", "ingest_runs", "audit_log"]
VERSIONED = OTHER_VERSIONED + list(FACT_TABLES)
TBL = "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin"
FK = "FOREIGN KEY (snapshot_id) REFERENCES repo_snapshots(id)"


def statements() -> list[str]:
    s = [
        f"CREATE TABLE repos (id BIGINT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE) {TBL}",
        f"""CREATE TABLE repo_snapshots (
              id BIGINT AUTO_INCREMENT PRIMARY KEY, repo_id BIGINT NOT NULL, branch VARCHAR(255) NOT NULL,
              commit_sha VARCHAR(64) NOT NULL, run_id CHAR(36) NOT NULL, pipeline_version VARCHAR(64) NOT NULL,
              versions JSON NOT NULL, output_digest VARCHAR(64) NOT NULL, coverage JSON NOT NULL,
              status VARCHAR(16) NOT NULL, created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
              CHECK (status = 'published'), FOREIGN KEY (repo_id) REFERENCES repos(id),
              INDEX repo_snapshots_lookup (repo_id, branch, id)) {TBL}""",
        f"""CREATE TABLE repo_dossiers ({P}, card JSON NOT NULL, section_index JSON NOT NULL, status VARCHAR(32) NOT NULL,
              PRIMARY KEY (snapshot_id), {FK}) {TBL}""",
        f"""CREATE TABLE ingest_runs (run_id CHAR(36) PRIMARY KEY, repo_name VARCHAR(255) NOT NULL, branch VARCHAR(255),
              commit_sha VARCHAR(64), status VARCHAR(16) NOT NULL, report JSON NOT NULL,
              started_at DATETIME(6) NOT NULL, finished_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
              CHECK (status IN ('published','blocked','failed')), INDEX ingest_runs_repo (repo_name, finished_at)) {TBL}""",
        f"""CREATE TABLE audit_log (id BIGINT AUTO_INCREMENT PRIMARY KEY, event_type VARCHAR(128) NOT NULL,
              actor VARCHAR(128) NOT NULL DEFAULT '', payload JSON NOT NULL, prev_hash VARCHAR(64) NOT NULL,
              row_hash VARCHAR(64) NOT NULL, created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)) {TBL}""",
    ]
    for name, (cols, key) in FACT_TABLES.items():
        # CHECK constraints (if any) are the trailing part of `cols`; keep them after the generated column
        m = re.search(r",\s*(CHECK \(.*\))\s*$", cols)
        check = ", " + m.group(1) if m else ""
        cols = cols[:m.start()] if m else cols
        extra = ""
        if name == "edges":
            extra = ", INDEX edges_dst (dst_repo, dst_ref(191))"
        k = ", ".join(f"`{c}`" for c in key)
        s.append(f"""CREATE TABLE {name} ({P}, {cols},
              key_hash CHAR(64) GENERATED ALWAYS AS (SHA2(CONCAT_WS(CHAR(31), {k}), 256)) STORED NOT NULL,
              PRIMARY KEY (snapshot_id, key_hash), {FK}{check}{extra}) {TBL}""")
    s.append("""CREATE VIEW current_snapshots AS
        SELECT s.* FROM repo_snapshots s
        JOIN (SELECT repo_id, branch, MAX(id) AS mid FROM repo_snapshots GROUP BY repo_id, branch) m ON s.id = m.mid""")
    for t in VERSIONED:
        s.append(f"CREATE TRIGGER {t}_no_update BEFORE UPDATE ON {t} FOR EACH ROW "
                 f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'table {t} is append-only: UPDATE rejected'")
        s.append(f"CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t} FOR EACH ROW "
                 f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'table {t} is append-only: DELETE rejected'")
    # audit chain: each row stores the hash of the previous row. MySQL forbids a locking read of the table being
    # inserted into from its own trigger (error 1442) and GET_LOCK would stay held until the session ends, so
    # concurrent audit writers are not serialized here (single-writer pipeline; audit-verify would flag a fork).
    s.append("""CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log FOR EACH ROW
        BEGIN
          DECLARE prev VARCHAR(64) DEFAULT NULL;
          DECLARE CONTINUE HANDLER FOR NOT FOUND SET prev = NULL;
          SELECT row_hash INTO prev FROM audit_log ORDER BY id DESC LIMIT 1;
          SET NEW.prev_hash = COALESCE(prev, REPEAT('0', 64));
          SET NEW.created_at = NOW(6);
          SET NEW.actor = SUBSTRING_INDEX(USER(), '@', 1);
          SET NEW.row_hash = SHA2(CONCAT(NEW.prev_hash, '|', NEW.event_type, '|', NEW.actor, '|',
                                         CAST(NEW.payload AS CHAR), '|', CAST(NEW.created_at AS CHAR)), 256);
        END""")
    s.append("""CREATE FUNCTION kb_audit_verify() RETURNS BIGINT READS SQL DATA
        BEGIN
          DECLARE bad BIGINT DEFAULT NULL;
          SELECT MIN(id) INTO bad FROM (
            SELECT id, row_hash, prev_hash,
                   SHA2(CONCAT(prev_hash, '|', event_type, '|', actor, '|', CAST(payload AS CHAR), '|',
                               CAST(created_at AS CHAR)), 256) AS calc,
                   LAG(row_hash) OVER (ORDER BY id) AS lagh
            FROM audit_log) t
          WHERE row_hash <> calc OR prev_hash <> COALESCE(lagh, REPEAT('0', 64));
          RETURN bad;
        END""")
    return s


def _ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
        raise ValueError(f"unsafe database name {name!r}")
    return name


def check_server(owner_url: str | None = None) -> dict:
    """Read-only preflight: version, packet size, binlog/trigger settings."""
    with db.mysql_connect(owner_url or db.url("owner"), autocommit=True, database=False) as c:
        v = {r["Variable_name"]: r["Value"] for r in c.execute(
            "SHOW VARIABLES WHERE Variable_name IN ('version','max_allowed_packet','log_bin',"
            "'log_bin_trust_function_creators','lower_case_table_names','sql_mode')").fetchall()}
    info = {"version": v["version"], "max_allowed_packet": int(v["max_allowed_packet"]), "log_bin": v["log_bin"],
            "log_bin_trust_function_creators": v["log_bin_trust_function_creators"], "warnings": []}
    if info["max_allowed_packet"] < 16 * 1024 * 1024:
        info["warnings"].append("max_allowed_packet < 16MB: large files may fail to insert; "
                                "run SET GLOBAL max_allowed_packet=67108864")
    if v["log_bin"] == "ON" and v["log_bin_trust_function_creators"] != "ON":
        info["warnings"].append("binary logging is on and log_bin_trust_function_creators=0: creating triggers/functions "
                                "needs SUPER or SET GLOBAL log_bin_trust_function_creators=1")
    return info


def init_db(owner_url: str | None = None) -> dict:
    """Create the database, schema, triggers, function, view and the kb_writer / kb_reader users. Idempotent."""
    owner_url = owner_url or db.url("owner")
    dbname = _ident(db.parse_mysql_url(owner_url)["database"] or "kb")
    out = {"database": dbname, "created_schema": False}
    out.update(check_server(owner_url))
    wpw = os.environ.get("KB_WRITER_PASSWORD", "kb_writer_dev")
    rpw = os.environ.get("KB_READER_PASSWORD", "kb_reader_dev")
    with db.mysql_connect(owner_url, autocommit=True, database=False) as c:
        c.execute(f"CREATE DATABASE IF NOT EXISTS `{dbname}` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin")
        c.execute(f"USE `{dbname}`")
        have = c.execute("SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_schema=%s AND table_name='repos'",
                         (dbname,)).fetchone()["n"]
        if not have:
            for stmt in statements():
                c.execute(stmt)
            out["created_schema"] = True
        out["grants_applied"] = True
        for host in ("localhost", "127.0.0.1"):
            for user, pw, privs in (("kb_writer", wpw, "SELECT, INSERT, EXECUTE"), ("kb_reader", rpw, "SELECT, EXECUTE")):
                if not out["grants_applied"]:
                    break
                try:
                    c.execute("CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s", (user, host, pw))
                    c.execute(f"GRANT {privs} ON `{dbname}`.* TO %s@%s", (user, host))
                except Exception as e:          # e.g. 1044/1227: the owner account has no GRANT OPTION
                    out["grants_applied"] = False
                    out.setdefault("warnings", []).append(
                        f"could not create/grant {user}@{host} ({type(e).__name__}: {e}). The owner account needs GRANT "
                        "OPTION (phpMyAdmin > User accounts > root > Edit privileges > tick 'Grant'). Until then point "
                        "KB_WRITER_URL and KB_READER_URL at the owner account: the append-only triggers still apply, "
                        "but the SELECT-only reader / INSERT-only writer privilege split is not enforced.")
    return out
