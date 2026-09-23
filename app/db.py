import os

import psycopg


def connect(*, timeout_seconds: int | None = None):
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required")
    if timeout_seconds is None:
        return psycopg.connect(url)
    if type(timeout_seconds) is not int or timeout_seconds < 1:
        raise ValueError("database timeout must be a positive integer")
    return psycopg.connect(url, connect_timeout=timeout_seconds, options=f"-c statement_timeout={timeout_seconds * 1000}")
