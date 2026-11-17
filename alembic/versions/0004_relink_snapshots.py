"""Allow a repo to return to an earlier output at the same commit (cross-repo edges can flip back and forth), and index
incoming-edge lookups. Append-only is untouched: no UPDATE/DELETE path is added; 'current' stays the latest row."""
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade():
    op.execute("""
    ALTER TABLE repo_snapshots DROP CONSTRAINT IF EXISTS repo_snapshots_repo_id_branch_commit_sha_output_digest_key;
    CREATE INDEX repo_snapshots_lookup ON repo_snapshots (repo_id, branch, id DESC);
    CREATE INDEX edges_dst ON edges (dst_repo, dst_ref);
    """)


def downgrade():
    raise RuntimeError("append-only knowledge base: downgrade is not supported")
