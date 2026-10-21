import os
import base64
import requests
import sqlite3
import smtplib
import re
import math
import uuid
import secrets
from email.mime.text import MIMEText
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from dotenv import load_dotenv
from database import init_db

# Load OS environment first, then fill missing values from local env files.
load_dotenv('. env', override=False)
load_dotenv('.env', override=False)

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', '').lower() in ('1', 'true', 'yes'),
)

# ==========================================
# DATABASE HELPER
# ==========================================
def get_db_connection():
    conn = sqlite3.connect('vior_hotel.db')
    conn.row_factory = sqlite3.Row
    columns = {row['name'] for row in conn.execute("PRAGMA table_info(orders)").fetchall()}
    if columns and 'checkout_request_id' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN checkout_request_id TEXT")
    if columns and 'payment_method' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT DEFAULT 'M-Pesa STK'")
        conn.commit()
    if columns and 'delivery_address' not in columns:
        conn.execute("ALTER TABLE orders ADD COLUMN delivery_address TEXT")
        conn.commit()
    migrations = {
        'customer_name': 'TEXT',
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
        if columns and column not in columns:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {column} {column_type}")
    conn.commit()
    return conn

MENU_PRICES = {
    'Tea': 50, 'Omelette': 100, 'Eggs': 50, 'Mandazi': 20, 'Chapati': 20,
    'Bread': 50, 'Sandwich': 150, 'Sausages': 60, 'Porridge': 50,
    'Coffee': 100, 'Fresh Juice': 150, 'Fruit Salad': 200, 'Cupcakes': 100,
    'Chapati Beans': 150, 'Green Grams': 150, 'French Beans': 150,
    'Cowpeas': 150, 'Ugali Beef Stew': 250, 'Ugali Nyama Choma': 400,
    'Rice Chicken': 300, 'Rice Beef': 300, 'Smashed Potatoes Stew': 250,
    'Chips': 150, 'Fish': 350, 'Chips and Chicken': 350, 'Matoke': 200,
    'Soda': 80, 'Biscuits': 50, 'Fruit Cake': 100, 'Crisps': 100,
    'Bites': 100, 'Meat Balls': 150,
}
MENU_ITEMS = [{'name': name, 'price': price} for name, price in MENU_PRICES.items()]
app.secret_key = os.getenv('FLASK_SECRET_KEY') or os.urandom(32)
init_db()


def _address_token_serializer():
    return URLSafeTimedSerializer(app.secret_key, salt='delivery-address-verification')


def _verified_address_payload(data):
    token = str(data.get('address_verification_token', '')).strip()
    try:
        verified = _address_token_serializer().loads(token, max_age=1800)
    except (BadSignature, SignatureExpired):
        return None
    if verified.get('address') != str(data.get('delivery_address', '')).strip():
        return None
    if verified.get('postal_code') != str(data.get('delivery_postal_code', '')).strip():
        return None
    return verified


def _save_order_items(conn, order_id, items):
    conn.executemany(
        "INSERT INTO order_items (order_id, item_name, unit_price) VALUES (?, ?, ?)",
        [(order_id, item['name'], MENU_PRICES[item['name']]) for item in items],
    )


def _mock_mpesa_response(order_number):
    """Allows local development and demos even when Safaricom credentials or network access is unavailable."""
    mock_checkout_id = f"mock-{uuid.uuid4().hex[:20]}"
    return jsonify({
        "MerchantRequestID": f"mock-{uuid.uuid4().hex}",
        "CheckoutRequestID": mock_checkout_id,
        "ResponseCode": "0",
        "ResponseDescription": "Success. Request accepted.",
        "CustomerMessage": "M-Pesa simulated in local mode. Complete the demo flow in development.",
        "OrderNumber": order_number,
    }), 200


def _should_use_mock_mpesa():
    return app.debug or os.getenv('ALLOW_MOCK_MPESA', '').lower() in ('1', 'true', 'yes', 'on')

# ==========================================
# PAGE ROUTES (HTML)
# ==========================================

@app.route('/')
def home():
    """Renders the main homepage"""
    if not session.get('customer_phone') or not session.get('customer_name'):
        return redirect(url_for('register'))
    session.setdefault('cart_id', uuid.uuid4().hex)
    return render_template('index.html', menu_items=MENU_ITEMS)

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Creates a lightweight customer profile before showing the homepage."""
    if session.get('customer_phone') and session.get('customer_name'):
        return redirect(url_for('home'))
    session.setdefault('register_csrf_token', secrets.token_urlsafe(32))

    if request.method == 'POST':
        submitted_token = request.form.get('csrf_token', '')
        if not secrets.compare_digest(submitted_token, session.get('register_csrf_token', '')):
            return render_template('register.html', error='Your session expired. Please submit the form again.', csrf_token=session['register_csrf_token']), 400

        fullname = ' '.join(request.form.get('fullname', '').split())
        raw_phone = re.sub(r'[\s()-]', '', request.form.get('phone', ''))
        if len(fullname) < 2 or len(fullname) > 80 or any(ord(char) < 32 for char in fullname):
            return render_template('register.html', error='Enter your name (2–80 characters).', fullname=fullname, phone=request.form.get('phone', ''), csrf_token=session['register_csrf_token'])
        if not re.fullmatch(r'(?:254|0|\+254)?[17]\d{8}', raw_phone):
            return render_template('register.html', error='Enter a valid Kenyan mobile number, such as 0712345678.', fullname=fullname, phone=request.form.get('phone', ''), csrf_token=session['register_csrf_token'])

        phone = raw_phone[1:] if raw_phone.startswith('+') else raw_phone
        if phone.startswith('0'):
            phone = '254' + phone[1:]
        elif not phone.startswith('254'):
            phone = '254' + phone

        legacy_phones = (phone, '0' + phone[3:], '+' + phone)
        conn = get_db_connection()
        user = conn.execute(
            "SELECT fullname, phone FROM users WHERE phone IN (?, ?, ?) LIMIT 1",
            legacy_phones,
        ).fetchone()
        if user and (user['fullname'] or '').strip().casefold() != fullname.casefold():
            conn.close()
            return render_template('register.html', error='That number already has a profile. Contact the hotel if you need to update it.', fullname=fullname, phone=request.form.get('phone', ''), csrf_token=session['register_csrf_token'])
        if not user:
            try:
                conn.execute(
                    "INSERT INTO users (fullname, phone) VALUES (?, ?)",
                    (fullname, phone),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.close()
                return render_template('register.html', error='We could not create your profile. Please try again.', fullname=fullname, phone=request.form.get('phone', ''), csrf_token=session['register_csrf_token']), 409
        conn.close()

        # Rotate the session at registration to avoid carrying a pre-registration session ID.
        session.clear()
        session['customer_name'] = user['fullname'] if user else fullname
        session['customer_phone'] = phone
        session['cart_id'] = uuid.uuid4().hex
        return redirect(url_for('home'))

    return render_template('register.html', csrf_token=session['register_csrf_token'])

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Handles staff admin sign-in; customer profiles are created at /register."""
    if session.get('is_admin'):
        return redirect(url_for('admin_dashboard'))
    if request.method == 'POST':
        # Admin access requires a server-side password; never trust the phone number alone.
        phone = re.sub(r'[\s()-]', '', request.form.get('phone', ''))
        admin_password = os.getenv('ADMIN_PASSWORD')
        if phone == '0118075281' and admin_password and secrets.compare_digest(request.form.get('admin_password', ''), admin_password):
            session.clear()
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        return render_template('login.html', error='Admin phone number or password is incorrect.'), 401

    return render_template('login.html')

@app.route('/cart')
def cart():
    """Renders the shopping cart and checkout page"""
    if not session.get('customer_phone'):
        return redirect(url_for('register'))
    session.setdefault('cart_id', uuid.uuid4().hex)
    return render_template('cart.html')

@app.route('/admin')
def admin_dashboard():
    """Renders the admin dashboard for Kairuvic"""
    if not session.get('is_admin'):
        return redirect(url_for('login'))
    conn = get_db_connection()
    orders = conn.execute("""
        SELECT orders.*, item_summary.items
        FROM orders
        LEFT JOIN (
            SELECT order_id, GROUP_CONCAT(item_name || ' × ' || quantity, ', ') AS items
            FROM (
                SELECT order_id, item_name, COUNT(*) AS quantity
                FROM order_items
                GROUP BY order_id, item_name
            ) AS grouped_items
            GROUP BY order_id
        ) AS item_summary ON item_summary.order_id = orders.id
        ORDER BY id DESC
    """).fetchall()
    conn.close()
    return render_template('admin.html', orders=orders)

@app.route('/admin/order/<int:order_id>/status', methods=['POST'])
def update_order_status(order_id):
    """Lets staff publish preparation and dispatch stages to customer tracking."""
    if not session.get('is_admin'):
        return redirect(url_for('login'))

    status = request.form.get('status', '')
    if status not in ('Payment Verified', 'Preparing', 'Out for Delivery', 'Delivered'):
        return jsonify({"error": "Choose a valid order status."}), 400

    conn = get_db_connection()
    order = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        conn.close()
        return jsonify({"error": "Order not found."}), 404

    current_status = order['status']
    allowed_transitions = {
        'Awaiting Till Payment — verify manually': {'Payment Verified'},
        'Payment Verified': {'Preparing'},
        'Paid': {'Preparing'},
        'Pending': {'Preparing'},
        'Preparing': {'Preparing', 'Out for Delivery'},
        'Out for Delivery': {'Out for Delivery', 'Delivered'},
        'Delivered': {'Delivered'},
    }
    if status not in allowed_transitions.get(current_status, set()):
        conn.close()
        return jsonify({"error": "Verify payment and update the order through each stage in sequence."}), 400

    status_timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if status == 'Out for Delivery':
        cursor = conn.execute(
            "UPDATE orders SET status = ?, status_updated_at = ?, driver_latitude = NULL, driver_longitude = NULL, driver_location_updated_at = NULL WHERE id = ?",
            (status, status_timestamp, order_id),
        )
    else:
        cursor = conn.execute(
            "UPDATE orders SET status = ?, status_updated_at = ? WHERE id = ?",
            (status, status_timestamp, order_id),
        )
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
        return jsonify({"error": "Order not found."}), 404
    return redirect(url_for('admin_dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# ==========================================
# API ROUTES (BACKEND LOGIC)
# ==========================================

@app.route('/api/review', methods=['POST'])
def add_review():
    """Handles customer reviews and sends them to email"""
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    review = str(data.get('text', '')).strip()
    if not name or not review:
        return jsonify({"error": "Name and review are required."}), 400
    conn = get_db_connection()
    conn.execute("INSERT INTO reviews (name, review) VALUES (?, ?)", (name, review))
    conn.commit()
    conn.close()
    
    # Send feedback to email
    send_email_feedback(name, review)
    return jsonify({"status": "success"})


@app.route('/api/address/verify', methods=['POST'])
def verify_delivery_address():
    """Geocodes a destination and verifies its postal code against map data."""
    data = request.get_json(silent=True) or {}
    street = str(data.get('street', '')).strip()
    city = str(data.get('city', '')).strip()
    region = str(data.get('region', '')).strip()
    address = ', '.join(part for part in (street, city, region, str(data.get('country', '')).strip()) if part)
    postal_code = str(data.get('postal_code', '')).strip()
    country = str(data.get('country', '')).strip()
    if not street or not city or not postal_code or not country:
        return jsonify({"error": "Enter the full address, postal code, and country."}), 400

    try:
        result = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={
                'street': street,
                'city': city,
                'state': region,
                'postalcode': postal_code,
                'country': country,
                'format': 'jsonv2',
                'addressdetails': 1,
                'limit': 5,
            },
            headers={'User-Agent': 'ViorHotelDelivery/1.0 (address verification)'},
            timeout=12,
        )
        result.raise_for_status()
        matches = result.json()
    except (requests.RequestException, ValueError):
        return jsonify({"error": "Address lookup is temporarily unavailable. Please try again or contact the hotel."}), 503

    normalized_postal = re.sub(r'[^a-z0-9]', '', postal_code.lower())
    for match in matches:
        matched_address = match.get('address', {})
        returned_postal = str(matched_address.get('postcode', '')).strip()
        normalized_returned = re.sub(r'[^a-z0-9]', '', returned_postal.lower())
        if returned_postal and normalized_postal == normalized_returned:
            try:
                latitude = float(match['lat'])
                longitude = float(match['lon'])
            except (KeyError, TypeError, ValueError):
                continue
            return jsonify({
                'verified': True,
                'postal_code': returned_postal,
                'display_name': match.get('display_name', address),
                'latitude': latitude,
                'longitude': longitude,
                'provider': 'OpenStreetMap Nominatim',
                'verification_token': _address_token_serializer().dumps({
                    'address': ', '.join(part for part in (street, city, region, country) if part),
                    'postal_code': returned_postal,
                    'latitude': latitude,
                    'longitude': longitude,
                }),
            })

    return jsonify({
        'verified': False,
        'error': 'We could not confirm that postal code for this address. Check the address and postal code, or contact the hotel.',
    }), 422

def send_email_feedback(name, review):
    """Sends an email using Gmail SMTP. 
    IMPORTANT: Replace sender_email and sender_password with your own!"""
    
    sender_email = "your_email@gmail.com" 
    sender_password = "your_app_password"  
    receiver_email = "victorkairu6@gmail.com"
    
    msg = MIMEText(f"New Review from {name}:\n\n{review}")
    msg['Subject'] = "Vior Hotel - New Feedback"
    msg['From'] = sender_email
    msg['To'] = receiver_email
    
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, receiver_email, msg.as_string())
    except Exception as e:
        print(f"Email failed to send: {e}")

# ==========================================
# M-PESA INTEGRATION
# ==========================================

@app.route('/api/mpesa/pay', methods=['POST'])
def mpesa_pay():
    """Initiates the STK Push to the customer's phone"""
    data = request.get_json(silent=True) or {}
    phone = str(data.get('phone', '')).strip().replace(' ', '').replace('-', '')
    items = data.get('items')
    order_type = data.get('order_type', 'Pickup')
    delivery_address = str(data.get('delivery_address', '')).strip()
    verified_address = _verified_address_payload(data) if order_type == 'Online' else None

    if not isinstance(items, list) or not items:
        return jsonify({"error": "Your cart is empty."}), 400
    if order_type not in ('Pickup', 'Online'):
        return jsonify({"error": "Choose a valid delivery method."}), 400
    if order_type == 'Online' and not verified_address:
        return jsonify({"error": "Verify your delivery address and postal code before checkout."}), 400
    if not re.fullmatch(r'(?:254|0|\+254)?[17]\d{8}', phone):
        return jsonify({"error": "Enter a valid Kenyan M-Pesa phone number."}), 400
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get('name'), str)
        or item['name'] not in MENU_PRICES
        for item in items
    ):
        return jsonify({"error": "Your cart contains an unavailable menu item."}), 400

    amount = sum(MENU_PRICES[item['name']] for item in items)
    if amount < 1:
        return jsonify({"error": "Order total must be greater than zero."}), 400

    # Ensure phone number is in the correct format (2547XXXXXXXX)
    if phone.startswith('+'):
        phone = phone[1:]
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    elif not phone.startswith('254'):
        phone = '254' + phone

    consumer_key = os.getenv('MPESA_CONSUMER_KEY')
    consumer_secret = os.getenv('MPESA_CONSUMER_SECRET')
    shortcode = os.getenv('MPESA_SHORTCODE')
    passkey = os.getenv('MPESA_PASSKEY')
    callback_url = os.getenv('CALLBACK_URL', '').strip()
    order_number = f"VH-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

    conn = get_db_connection()
    conn.execute(
        "INSERT INTO orders (order_number, customer_phone, customer_name, order_type, delivery_time, amount, status, payment_method, delivery_address, delivery_postal_code, address_verified, delivery_latitude, delivery_longitude, status_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_number, phone, session.get('customer_name'), order_type, datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), amount, 'Payment Pending', 'M-Pesa STK', delivery_address or None, verified_address.get('postal_code') if verified_address else None, int(bool(verified_address)), verified_address.get('latitude') if verified_address else None, verified_address.get('longitude') if verified_address else None, datetime.now(timezone.utc).isoformat(timespec='seconds'))
    )
    conn.commit()
    order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _save_order_items(conn, order_id, items)
    conn.commit()
    conn.close()

    if not all((consumer_key, consumer_secret, shortcode, passkey)) or not callback_url.startswith('https://') or ' ' in callback_url:
        if _should_use_mock_mpesa():
            return _mock_mpesa_response(order_number)
        _update_order_status(order_id, 'Payment Request Failed')
        return jsonify({"error": "M-Pesa is not configured yet. Please contact the hotel."}), 503

    # 1. Get Access Token
    auth_url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    try:
        auth_response = requests.get(auth_url, auth=(consumer_key, consumer_secret), timeout=15)
    except Exception:
        if _should_use_mock_mpesa():
            return _mock_mpesa_response(order_number)
        _update_order_status(order_id, 'Payment Request Failed')
        return jsonify({"error": "Could not connect to M-Pesa. Please try again."}), 502
    
    if auth_response.status_code != 200:
        if _should_use_mock_mpesa():
            return _mock_mpesa_response(order_number)
        _update_order_status(order_id, 'Payment Request Failed')
        return jsonify({"error": "Failed to get access token"}), 400
        
    access_token = auth_response.json()['access_token']

    # 2. Generate Password
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    password = base64.b64encode((shortcode + passkey + timestamp).encode()).decode()

    # 3. Prepare STK Push Request
    stk_url = "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest"
    headers = {"Authorization": f"Bearer {access_token}"}
    
    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": "Vior_Hotel",
        "TransactionDesc": "Vior Hotel Order"
    }

    # 4. Send Request to Safaricom
    try:
        response = requests.post(stk_url, json=payload, headers=headers, timeout=20)
        result = response.json()
    except Exception:
        if _should_use_mock_mpesa():
            return _mock_mpesa_response(order_number)
        _update_order_status(order_id, 'Payment Request Failed')
        return jsonify({"error": "M-Pesa did not respond. Please try again."}), 502

    if response.ok and result.get('ResponseCode') == '0':
        checkout_request_id = result.get('CheckoutRequestID')
        conn = get_db_connection()
        conn.execute("UPDATE orders SET checkout_request_id = ? WHERE id = ?", (checkout_request_id, order_id))
        conn.commit()
        conn.close()
        result['OrderNumber'] = order_number
    else:
        _update_order_status(order_id, 'Payment Request Failed')
    
    # 5. Return response to Frontend
    return jsonify(result), response.status_code

