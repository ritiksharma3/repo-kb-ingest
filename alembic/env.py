import os
from alembic import context
from sqlalchemy import create_engine

url = os.environ.get("KB_OWNER_URL", "postgresql://kb_owner:kb_owner_dev@localhost:54329/kb")
url = url.replace("postgresql://", "postgresql+psycopg://", 1)
engine = create_engine(url)
with engine.connect() as conn:
    context.configure(connection=conn, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
    conn.commit()
