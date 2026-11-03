import base64
import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from database import query_db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

MENU_ITEMS = [
    {"name": "Tea", "price": 50},
    {"name": "Sausages", "price": 60},
    {"name": "Bread", "price": 50},
    {"name": "Omelette", "price": 100},
    {"name": "Eggs", "price": 50},
    {"name": "Chapati", "price": 20},
    {"name": "Mandazi", "price": 20},
    {"name": "Fresh Juice", "price": 150},
    {"name": "Chapati Beans", "price": 150},
    {"name": "Ugali Beef Stew", "price": 250},
    {"name": "Ugali Nyama Choma", "price": 400},
    {"name": "Rice Chicken", "price": 300},
    {"name": "Chips", "price": 150},
    {"name": "Fish", "price": 350},
    {"name": "Matoke", "price": 200},
    {"name": "Soda", "price": 80},
    {"name": "Biscuits", "price": 50},
    {"name": "Fruit Cake", "price": 100},
    {"name": "Crisps", "price": 100},
    {"name": "Bites", "price": 100},
    {"name": "Meat Balls", "price": 150},
    {"name": "Cupcakes", "price": 100},
]
MENU_PRICES = {item["name"]: item["price"] for item in MENU_ITEMS}
ORDER_STATUSES = {"Pending", "Payment Verified", "Preparing", "Out for Delivery", "Delivered"}
ADDRESS_TOKEN_SALT = "vior-delivery-address"


def _address_signer():
    return URLSafeTimedSerializer(app.secret_key, salt=ADDRESS_TOKEN_SALT)


def _normalize_phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0") and len(digits) == 10:
        digits = "254" + digits[1:]
    elif digits.startswith("7") or digits.startswith("1"):
        if len(digits) == 9:
            digits = "254" + digits
    elif digits.startswith("254") and len(digits) == 12:
        pass
    else:
        return None
    return digits if re.fullmatch(r"254[17]\d{8}", digits) else None


def _order_number():
    return f"VH-{datetime.now(timezone.utc):%Y%m%d%H%M%S}{secrets.token_hex(2).upper()}"


def _order_items(data):
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 30:
        raise ValueError("Your cart is empty or contains too many items.")

    trusted_items = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("The order contains an invalid menu item.")
        name = item.get("name")
        if not isinstance(name, str) or name not in MENU_PRICES:
            raise ValueError(f"{name or 'An item'} is not on the current menu.")
        trusted_items.append({"name": name, "price": MENU_PRICES[name]})
    return trusted_items


def _delivery_details(data):
    order_type = data.get("order_type", "Pickup")
    if not isinstance(order_type, str) or order_type not in {"Pickup", "Online"}:
        raise ValueError("Choose Pickup or Online Delivery.")
    if order_type == "Pickup":
        return order_type, "", "", False, None, None

    address = str(data.get("delivery_address") or "").strip()
    postal_code = str(data.get("delivery_postal_code") or "").strip()
    token = data.get("address_verification_token")
    if not address or not postal_code or not isinstance(token, str):
        raise ValueError("Verify your delivery address and postal code before ordering.")
    try:
        verified = _address_signer().loads(token, max_age=900)
    except (BadSignature, SignatureExpired):
        raise ValueError("Your address verification expired. Please verify it again.") from None
    if not isinstance(verified, dict):
        raise ValueError("Your address verification is invalid. Please verify it again.")
    if verified.get("address") != address or verified.get("postal_code") != postal_code:
        raise ValueError("The verified address does not match your delivery details.")
    return order_type, address, postal_code, True, verified.get("latitude"), verified.get("longitude")