def _update_order_status(order_id, status):
    conn = get_db_connection()
    conn.execute(
        "UPDATE orders SET status = ?, status_updated_at = ? WHERE id = ?",
        (status, datetime.now().isoformat(timespec='seconds'), order_id),
    )
    conn.commit()
    conn.close()


def _normalize_order_status(raw_status):
    """Maps DB status values to the 4 tracking stages shown in the UI."""
    status = (raw_status or 'Pending').strip()
    lowered = status.lower()

    if 'delivered' in lowered:
        return 'Delivered'
    if 'out for delivery' in lowered or 'out-for-delivery' in lowered or ('out' in lowered and 'delivery' in lowered):
        return 'Out for Delivery'
    if 'prepar' in lowered:
        return 'Preparing'
    return 'Pending'


@app.route('/api/order/status/<string:order_number>')
def order_status(order_number):
    """Returns the current status for a given order number."""
    conn = get_db_connection()
    row = conn.execute(
        "SELECT order_number, status, status_updated_at, driver_latitude, driver_longitude, driver_location_updated_at, delivery_latitude, delivery_longitude, delivery_address, delivery_postal_code, address_verified FROM orders WHERE lower(order_number) = lower(?) LIMIT 1",
        (order_number,),
    ).fetchone()
    conn.close()

    if row is None:
        return jsonify({"error": "Order not found."}), 404

    status = _normalize_order_status(row['status'])
    return jsonify({
        "order_number": row['order_number'],
        "status": status,
        "raw_status": row['status'],
        "status_updated_at": row['status_updated_at'],
        "driver": {
            "latitude": row['driver_latitude'],
            "longitude": row['driver_longitude'],
            "updated_at": row['driver_location_updated_at'],
        } if row['driver_latitude'] is not None and row['driver_longitude'] is not None else None,
        "destination": {
            "latitude": row['delivery_latitude'],
            "longitude": row['delivery_longitude'],
            "address": row['delivery_address'],
            "postal_code": row['delivery_postal_code'],
            "verified": bool(row['address_verified']),
        } if row['delivery_address'] else None,
    })


