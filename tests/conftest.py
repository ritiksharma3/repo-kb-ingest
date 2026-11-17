import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
GOLDEN = ROOT / "tests" / "golden"
MAPPINGS = ROOT / "config" / "mappings.yaml"
ADMIN = os.environ.get("KB_ADMIN_URL", "postgresql://kb_owner:kb_owner_dev@localhost:54329/postgres")


def _swap_db(url: str, db: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + db


MYSQL_ADMIN = os.environ.get("KB_ADMIN_URL_MYSQL", "mysql://root:@127.0.0.1:3306/mysql")


def _mysql_testdb():
    """Throwaway MySQL database created with `init_db`; env URLs point at it for the whole session. If the owner
    account lacks GRANT OPTION, writer/reader fall back to the owner account (roles_enforced -> False)."""
    from kbingest import db, mysql_schema
    name = "kbtest_" + uuid.uuid4().hex[:8]
    owner = _swap_db(MYSQL_ADMIN, name)
    try:
        info = mysql_schema.init_db(owner)
    except Exception as e:
        pytest.skip(f"mysql not reachable/usable: {e}")
    roles = info["grants_applied"]
    env = {"KB_OWNER_URL": owner,
           "KB_WRITER_URL": _swap_db("mysql://kb_writer:kb_writer_dev@127.0.0.1:3306/x", name) if roles else owner,
           "KB_READER_URL": _swap_db("mysql://kb_reader:kb_reader_dev@127.0.0.1:3306/x", name) if roles else owner}
    keys = tuple(env) + ("KB_DB", "KB_ROLES_ENFORCED")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update(env)
    os.environ["KB_DB"] = "mysql"
    os.environ["KB_ROLES_ENFORCED"] = "1" if roles else "0"
    yield name
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    with db.mysql_connect(MYSQL_ADMIN, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name}")


@pytest.fixture(scope="session")
def testdb():
    """Throwaway database (Postgres: migrated with Alembic; KB_DB=mysql: created by init_db)."""
    if os.environ.get("KB_DB", "postgres").lower() == "mysql":
        yield from _mysql_testdb()
        return
    name = "kbtest_" + uuid.uuid4().hex[:8]
    try:
        admin = psycopg.connect(ADMIN, autocommit=True)
    except Exception as e:
        pytest.skip(f"postgres not reachable: {e}")
    admin.execute(f"CREATE DATABASE {name}")
    env = {**os.environ,
           "KB_OWNER_URL": _swap_db(ADMIN, name),
           "KB_WRITER_URL": _swap_db("postgresql://kb_writer:kb_writer_dev@localhost:54329/x", name),
           "KB_READER_URL": _swap_db("postgresql://kb_reader:kb_reader_dev@localhost:54329/x", name)}
    r = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    keys = ("KB_OWNER_URL", "KB_WRITER_URL", "KB_READER_URL")
    old = {k: os.environ.get(k) for k in keys}
    os.environ.update({k: env[k] for k in keys})
    yield name
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
    admin.close()


@pytest.fixture(scope="session")
def repos(tmp_path_factory):
    from kbingest.fixtures import make_repo
    base = tmp_path_factory.mktemp("repos")
    out = {}
    for n in ("orders-api", "gaps-api", "login-api", "mock-api"):
        out[n] = (base / n, make_repo(FIXTURES / n, base / n))
    return out
