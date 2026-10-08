import sqlite3
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "harness.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "database", "schema.sql")


def init_db():
    with get_conn() as conn:
        with open(SCHEMA_PATH) as f:
            conn.executescript(f.read())


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
