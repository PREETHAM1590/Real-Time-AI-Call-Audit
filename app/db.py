import os

import psycopg


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    return psycopg.connect(url)