@app.route('/api/admin/order/<int:order_id>/location', methods=['POST'])
def update_driver_location(order_id):
    """Accepts GPS readings from an authenticated driver's device."""
    if not session.get('is_admin'):
        return jsonify({"error": "Admin access required."}), 403
    data = request.get_json(silent=True) or {}
    try:
        latitude = float(data.get('latitude'))
        longitude = float(data.get('longitude'))
        accuracy = float(data.get('accuracy', 1000))
    except (TypeError, ValueError):
        return jsonify({"error": "Valid GPS coordinates are required."}), 400
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return jsonify({"error": "GPS coordinates are outside valid ranges."}), 400
    if not math.isfinite(accuracy) or accuracy < 0 or accuracy > 100:
        return jsonify({"error": "GPS accuracy is too low. Wait for a more accurate location."}), 422

    conn = get_db_connection()
    order = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        conn.close()
        return jsonify({"error": "Order not found."}), 404
    if order['status'] != 'Out for Delivery':
        conn.close()
        return jsonify({"error": "Set the order to Out for Delivery before sharing driver GPS."}), 409
    updated_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    conn.execute(
        "UPDATE orders SET driver_latitude = ?, driver_longitude = ?, driver_location_updated_at = ? WHERE id = ?",
        (latitude, longitude, updated_at, order_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"status": "location_updated", "updated_at": updated_at})


