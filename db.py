"""PostgreSQL access layer.

One connection pool for the process. Every caller works through a context
manager so a transaction either commits or rolls back; there is no path that
leaves a money operation half applied.

Row locking is done with SELECT ... FOR UPDATE, which is what makes the
inventory race correct under real concurrency rather than only in a demo.
"""

import logging
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

import config

log = logging.getLogger("db")

_pool = None


def pool():
    global _pool
    if _pool is None:
        if not config.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Point it at PostgreSQL "
                "(Supabase, RDS, Neon or a local instance).")
        _pool = ConnectionPool(
            conninfo=config.DATABASE_URL,
            min_size=config.DB_POOL_MIN,
            max_size=config.DB_POOL_MAX,
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


@contextmanager
def transaction():
    """Run a unit of work. Commits on success, rolls back on any exception."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


@contextmanager
def read():
    """Read-only work. Still pooled, no explicit transaction needed."""
    with pool().connection() as conn:
        yield conn


def query(sql, params=None):
    with read() as conn:
        return conn.execute(sql, params or ()).fetchall()


def query_one(sql, params=None):
    with read() as conn:
        return conn.execute(sql, params or ()).fetchone()


def execute(sql, params=None):
    with transaction() as conn:
        cur = conn.execute(sql, params or ())
        return cur.rowcount


def migrate():
    """Apply every .sql file in migrations/ in filename order.

    Deliberately simple: the files are written to be safe to re-run, so the
    tracking table records what ran without needing rollback support.
    """
    from pathlib import Path

    migrations_dir = Path(__file__).parent / "migrations"
    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        log.warning("no migration files found in %s", migrations_dir)
        return []

    applied = []
    with transaction() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename   TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        done = {r["filename"] for r in
                conn.execute("SELECT filename FROM schema_migrations").fetchall()}

        for path in files:
            if path.name in done:
                continue
            log.info("applying migration %s", path.name)
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)",
                (path.name,))
            applied.append(path.name)

    return applied


def healthy():
    try:
        with read() as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.Error as exc:
        log.error("database health check failed: %s", exc)
        return False


def close():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
