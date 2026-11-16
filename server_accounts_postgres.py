"""Optional PostgreSQL adapter for the shared home account store."""
from contextlib import contextmanager
import re


# Serialize mutations and schema initialization as BEGIN IMMEDIATE does on SQLite.
WRITE_LOCK = 0x4B4F4E54


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters=()):
        # Account queries are fixed SQL with positional placeholders.
        return self.connection.execute(statement.replace("?", "%s"), parameters)


class PostgresStore:
    def __init__(self, database_url, schema):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL accounts require psycopg[binary]") from exc
        self.driver = psycopg
        self.integrity_error = psycopg.IntegrityError
        self.database_url = database_url
        self.row_factory = dict_row
        translated = re.sub(r"\bBLOB\b", "BYTEA", schema)
        translated = re.sub(r"\bINTEGER\b", "BIGINT", translated)
        with self.connect(write=True) as db:
            for statement in translated.split(";"):
                if statement.strip():
                    db.execute(statement)

    @contextmanager
    def connect(self, write=False):
        try:
            with self.driver.connect(
                self.database_url, row_factory=self.row_factory, connect_timeout=5,
                options="-c statement_timeout=5000 -c lock_timeout=5000"
            ) as connection:
                if write:
                    connection.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))
                else:
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                yield PostgresConnection(connection)
        except (self.driver.OperationalError, self.driver.errors.LockNotAvailable) as exc:
            raise OSError("Accounts database unavailable") from exc
