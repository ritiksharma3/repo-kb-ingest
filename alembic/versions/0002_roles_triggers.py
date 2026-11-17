"""Roles, grants, append-only triggers, audit chain, 'current' views."""
import os
from alembic import op

revision = "0002"
down_revision = "0001"

VERSIONED = [
    "repos", "repo_snapshots", "files", "symbols", "symbol_refs", "routes", "outbound_calls",
    "db_access", "queues", "log_sites", "test_map", "findings", "convention_stats", "git_facts",
    "extraction_reports", "file_cards", "module_summaries", "repo_dossiers", "dossier_sections",
    "edges", "log_source_map", "change_log", "ingest_runs", "audit_log",
]
SEQS = ["repos_id_seq", "repo_snapshots_id_seq", "audit_log_id_seq"]


def upgrade():
    wpw = os.environ.get("KB_WRITER_PASSWORD", "kb_writer_dev").replace("'", "''")
    rpw = os.environ.get("KB_READER_PASSWORD", "kb_reader_dev").replace("'", "''")
    op.execute(f"""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='kb_writer') THEN
        CREATE ROLE kb_writer LOGIN PASSWORD '{wpw}'; END IF;
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='kb_reader') THEN
        CREATE ROLE kb_reader LOGIN PASSWORD '{rpw}'; END IF;
    END $$;
    -- writer may SET ROLE kb_reader (used by the in-transaction retrieval self-test)
    GRANT kb_reader TO kb_writer;
    GRANT USAGE ON SCHEMA public TO kb_writer, kb_reader;
    """)

    # Append-only: INSERT/SELECT only for the writer; SELECT only for the reader.
    for t in VERSIONED:
        op.execute(f"GRANT SELECT ON {t} TO kb_reader; GRANT SELECT, INSERT ON {t} TO kb_writer;")
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON {t} FROM kb_writer, kb_reader, PUBLIC;")
    op.execute("""
    CREATE FUNCTION kb_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'table % is append-only: % rejected', TG_TABLE_NAME, TG_OP; END $$;
    """)
    for t in VERSIONED:
        op.execute(f"""
        CREATE TRIGGER {t}_append_only BEFORE UPDATE OR DELETE ON {t}
          FOR EACH ROW EXECUTE FUNCTION kb_reject_mutation();
        CREATE TRIGGER {t}_no_truncate BEFORE TRUNCATE ON {t}
          FOR EACH STATEMENT EXECUTE FUNCTION kb_reject_mutation();
        """)
    for s in SEQS:
        op.execute(f"GRANT USAGE, SELECT ON SEQUENCE {s} TO kb_writer;")

    # Audit chain: each row stores the hash of the previous row (serialized by advisory lock).
    op.execute("""
    CREATE FUNCTION kb_audit_chain() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE prev text;
    BEGIN
      PERFORM pg_advisory_xact_lock(727001);
      SELECT row_hash INTO prev FROM audit_log ORDER BY id DESC LIMIT 1;
      NEW.prev_hash := coalesce(prev, repeat('0', 64));
      NEW.created_at := clock_timestamp();
      NEW.row_hash := encode(digest(
        NEW.prev_hash || '|' || NEW.event_type || '|' || NEW.actor || '|' || NEW.payload::text || '|' ||
        NEW.created_at::text, 'sha256'), 'hex');
      RETURN NEW;
    END $$;
    CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log
      FOR EACH ROW EXECUTE FUNCTION kb_audit_chain();
    -- verification helper: returns first broken id, or NULL when the chain is intact
    CREATE FUNCTION kb_audit_verify() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
    DECLARE r record; prev text := repeat('0', 64);
    BEGIN
      FOR r IN SELECT * FROM audit_log ORDER BY id LOOP
        IF r.prev_hash <> prev OR r.row_hash <> encode(digest(
            r.prev_hash || '|' || r.event_type || '|' || r.actor || '|' || r.payload::text || '|' ||
            r.created_at::text, 'sha256'), 'hex') THEN
          RETURN r.id;
        END IF;
        prev := r.row_hash;
      END LOOP;
      RETURN NULL;
    END $$;
    GRANT EXECUTE ON FUNCTION kb_audit_verify() TO kb_reader, kb_writer;
    """)

    # "Current" = latest published snapshot per (repo, branch).
    op.execute("""
    CREATE VIEW current_snapshots AS
      SELECT DISTINCT ON (repo_id, branch) * FROM repo_snapshots ORDER BY repo_id, branch, id DESC;
    GRANT SELECT ON current_snapshots TO kb_reader, kb_writer;
    """)


def downgrade():
    raise RuntimeError("append-only knowledge base: downgrade is not supported")