@app.route('/api/mpesa/till-order', methods=['POST'])
def mpesa_till_order():
    """Records an order for customer-initiated M-Pesa Till payment."""
    data = request.get_json(silent=True) or {}
    phone = str(data.get('phone', '')).strip().replace(' ', '').replace('-', '')
    items = data.get('items')
    order_type = data.get('order_type', 'Pickup')
    delivery_address = str(data.get('delivery_address', '')).strip()
    verified_address = _verified_address_payload(data) if order_type == 'Online' else None

    if not isinstance(items, list) or not items:
        return jsonify({"error": "Your cart is empty."}), 400
    if order_type not in ('Pickup', 'Online'):
        return jsonify({"error": "Choose a valid delivery method."}), 400
    if order_type == 'Online' and not verified_address:
        return jsonify({"error": "Verify your delivery address and postal code before checkout."}), 400
    if not re.fullmatch(r'(?:254|0|\+254)?[17]\d{8}', phone):
        return jsonify({"error": "Enter a valid Kenyan phone number for your order."}), 400
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get('name'), str)
        or item['name'] not in MENU_PRICES
        for item in items
    ):
        return jsonify({"error": "Your cart contains an unavailable menu item."}), 400

    phone = phone[1:] if phone.startswith('+') else phone
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    elif not phone.startswith('254'):
        phone = '254' + phone
    amount = sum(MENU_PRICES[item['name']] for item in items)
    order_number = f"VH-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO orders (order_number, customer_phone, customer_name, order_type, delivery_time, amount, status, payment_method, delivery_address, delivery_postal_code, address_verified, delivery_latitude, delivery_longitude, status_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (order_number, phone, session.get('customer_name'), order_type, datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), amount, 'Awaiting Till Payment — verify manually', 'M-Pesa Till', delivery_address or None, verified_address.get('postal_code') if verified_address else None, int(bool(verified_address)), verified_address.get('latitude') if verified_address else None, verified_address.get('longitude') if verified_address else None, datetime.now(timezone.utc).isoformat(timespec='seconds'))
    )
    order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _save_order_items(conn, order_id, items)
    conn.commit()
    conn.close()
    return jsonify({
        "status": "awaiting_payment",
        "order_number": order_number,
        "amount": amount,
        "till_number": os.getenv('MPESA_TILL_NUMBER', '3484738'),
        "message": "Order saved. Complete payment to the till, then the hotel will verify it.",
    }), 201

