import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

def init_db():
    # Get the database URL from environment variables
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        print("Error: DATABASE_URL environment variable is missing.")
        return

    # Connect to the cloud database
    conn = psycopg2.connect(db_url)
    cursor = conn.cursor()
    
    # Create tables (Postgres uses SERIAL instead of AUTOINCREMENT)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            fullname TEXT,
            dob TEXT,
            phone TEXT UNIQUE,
            gender TEXT,
            customer_type TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id SERIAL PRIMARY KEY,
            order_number TEXT,
            customer_phone TEXT,
            order_type TEXT,
            delivery_time TEXT,
            amount REAL,
            status TEXT DEFAULT 'Pending'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS reviews (
            id SERIAL PRIMARY KEY,
            name TEXT,
            review TEXT
        )
    ''')
    
    conn.commit()
    cursor.close()
    conn.close()
    print("Cloud Database created successfully!")

if __name__ == '__main__':
    init_db()