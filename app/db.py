import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row)


def apply_schema(conn):
    conn.execute((Path(__file__).parent / "schema.sql").read_text())
    conn.commit()
