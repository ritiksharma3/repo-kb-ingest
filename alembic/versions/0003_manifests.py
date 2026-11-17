"""manifests table: parsed manifest facts + checked-in mapping, so dossiers can be re-rendered from the DB."""
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.execute("""
    CREATE TABLE manifests (
      snapshot_id bigint NOT NULL REFERENCES repo_snapshots(id), run_id uuid NOT NULL,
      kind text NOT NULL, path text NOT NULL, data jsonb NOT NULL, origin text NOT NULL,
      PRIMARY KEY (snapshot_id, kind, path)
    );
    GRANT SELECT ON manifests TO kb_reader;
    GRANT SELECT, INSERT ON manifests TO kb_writer;
    REVOKE UPDATE, DELETE, TRUNCATE ON manifests FROM kb_writer, kb_reader, PUBLIC;
    CREATE TRIGGER manifests_append_only BEFORE UPDATE OR DELETE ON manifests
      FOR EACH ROW EXECUTE FUNCTION kb_reject_mutation();
    CREATE TRIGGER manifests_no_truncate BEFORE TRUNCATE ON manifests
      FOR EACH STATEMENT EXECUTE FUNCTION kb_reject_mutation();
    """)


def downgrade():
    raise RuntimeError("append-only knowledge base: downgrade is not supported")