def _create_order(data):
    try:
        items = _order_items(data)
        phone = _normalize_phone(data.get("phone"))
        if not phone:
            raise ValueError("Enter a valid Kenyan mobile number.")
        order_type, address, postal_code, verified, latitude, longitude = _delivery_details(data)
    except ValueError as error:
        return None, (jsonify({"error": str(error)}), 400)

    amount = sum(item["price"] for item in items)
    order_number = _order_number()
    customer_name = session.get("customer_name", "Guest")
    status = "Awaiting Till Payment — verify manually"
    query_db(
        """
        INSERT INTO orders (
            order_number, customer_name, customer_phone, items, order_type,
            delivery_address, delivery_postal_code, address_verified,
            destination_latitude, destination_longitude, delivery_time, amount,
            status, status_updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            order_number,
            customer_name,
            phone,
            json.dumps(items),
            order_type,
            address,
            postal_code,
            verified,
            latitude,
            longitude,
            "ASAP",
            amount,
            status,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return {
        "order_number": order_number,
        "phone": phone,
        "amount": amount,
        "items": items,
        "order_type": order_type,
    }, None


def _is_admin():
    return session.get("is_admin") is True


def _json_data():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _manual_payment_response(order):
    order["checkout_request_id"] = f"MANUAL-{order['order_number']}"
    query_db(
        "UPDATE orders SET checkout_request_id = %s WHERE order_number = %s",
        (order["checkout_request_id"], order["order_number"]),
    )
    return jsonify({
        "ResponseCode": "0",
        "ResponseDescription": "Order saved; complete payment at the hotel till.",
        "CustomerMessage": "M-Pesa prompt is unavailable. Use the hotel Till to pay, then keep your order number.",
        "CheckoutRequestID": order["checkout_request_id"],
        "OrderNumber": order["order_number"],
        "Fallback": True,
        "TillNumber": os.getenv("MPESA_TILL_NUMBER", "3484738"),
    })


@app.route("/", endpoint="index")
def home():
    session.setdefault("cart_id", secrets.token_urlsafe(12))
    return render_template("index.html", menu_items=MENU_ITEMS)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        csrf_token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
        return render_template("register.html", csrf_token=csrf_token)

    fullname = str(request.form.get("fullname") or "").strip()
    phone = _normalize_phone(request.form.get("phone"))
    if not hmac.compare_digest(str(request.form.get("csrf_token") or ""), session.get("csrf_token", "")):
        return render_template("register.html", error="Please refresh the page and try again.", csrf_token=session.get("csrf_token", ""))
    if len(fullname) < 2 or len(fullname) > 80 or not phone:
        return render_template(
            "register.html",
            error="Enter your name and a valid Kenyan mobile number.",
            fullname=fullname,
            phone=request.form.get("phone", ""),
            csrf_token=session["csrf_token"],
        ), 400

    existing = query_db("SELECT id FROM users WHERE phone = %s", (phone,), one=True)
    if not existing:
        query_db("INSERT INTO users (fullname, phone) VALUES (%s, %s)", (fullname, phone))
    else:
        query_db("UPDATE users SET fullname = %s WHERE phone = %s", (fullname, phone))
    session["customer_name"] = fullname
    session["customer_phone"] = phone
    session.pop("csrf_token", None)
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        configured_phone = _normalize_phone(os.getenv("ADMIN_PHONE"))
        submitted_phone = _normalize_phone(request.form.get("phone"))
        configured_password = os.getenv("ADMIN_PASSWORD")
        submitted_password = str(request.form.get("admin_password") or "")
        if (
            configured_phone
            and configured_password
            and submitted_phone == configured_phone
            and hmac.compare_digest(submitted_password, configured_password)
        ):
            session.clear()
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("login.html", error="Staff sign in is unavailable or the credentials are incorrect."), 401
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/cart")
def cart():
    session.setdefault("cart_id", secrets.token_urlsafe(12))
    return render_template("cart.html")


@app.route("/admin")
def admin_dashboard():
    if not _is_admin():
        return redirect(url_for("login"))
    orders = query_db("SELECT * FROM orders ORDER BY id DESC")
    for order in orders:
        try:
            order["items"] = ", ".join(
                f"{item['name']} × {item.get('quantity', 1)}"
                for item in json.loads(order.get("items") or "[]")
            )
        except (TypeError, ValueError, KeyError):
            app.logger.exception("Could not decode items for order %s", order.get("order_number"))
            order["items"] = "Order details unavailable"
    return render_template("admin.html", orders=orders)


@app.route("/admin/order/<int:order_id>/status", methods=["POST"], endpoint="update_order_status")
def update_order_status(order_id):
    if not _is_admin():
        return redirect(url_for("login"))
    requested_status = request.form.get("status")
    if requested_status not in ORDER_STATUSES:
        return "Invalid order status.", 400

    order = query_db("SELECT status FROM orders WHERE id = %s", (order_id,), one=True)
    if not order:
        return "Order not found.", 404
    current = order["status"]
    allowed = {
        "Awaiting Till Payment — verify manually": {"Payment Verified"},
        "Pending": {"Preparing", "Payment Verified"},
        "Payment Prompt Sent": {"Preparing", "Payment Verified"},
        "Paid": {"Preparing"},
        "Payment Verified": {"Preparing"},
        "Preparing": {"Out for Delivery"},
        "Out for Delivery": {"Delivered"},
        "Delivered": set(),
    }
    if requested_status not in allowed.get(current, set()):
        return "This order cannot move to the requested status.", 409
    query_db(
        "UPDATE orders SET status = %s, status_updated_at = %s WHERE id = %s",
        (requested_status, datetime.now(timezone.utc).isoformat(), order_id),
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/api/review", methods=["POST"])
def add_review():
    data = _json_data()
    if data is None:
        return jsonify({"error": "A JSON review is required."}), 400
    name = str(data.get("name") or "").strip()
    text = str(data.get("text") or "").strip()
    if not name or not text or len(name) > 80 or len(text) > 1000:
        return jsonify({"error": "Enter your name and a review of 1,000 characters or fewer."}), 400
    query_db("INSERT INTO reviews (name, review) VALUES (%s, %s)", (name, text))
    return jsonify({"status": "success"}), 201


@app.route("/api/address/verify", methods=["POST"])
def verify_address():
    data = _json_data()
    if data is None:
        return jsonify({"error": "A JSON address is required."}), 400
    address_parts = [
        str(data.get(key) or "").strip()
        for key in ("delivery_address", "delivery_locality", "delivery_region", "delivery_postal_code", "delivery_country")
    ]
    street, locality, region, postal_code, country = address_parts
    if not street or not locality or not postal_code or not country:
        return jsonify({"error": "Enter a street, town or city, postal code, and country."}), 400
    query = ", ".join(part for part in (street, postal_code) if part)
    try:
        response = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "jsonv2", "addressdetails": 1, "limit": 1},
            headers={"User-Agent": "ViorHotel/1.0 (delivery address verification)"},
            timeout=8,
        )
        response.raise_for_status()
        matches = response.json()
    except (requests.RequestException, ValueError) as error:
        app.logger.warning("Address verification service failed: %s", error)
        return jsonify({"error": "Address verification is temporarily unavailable. Please try again."}), 503

    normalized_postal = re.sub(r"\s+", "", postal_code).casefold()
    if (
        not isinstance(matches, list)
        or not matches
        or not isinstance(matches[0], dict)
        or not isinstance(matches[0].get("address"), dict)
        or re.sub(r"\s+", "", str(matches[0]["address"].get("postcode", ""))).casefold() != normalized_postal
    ):
        return jsonify({"error": "We could not confirm that address and postal code. Please check the details."}), 422

    match = matches[0]
    verified_address = street
    try:
        latitude = float(match["lat"])
        longitude = float(match["lon"])
    except (KeyError, TypeError, ValueError):
        app.logger.warning("Address verification returned invalid coordinates.")
        return jsonify({"error": "Address verification returned an invalid location. Please try again."}), 503
    token = _address_signer().dumps({
        "address": verified_address,
        "postal_code": postal_code,
        "latitude": latitude,
        "longitude": longitude,
    })
    return jsonify({
        "verified": True,
        "address": verified_address,
        "postal_code": postal_code,
        "latitude": latitude,
        "longitude": longitude,
        "verification_token": token,
    })


@app.route("/api/mpesa/pay", methods=["POST"])
def mpesa_pay():
    data = _json_data()
    if data is None:
        return jsonify({"error": "A JSON order is required."}), 400
    order, error_response = _create_order(data)
    if error_response:
        return error_response

    consumer_key = os.getenv("MPESA_CONSUMER_KEY")
    consumer_secret = os.getenv("MPESA_CONSUMER_SECRET")
    shortcode = os.getenv("MPESA_SHORTCODE")
    passkey = os.getenv("MPESA_PASSKEY")
    callback_url = os.getenv("CALLBACK_URL")
    if not all((consumer_key, consumer_secret, shortcode, passkey, callback_url)):
        app.logger.warning("M-Pesa credentials or callback URL are missing; returning manual till payment instructions.")
        return _manual_payment_response(order)

    api_url = (
        "https://api.safaricom.co.ke"
        if os.getenv("MPESA_ENVIRONMENT", "sandbox").casefold() == "production"
        else "https://sandbox.safaricom.co.ke"
    )
    try:
        auth_response = requests.get(
            f"{api_url}/oauth/v1/generate?grant_type=client_credentials",
            auth=(consumer_key, consumer_secret),
            timeout=10,
        )
        auth_response.raise_for_status()
        access_token = auth_response.json()["access_token"]
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
        response = requests.post(
            f"{api_url}/mpesa/stkpush/v1/processrequest",
            json={
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": order["amount"],
                "PartyA": order["phone"],
                "PartyB": shortcode,
                "PhoneNumber": order["phone"],
                "CallBackURL": callback_url,
                "AccountReference": order["order_number"],
                "TransactionDesc": "Vior Hotel Order",
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        response.raise_for_status()
        payment = response.json()
        if payment.get("ResponseCode") != "0" or not payment.get("CheckoutRequestID"):
            app.logger.warning("M-Pesa rejected checkout for order %s: %s", order["order_number"], payment)
            return _manual_payment_response(order)
    except (requests.RequestException, ValueError, KeyError) as error:
        app.logger.warning("M-Pesa checkout failed for order %s: %s", order["order_number"], error)
        return _manual_payment_response(order)
    except Exception as error:
        app.logger.warning("M-Pesa authentication failed for order %s: %s", order["order_number"], error)
        return _manual_payment_response(order)

    query_db(
        "UPDATE orders SET status = %s, checkout_request_id = %s WHERE order_number = %s",
        ("Payment Prompt Sent", payment["CheckoutRequestID"], order["order_number"]),
    )
    payment["OrderNumber"] = order["order_number"]
    payment["Fallback"] = False
    return jsonify(payment)


@app.route("/api/mpesa/till-order", methods=["POST"])
def mpesa_till_order():
    data = _json_data()
    if data is None:
        return jsonify({"error": "A JSON order is required."}), 400
    order, error_response = _create_order(data)
    if error_response:
        return error_response
    return jsonify({
        "order_number": order["order_number"],
        "amount": order["amount"],
        "till_number": os.getenv("MPESA_TILL_NUMBER", "3484738"),
    }), 201


@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    data = _json_data()
    if data is None:
        return jsonify({"ResultCode": 1, "ResultDesc": "Invalid callback payload"}), 400
    try:
        callback = data["Body"]["stkCallback"]
        checkout_id = callback["CheckoutRequestID"]
        result_code = callback["ResultCode"]
    except (KeyError, TypeError):
        return jsonify({"ResultCode": 1, "ResultDesc": "Invalid callback payload"}), 400
    order = query_db("SELECT id FROM orders WHERE checkout_request_id = %s", (checkout_id,), one=True)
    if order and result_code == 0:
        query_db(
            "UPDATE orders SET status = %s, status_updated_at = %s WHERE id = %s",
            ("Paid", datetime.now(timezone.utc).isoformat(), order["id"]),
        )
    elif order:
        query_db(
            "UPDATE orders SET status = %s, status_updated_at = %s WHERE id = %s",
            ("Payment Failed", datetime.now(timezone.utc).isoformat(), order["id"]),
        )
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route("/api/order/status/<order_number>")
def order_status(order_number):
    order = query_db(
        "SELECT order_number, status, status_updated_at, address_verified, delivery_postal_code, "
        "destination_latitude, destination_longitude, driver_latitude, driver_longitude, "
        "driver_accuracy, driver_updated_at FROM orders WHERE order_number = %s",
        (order_number,),
        one=True,
    )
    if not order:
        return jsonify({"error": "Order not found."}), 404
    raw_status = order["status"]
    visible_status = "Pending" if raw_status.startswith("Awaiting") or raw_status in {
        "Payment Prompt Sent", "Payment Failed", "Payment Verified", "Paid",
    } else raw_status
    return jsonify({
        "order_number": order["order_number"],
        "status": visible_status,
        "raw_status": raw_status,
        "status_updated_at": order["status_updated_at"],
        "destination": {
            "verified": bool(order["address_verified"]),
            "postal_code": order["delivery_postal_code"],
            "latitude": order["destination_latitude"],
            "longitude": order["destination_longitude"],
        } if order["address_verified"] else None,
        "driver": {
            "latitude": order["driver_latitude"],
            "longitude": order["driver_longitude"],
            "accuracy": order["driver_accuracy"],
            "updated_at": order["driver_updated_at"],
        } if order["driver_latitude"] is not None else None,
    })


@app.route("/api/admin/order/<int:order_id>/location", methods=["POST"])
def update_driver_location(order_id):
    if not _is_admin():
        return jsonify({"error": "Staff sign in is required."}), 401
    data = _json_data()
    if data is None:
        return jsonify({"error": "A JSON location is required."}), 400
    try:
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
        accuracy = float(data.get("accuracy"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid GPS location is required."}), 400
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and accuracy >= 0):
        return jsonify({"error": "GPS coordinates are outside valid ranges."}), 400
    order = query_db("SELECT status FROM orders WHERE id = %s", (order_id,), one=True)
    if not order or order["status"] != "Out for Delivery":
        return jsonify({"error": "Only orders out for delivery can share a location."}), 409
    query_db(
        "UPDATE orders SET driver_latitude = %s, driver_longitude = %s, "
        "driver_accuracy = %s, driver_updated_at = %s WHERE id = %s",
        (latitude, longitude, accuracy, datetime.now(timezone.utc).isoformat(), order_id),
    )
    return jsonify({"status": "success"})


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
