import os
import base64
import requests
import psycopg2
import smtplib
from email.mime.text import MIMEText
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ==========================================
# DATABASE HELPER (PostgreSQL for Vercel)
# ==========================================
def get_db_connection():
    # Vercel provides the DATABASE_URL in the environment variables
    db_url = os.getenv('DATABASE_URL')
    # Some providers use postgres:// instead of postgresql://
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    
    conn = psycopg2.connect(db_url)
    return conn

# ==========================================
# PAGE ROUTES
# ==========================================

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        fullname = request.form['fullname']
        dob = request.form['dob']
        phone = request.form['phone']
        gender = request.form['gender']
        customer_type = request.form['customer_type']
        
        if phone == '0118075281':
            return redirect(url_for('admin_dashboard'))
        
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (fullname, dob, phone, gender, customer_type) VALUES (%s, %s, %s, %s, %s)",
                (fullname, dob, phone, gender, customer_type)
            )
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback() # Important for Postgres if insert fails
        finally:
            cursor.close()
            conn.close()
            
        return redirect(url_for('home'))
        
    return render_template('login.html')

@app.route('/cart')
def cart():
    return render_template('cart.html')

@app.route('/admin')
def admin_dashboard():
    return render_template('admin.html')

# ==========================================
# API ROUTES
# ==========================================

@app.route('/api/review', methods=['POST'])
def add_review():
    data = request.json
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO reviews (name, review) VALUES (%s, %s)", (data['name'], data['text']))
    conn.commit()
    cursor.close()
    conn.close()
    
    # Uncomment and configure this to send emails in production
    # send_email_feedback(data['name'], data['text'])
    return jsonify({"status": "success"})

# ==========================================
# M-PESA INTEGRATION
# ==========================================

@app.route('/api/mpesa/pay', methods=['POST'])
def mpesa_pay():
    data = request.json
    phone = data.get('phone')
    amount = data.get('amount')

    phone = str(phone).strip()
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    elif phone.startswith('+'):
        phone = phone[1:]

    if os.getenv('MPESA_ENVIRONMENT') == 'production':
        MPESA_API_URL = "https://api.safaricom.co.ke"
    else:
        MPESA_API_URL = "https://sandbox.safaricom.co.ke"

    auth_url = f"{MPESA_API_URL}/oauth/v1/generate?grant_type=client_credentials"
    auth_response = requests.get(
        auth_url, 
        auth=(os.getenv('MPESA_CONSUMER_KEY'), os.getenv('MPESA_CONSUMER_SECRET'))
    )
    
    if auth_response.status_code != 200:
        return jsonify({"error": "Failed to get token"}), 400
        
    access_token = auth_response.json()['access_token']

    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    shortcode = os.getenv('MPESA_SHORTCODE')
    passkey = os.getenv('MPESA_PASSKEY')
    password = base64.b64encode((shortcode + passkey + timestamp).encode()).decode()

    stk_url = f"{MPESA_API_URL}/mpesa/stkpush/v1/processrequest"
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
        "CallBackURL": os.getenv('CALLBACK_URL'),
        "AccountReference": "Vior_Hotel",
        "TransactionDesc": "Vior Hotel Order"
    }

    response = requests.post(stk_url, json=payload, headers=headers)
    return jsonify(response.json()), response.status_code

@app.route('/mpesa/callback', methods=['POST'])
def mpesa_callback():
    print("Callback received:", request.json)
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

if __name__ == '__main__':
    app.run(debug=True)