@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    """Receives the payment confirmation from Safaricom"""
    data = request.get_json(silent=True) or {}
    callback = data.get('Body', {}).get('stkCallback', {})
    checkout_request_id = callback.get('CheckoutRequestID')
    if not checkout_request_id:
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

    result_code = callback.get('ResultCode')
    result_items = callback.get('CallbackMetadata', {}).get('Item', [])
    metadata = {item.get('Name'): item.get('Value') for item in result_items if isinstance(item, dict)}
    conn = get_db_connection()
    if result_code == 0:
        conn.execute(
            "UPDATE orders SET status = 'Paid', status_updated_at = ? WHERE checkout_request_id = ?",
            (datetime.now(timezone.utc).isoformat(timespec='seconds'), checkout_request_id)
        )
    else:
        conn.execute(
            "UPDATE orders SET status = 'Payment Failed', status_updated_at = ? WHERE checkout_request_id = ? AND status = 'Payment Pending'",
            (datetime.now(timezone.utc).isoformat(timespec='seconds'), checkout_request_id)
        )
    conn.commit()
    conn.close()
    print(f"M-Pesa callback for {checkout_request_id}: {result_code}; receipt {metadata.get('MpesaReceiptNumber', 'unavailable')}")
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

# ==========================================
# START SERVER
# ==========================================
if __name__ == '__main__':
    app.run(debug=True)