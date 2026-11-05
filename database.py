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
        phone_verified_at TEXT,
        email TEXT,
        preferred_language TEXT NOT NULL DEFAULT 'en',
        whatsapp_opt_in INTEGER NOT NULL DEFAULT 0,
        dob TEXT,
        gender TEXT,
        customer_type TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT NOT NULL UNIQUE,
        idempotency_key TEXT,
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
        driver_updated_at TEXT,
        customer_id INTEGER,
        special_instructions TEXT,
        scheduled_at TEXT,
        whatsapp_opt_in INTEGER NOT NULL DEFAULT 0,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        review TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS otp_challenges (
        id TEXT PRIMARY KEY,
        phone TEXT NOT NULL,
        fullname TEXT,
        purpose TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        consumed_at TEXT,
        request_ip TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT NOT NULL,
        provider TEXT NOT NULL,
        merchant_reference TEXT NOT NULL UNIQUE,
        order_tracking_id TEXT UNIQUE,
        redirect_url TEXT,
        amount REAL NOT NULL,
        currency TEXT NOT NULL,
        status TEXT NOT NULL,
        provider_status TEXT,
        receipt TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS menu_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL,
        category TEXT NOT NULL,
        price INTEGER NOT NULL,
        media_url TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'image',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        caption TEXT NOT NULL,
        media_url TEXT NOT NULL,
        media_type TEXT NOT NULL,
        button_text TEXT,
        button_url TEXT,
        published INTEGER NOT NULL DEFAULT 0,
        starts_at TEXT,
        ends_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS customer_addresses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        label TEXT NOT NULL,
        address TEXT NOT NULL,
        locality TEXT NOT NULL,
        region TEXT,
        postal_code TEXT NOT NULL,
        country TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS staff_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fullname TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        details TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS loyalty_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL UNIQUE,
        points_balance INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS loyalty_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        order_number TEXT,
        points INTEGER NOT NULL,
        transaction_type TEXT NOT NULL,
        reference TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        unit TEXT NOT NULL,
        quantity REAL NOT NULL DEFAULT 0,
        reorder_level REAL NOT NULL DEFAULT 0,
        unit_cost REAL NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suppliers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id INTEGER NOT NULL,
        supplier_id INTEGER,
        quantity REAL NOT NULL,
        unit_cost REAL NOT NULL DEFAULT 0,
        movement_type TEXT NOT NULL,
        reference TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_recipes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        menu_item_name TEXT NOT NULL,
        inventory_item_id INTEGER NOT NULL,
        quantity_per_serving REAL NOT NULL,
        UNIQUE (menu_item_name, inventory_item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT NOT NULL UNIQUE,
        transaction_type TEXT NOT NULL,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        currency TEXT NOT NULL DEFAULT 'KES',
        reference TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_assignments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_number TEXT NOT NULL UNIQUE,
        driver_name TEXT NOT NULL,
        driver_phone TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Assigned',
        assigned_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT NOT NULL UNIQUE,
        channel TEXT NOT NULL,
        recipient TEXT NOT NULL,
        template_name TEXT NOT NULL,
        language_code TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        last_error TEXT,
        provider_message_id TEXT,
        created_at TEXT NOT NULL,
        sent_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_ip TEXT NOT NULL,
        attempted_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_otp_phone_created_at ON otp_challenges (phone, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_otp_ip_created_at ON otp_challenges (request_ip, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_payments_order_number ON payments (order_number)",
    "CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip_time ON auth_attempts (request_ip, attempted_at)",
)

_POSTGRES_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        fullname TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        phone_verified_at TEXT,
        email TEXT,
        preferred_language TEXT NOT NULL DEFAULT 'en',
        whatsapp_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
        dob TEXT,
        gender TEXT,
        customer_type TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY,
        order_number TEXT NOT NULL UNIQUE,
        idempotency_key TEXT,
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
        driver_updated_at TEXT,
        customer_id INTEGER,
        special_instructions TEXT,
        scheduled_at TEXT,
        whatsapp_opt_in BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        review TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS otp_challenges (
        id TEXT PRIMARY KEY,
        phone TEXT NOT NULL,
        fullname TEXT,
        purpose TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        consumed_at TEXT,
        request_ip TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY,
        order_number TEXT NOT NULL,
        provider TEXT NOT NULL,
        merchant_reference TEXT NOT NULL UNIQUE,
        order_tracking_id TEXT UNIQUE,
        redirect_url TEXT,
        amount DOUBLE PRECISION NOT NULL,
        currency TEXT NOT NULL,
        status TEXT NOT NULL,
        provider_status TEXT,
        receipt TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS menu_items (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL,
        category TEXT NOT NULL,
        price INTEGER NOT NULL,
        media_url TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'image',
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stories (
        id SERIAL PRIMARY KEY,
        title TEXT NOT NULL,
        caption TEXT NOT NULL,
        media_url TEXT NOT NULL,
        media_type TEXT NOT NULL,
        button_text TEXT,
        button_url TEXT,
        published BOOLEAN NOT NULL DEFAULT FALSE,
        starts_at TEXT,
        ends_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS customer_addresses (
        id SERIAL PRIMARY KEY,
        customer_id INTEGER NOT NULL,
        label TEXT NOT NULL,
        address TEXT NOT NULL,
        locality TEXT NOT NULL,
        region TEXT,
        postal_code TEXT NOT NULL,
        country TEXT NOT NULL,
        is_default BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS staff_accounts (
        id SERIAL PRIMARY KEY,
        fullname TEXT NOT NULL,
        phone TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        details TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS loyalty_accounts (
        id SERIAL PRIMARY KEY,
        customer_id INTEGER NOT NULL UNIQUE,
        points_balance INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS loyalty_transactions (
        id SERIAL PRIMARY KEY,
        customer_id INTEGER NOT NULL,
        order_number TEXT,
        points INTEGER NOT NULL,
        transaction_type TEXT NOT NULL,
        reference TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_items (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        unit TEXT NOT NULL,
        quantity DOUBLE PRECISION NOT NULL DEFAULT 0,
        reorder_level DOUBLE PRECISION NOT NULL DEFAULT 0,
        unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suppliers (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        phone TEXT,
        email TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_movements (
        id SERIAL PRIMARY KEY,
        item_id INTEGER NOT NULL,
        supplier_id INTEGER,
        quantity DOUBLE PRECISION NOT NULL,
        unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0,
        movement_type TEXT NOT NULL,
        reference TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS inventory_recipes (
        id SERIAL PRIMARY KEY,
        menu_item_name TEXT NOT NULL,
        inventory_item_id INTEGER NOT NULL,
        quantity_per_serving DOUBLE PRECISION NOT NULL,
        UNIQUE (menu_item_name, inventory_item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS financial_transactions (
        id SERIAL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        transaction_type TEXT NOT NULL,
        category TEXT NOT NULL,
        amount DOUBLE PRECISION NOT NULL,
        currency TEXT NOT NULL DEFAULT 'KES',
        reference TEXT,
        notes TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS delivery_assignments (
        id SERIAL PRIMARY KEY,
        order_number TEXT NOT NULL UNIQUE,
        driver_name TEXT NOT NULL,
        driver_phone TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Assigned',
        assigned_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_outbox (
        id SERIAL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        channel TEXT NOT NULL,
        recipient TEXT NOT NULL,
        template_name TEXT NOT NULL,
        language_code TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'Pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT NOT NULL,
        last_error TEXT,
        provider_message_id TEXT,
        created_at TEXT NOT NULL,
        sent_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_attempts (
        id SERIAL PRIMARY KEY,
        request_ip TEXT NOT NULL,
        attempted_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_otp_phone_created_at ON otp_challenges (phone, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_otp_ip_created_at ON otp_challenges (request_ip, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_payments_order_number ON payments (order_number)",
    "CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip_time ON auth_attempts (request_ip, attempted_at)",
)

_MIGRATIONS = {
    "users": {
        "fullname": "TEXT",
        "phone": "TEXT",
        "phone_verified_at": "TEXT",
        "email": "TEXT",
        "preferred_language": "TEXT NOT NULL DEFAULT 'en'",
        "whatsapp_opt_in": "BOOLEAN NOT NULL DEFAULT FALSE",
        "created_at": "TEXT",
        "dob": "TEXT",
        "gender": "TEXT",
        "customer_type": "TEXT",
    },
    "orders": {
        "order_number": "TEXT",
        "idempotency_key": "TEXT",
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
        "customer_id": "INTEGER",
        "special_instructions": "TEXT",
        "scheduled_at": "TEXT",
        "whatsapp_opt_in": "BOOLEAN NOT NULL DEFAULT FALSE",
        "created_at": "TEXT",
    },
    "reviews": {
        "name": "TEXT",
        "review": "TEXT",
    },
    "menu_items": {
        "name": "TEXT",
        "description": "TEXT",
        "category": "TEXT",
        "price": "INTEGER",
        "media_url": "TEXT",
        "media_type": "TEXT NOT NULL DEFAULT 'image'",
        "active": "BOOLEAN NOT NULL DEFAULT TRUE",
        "created_at": "TEXT",
    },
    "stories": {
        "title": "TEXT",
        "caption": "TEXT",
        "media_url": "TEXT",
        "media_type": "TEXT",
        "button_text": "TEXT",
        "button_url": "TEXT",
        "published": "BOOLEAN NOT NULL DEFAULT FALSE",
        "starts_at": "TEXT",
        "ends_at": "TEXT",
        "created_at": "TEXT",
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
                    sqlite_type = sqlite_type.replace("BOOLEAN NOT NULL DEFAULT TRUE", "INTEGER NOT NULL DEFAULT 1")
                    sqlite_type = sqlite_type.replace("DOUBLE PRECISION", "REAL")
                    cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type}")
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_idempotency_key "
            "ON orders (idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
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


def execute_transaction(statements):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        rowcounts = []
        for statement, params in statements:
            if _connection_is_sqlite(connection):
                statement = statement.replace("%s", "?")
            cursor.execute(statement, params)
            rowcounts.append(cursor.rowcount)
        connection.commit()
        return rowcounts
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def execute_transaction_checked(statements, checked_indexes):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        rowcounts = []
        for index, (statement, params) in enumerate(statements):
            if _connection_is_sqlite(connection):
                statement = statement.replace("%s", "?")
            cursor.execute(statement, params)
            rowcounts.append(cursor.rowcount)
            if index in checked_indexes and cursor.rowcount != checked_indexes[index]:
                raise RuntimeError("A required record changed before this operation completed.")
        connection.commit()
        return rowcounts
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def add_loyalty_points(customer_id, order_number, points, created_at):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        insert = """
            INSERT INTO loyalty_transactions
                (customer_id, order_number, points, transaction_type, reference, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (reference) DO NOTHING
        """
        if _connection_is_sqlite(connection):
            insert = insert.replace("%s", "?")
        cursor.execute(
            insert,
            (customer_id, order_number, points, "earned", f"order:{order_number}", created_at),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return False
        account = """
            INSERT INTO loyalty_accounts (customer_id, points_balance, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (customer_id) DO UPDATE SET
                points_balance = loyalty_accounts.points_balance + excluded.points_balance,
                updated_at = excluded.updated_at
        """
        if _connection_is_sqlite(connection):
            account = account.replace("%s", "?")
        cursor.execute(account, (customer_id, points, created_at))
        connection.commit()
        return True
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
