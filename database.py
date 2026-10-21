import sqlite3

def init_db():
    conn = sqlite3.connect('vior_hotel.db')
    cursor = conn.cursor()
    # Users Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fullname TEXT,
            dob TEXT,
            phone TEXT UNIQUE,
            gender TEXT,
            customer_type TEXT
        )
    ''')
    # Orders Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_number TEXT,
            customer_phone TEXT,
            customer_name TEXT,
            order_type TEXT,
            delivery_time TEXT,
            amount REAL,
            payment_method TEXT DEFAULT 'M-Pesa STK',
            checkout_request_id TEXT,
            delivery_address TEXT,
            delivery_postal_code TEXT,
            address_verified INTEGER DEFAULT 0,
            delivery_latitude REAL,
            delivery_longitude REAL,
            driver_latitude REAL,
            driver_longitude REAL,
            driver_location_updated_at TEXT,
            status_updated_at TEXT,
            status TEXT DEFAULT 'Pending'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            item_name TEXT NOT NULL,
            unit_price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
        )
    ''')
    columns = {row[1] for row in cursor.execute("PRAGMA table_info(orders)").fetchall()}
    migrations = {
        'customer_name': 'TEXT',
        'delivery_address': 'TEXT',
        'delivery_postal_code': 'TEXT',
        'address_verified': 'INTEGER DEFAULT 0',
        'delivery_latitude': 'REAL',
        'delivery_longitude': 'REAL',
        'driver_latitude': 'REAL',
        'driver_longitude': 'REAL',
        'driver_location_updated_at': 'TEXT',
        'status_updated_at': 'TEXT',
    }
    for column, column_type in migrations.items():
        if column not in columns:
            cursor.execute(f"ALTER TABLE orders ADD COLUMN {column} {column_type}")
    # Reviews Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            review TEXT
        )
    ''')
    conn.commit()
    conn.close()
    print("Database created successfully!")

if __name__ == '__main__':
    init_db()