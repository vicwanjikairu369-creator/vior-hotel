import os
import sqlite3
import threading

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

_initialized = False
_initialization_lock = threading.Lock()

_SQLITE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fullname TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        dob TEXT,
        gender TEXT,
        customer_type TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT NOT NULL UNIQUE,
        customer_name TEXT,
        customer_phone TEXT,
        items TEXT,
        order_type TEXT,
        delivery_address TEXT,
        delivery_postal_code TEXT,
        address_verified INTEGER NOT NULL DEFAULT 0,
        destination_latitude REAL,
        destination_longitude REAL,
        delivery_time TEXT,
        amount REAL,
        status TEXT NOT NULL DEFAULT 'Pending',
        status_updated_at TEXT,
        checkout_request_id TEXT,
        driver_latitude REAL,
        driver_longitude REAL,
        driver_accuracy REAL,
        driver_updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        review TEXT NOT NULL
    )
    """,
)

_POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        fullname TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        dob TEXT,
        gender TEXT,
        customer_type TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        order_number TEXT NOT NULL UNIQUE,
        customer_name TEXT,
        customer_phone TEXT,
        items TEXT,
        order_type TEXT,
        delivery_address TEXT,
        delivery_postal_code TEXT,
        address_verified BOOLEAN NOT NULL DEFAULT FALSE,
        destination_latitude DOUBLE PRECISION,
        destination_longitude DOUBLE PRECISION,
        delivery_time TEXT,
        amount DOUBLE PRECISION,
        status TEXT NOT NULL DEFAULT 'Pending',
        status_updated_at TEXT,
        checkout_request_id TEXT,
        driver_latitude DOUBLE PRECISION,
        driver_longitude DOUBLE PRECISION,
        driver_accuracy DOUBLE PRECISION,
        driver_updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        review TEXT NOT NULL
    )
    """,
)

_MIGRATIONS = {
    "users": {
        "fullname": "TEXT",
        "phone": "TEXT",
        "dob": "TEXT",
        "gender": "TEXT",
        "customer_type": "TEXT",
    },
    "orders": {
        "order_number": "TEXT",
        "customer_name": "TEXT",
        "customer_phone": "TEXT",
        "items": "TEXT",
        "order_type": "TEXT",
        "delivery_address": "TEXT",
        "delivery_postal_code": "TEXT",
        "address_verified": "BOOLEAN NOT NULL DEFAULT FALSE",
        "destination_latitude": "DOUBLE PRECISION",
        "destination_longitude": "DOUBLE PRECISION",
        "delivery_time": "TEXT",
        "amount": "DOUBLE PRECISION",
        "status": "TEXT DEFAULT 'Pending'",
        "status_updated_at": "TEXT",
        "checkout_request_id": "TEXT",
        "driver_latitude": "DOUBLE PRECISION",
        "driver_longitude": "DOUBLE PRECISION",
        "driver_accuracy": "DOUBLE PRECISION",
        "driver_updated_at": "TEXT",
    },
    "reviews": {
        "name": "TEXT",
        "review": "TEXT",
    },
}


def _connection_is_sqlite(connection):
    return isinstance(connection, sqlite3.Connection)


def _create_or_migrate_schema(connection):
    sqlite = _connection_is_sqlite(connection)
    schemas = _SQLITE_SCHEMA if sqlite else _POSTGRES_SCHEMA
    cursor = connection.cursor()
    try:
        for statement in schemas:
            cursor.execute(statement)

        for table, columns in _MIGRATIONS.items():
            if sqlite:
                cursor.execute(f"PRAGMA table_info({table})")
                existing = {row[1] for row in cursor.fetchall()}
            else:
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = %s",
                    (table,),
                )
                existing = {row[0] for row in cursor.fetchall()}
            for column, column_type in columns.items():
                if column not in existing:
                    sqlite_type = column_type.replace("BOOLEAN NOT NULL DEFAULT FALSE", "INTEGER NOT NULL DEFAULT 0")
                    sqlite_type = sqlite_type.replace("DOUBLE PRECISION", "REAL")
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def get_db_connection():
    global _initialized
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        if database_url.startswith("postgres://"):
            database_url = database_url.replace("postgres://", "postgresql://", 1)
        connection = psycopg2.connect(database_url)
    else:
        database_path = os.getenv("DATABASE_PATH", "vior_hotel.db")
        connection = sqlite3.connect(database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")

    try:
        if not _initialized:
            with _initialization_lock:
                if not _initialized:
                    _create_or_migrate_schema(connection)
                    _initialized = True
        return connection
    except Exception:
        connection.close()
        raise


def query_db(statement, params=(), one=False):
    connection = get_db_connection()
    cursor = connection.cursor(cursor_factory=RealDictCursor) if not _connection_is_sqlite(connection) else connection.cursor()
    try:
        if _connection_is_sqlite(connection):
            statement = statement.replace("%s", "?")
        cursor.execute(statement, params)
        if cursor.description:
            rows = cursor.fetchall()
            return dict(rows[0]) if one and rows else (None if one else [dict(row) for row in rows])
        connection.commit()
        return cursor.rowcount
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def init_db():
    connection = get_db_connection()
    connection.close()


if __name__ == "__main__":
    init_db()
    print("Vior Hotel database is ready.")
