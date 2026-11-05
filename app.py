import hmac
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from database import execute_transaction, query_db

load_dotenv()

app = Flask(__name__)
_secret_key = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")
if not _secret_key and os.getenv("VERCEL") == "1":
    raise RuntimeError("Set SECRET_KEY in the deployment environment.")
if os.getenv("VERCEL") == "1" and not os.getenv("DATABASE_URL"):
    raise RuntimeError("Set DATABASE_URL to durable PostgreSQL storage in the deployment environment.")
app.secret_key = _secret_key or secrets.token_hex(32)
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
ORDER_STATUSES = {
    "Pending", "Payment Verified", "Payment Reversed", "Preparing", "Out for Delivery", "Delivered",
}
ADDRESS_TOKEN_SALT = "vior-delivery-address"
OTP_TTL = timedelta(minutes=5)
OTP_MAX_ATTEMPTS = 5
OTP_PHONE_PER_MINUTE_LIMIT = 1
OTP_PHONE_PER_HOUR_LIMIT = 5
OTP_IP_PER_HOUR_LIMIT = 20


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
    return f"VH-{datetime.now(timezone.utc):%Y%m%d%H%M%S}{secrets.token_hex(16).upper()}"


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


def _validated_order(data):
    try:
        items = _order_items(data)
        phone = _normalize_phone(data.get("phone"))
        if not phone:
            raise ValueError("Enter a valid Kenyan mobile number.")
        order_type, address, postal_code, verified, latitude, longitude = _delivery_details(data)
    except ValueError as error:
        return None, (jsonify({"error": str(error)}), 400)
    return {
        "items": items,
        "phone": phone,
        "order_type": order_type,
        "address": address,
        "postal_code": postal_code,
        "address_verified": verified,
        "latitude": latitude,
        "longitude": longitude,
    }, None


def _create_order(data, idempotency_key=None):
    validated, error_response = _validated_order(data)
    if error_response:
        return None, error_response
    items = validated["items"]
    phone = validated["phone"]
    order_type = validated["order_type"]
    address = validated["address"]
    postal_code = validated["postal_code"]
    verified = validated["address_verified"]
    latitude = validated["latitude"]
    longitude = validated["longitude"]

    amount = sum(item["price"] for item in items)
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9._:-]{16,128}", idempotency_key
        ):
            return None, (jsonify({"error": "A valid checkout idempotency key is required."}), 400)
        existing = query_db(
            "SELECT * FROM orders WHERE idempotency_key = %s",
            (idempotency_key,),
            one=True,
        )
        if existing:
            try:
                same_items = json.loads(existing["items"] or "[]") == items
            except (TypeError, ValueError):
                same_items = False
            if (
                same_items
                and existing["customer_phone"] == phone
                and existing["order_type"] == order_type
                and existing["delivery_address"] == address
                and existing["delivery_postal_code"] == postal_code
                and existing["amount"] == amount
            ):
                return {
                    "order_number": existing["order_number"],
                    "phone": phone,
                    "amount": amount,
                    "items": items,
                    "order_type": order_type,
                    "delivery_address": address,
                    "delivery_postal_code": postal_code,
                    "customer_name": existing["customer_name"] or "Guest",
                    "status": existing["status"],
                    "existing": True,
                }, None
            return None, (
                jsonify({"error": "This checkout key was already used for a different order."}),
                409,
            )

    order_number = _order_number()
    customer_name = session.get("customer_name", "Guest")
    status = "Awaiting Till Payment — verify manually"
    query_db(
        """
        INSERT INTO orders (
            order_number, idempotency_key, customer_name, customer_phone, items, order_type,
            delivery_address, delivery_postal_code, address_verified,
            destination_latitude, destination_longitude, delivery_time, amount,
            status, status_updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            order_number,
            idempotency_key,
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
        "delivery_address": address,
        "delivery_postal_code": postal_code,
        "customer_name": customer_name,
        "status": status,
        "existing": False,
    }, None


def _is_admin():
    return session.get("is_admin") is True


def _valid_csrf_token():
    csrf_token = session.get("csrf_token")
    return bool(csrf_token) and hmac.compare_digest(
        str(request.form.get("csrf_token") or ""), csrf_token
    )


def _otp_code_hash(challenge_id, phone, code):
    value = f"{challenge_id}:{phone}:{code}".encode()
    return hmac.new(app.secret_key.encode(), value, "sha256").hexdigest()


def _send_customer_otp(phone, purpose, fullname=None):
    username = os.getenv("AFRICASTALKING_USERNAME")
    api_key = os.getenv("AFRICASTALKING_API_KEY")
    if not username or not api_key:
        return "Phone verification is not configured. Please contact the hotel.", 503

    now = datetime.now(timezone.utc)
    day_ago = (now - timedelta(days=1)).isoformat()
    hour_ago = (now - timedelta(hours=1)).isoformat()
    minute_ago = (now - timedelta(minutes=1)).isoformat()
    query_db("DELETE FROM otp_challenges WHERE created_at < %s", (day_ago,))
    phone_hour = query_db(
        "SELECT COUNT(*) AS total FROM otp_challenges WHERE phone = %s AND created_at >= %s",
        (phone, hour_ago),
        one=True,
    )
    phone_minute = query_db(
        "SELECT COUNT(*) AS total FROM otp_challenges WHERE phone = %s AND created_at >= %s",
        (phone, minute_ago),
        one=True,
    )
    ip_hour = query_db(
        "SELECT COUNT(*) AS total FROM otp_challenges WHERE request_ip = %s AND created_at >= %s",
        (request.remote_addr or "unknown", hour_ago),
        one=True,
    )
    if (
        phone_minute["total"] >= OTP_PHONE_PER_MINUTE_LIMIT
        or phone_hour["total"] >= OTP_PHONE_PER_HOUR_LIMIT
        or ip_hour["total"] >= OTP_IP_PER_HOUR_LIMIT
    ):
        return "Too many verification attempts. Please wait before requesting another code.", 429

    query_db(
        "UPDATE otp_challenges SET consumed_at = %s "
        "WHERE phone = %s AND purpose = %s AND consumed_at IS NULL",
        (now.isoformat(), phone, purpose),
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = secrets.token_urlsafe(24)
    expires_at = (now + OTP_TTL).isoformat()
    query_db(
        """
        INSERT INTO otp_challenges (
            id, phone, fullname, purpose, code_hash, created_at, expires_at, request_ip
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            challenge_id,
            phone,
            fullname,
            purpose,
            _otp_code_hash(challenge_id, phone, code),
            now.isoformat(),
            expires_at,
            request.remote_addr or "unknown",
        ),
    )

    message = f"Your Vior Hotel verification code is {code}. It expires in 5 minutes."
    sms_data = {"username": username, "to": f"+{phone}", "message": message}
    sender_id = os.getenv("AFRICASTALKING_SENDER_ID")
    if sender_id:
        sms_data["from"] = sender_id
    try:
        response = requests.post(
            "https://api.africastalking.com/version1/messaging",
            headers={"apiKey": api_key, "Accept": "application/json"},
            data=sms_data,
            timeout=10,
        )
        response.raise_for_status()
        recipients = response.json().get("SMSMessageData", {}).get("Recipients", [])
    except (requests.RequestException, ValueError, AttributeError) as error:
        query_db(
            "UPDATE otp_challenges SET consumed_at = %s WHERE id = %s",
            (datetime.now(timezone.utc).isoformat(), challenge_id),
        )
        app.logger.warning("Africa's Talking OTP request failed: %s", error)
        return "Could not send a verification code right now. Please try again later.", 503

    if not isinstance(recipients, list) or not any(
        isinstance(recipient, dict)
        and str(recipient.get("statusCode")) == "101"
        for recipient in recipients
    ):
        query_db(
            "UPDATE otp_challenges SET consumed_at = %s WHERE id = %s",
            (datetime.now(timezone.utc).isoformat(), challenge_id),
        )
        app.logger.warning("Africa's Talking did not accept the OTP message.")
        return "Could not send a verification code right now. Please try again later.", 503

    previous_challenge = session.get("pending_otp")
    if isinstance(previous_challenge, dict) and previous_challenge.get("id"):
        query_db(
            "UPDATE otp_challenges SET consumed_at = %s "
            "WHERE id = %s AND consumed_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), previous_challenge["id"]),
        )
    session["pending_otp"] = {
        "id": challenge_id,
        "phone": phone,
        "purpose": purpose,
    }
    return None


def _verify_customer_otp(code, purpose):
    pending = session.get("pending_otp")
    if not isinstance(pending, dict) or pending.get("purpose") != purpose:
        return None, ("Request a verification code first.", 400)
    challenge_id = pending.get("id")
    phone = pending.get("phone")
    if not isinstance(challenge_id, str) or not isinstance(phone, str):
        return None, ("Request a new verification code.", 400)

    challenge = query_db(
        "SELECT * FROM otp_challenges WHERE id = %s AND phone = %s AND purpose = %s",
        (challenge_id, phone, purpose),
        one=True,
    )
    if not challenge or challenge["consumed_at"]:
        return None, ("This verification code is no longer valid. Request a new one.", 400)
    try:
        expires_at = datetime.fromisoformat(challenge["expires_at"])
    except (TypeError, ValueError):
        app.logger.error("Invalid OTP expiry for challenge %s", challenge_id)
        return None, ("This verification code is no longer valid. Request a new one.", 400)
    now = datetime.now(timezone.utc)
    if now >= expires_at:
        return None, ("This verification code has expired. Request a new one.", 400)
    if challenge["attempts"] >= OTP_MAX_ATTEMPTS:
        return None, ("Too many incorrect codes. Request a new one.", 429)

    valid_format = isinstance(code, str) and re.fullmatch(r"\d{6}", code)
    valid_code = valid_format and hmac.compare_digest(
        challenge["code_hash"], _otp_code_hash(challenge_id, phone, code)
    )
    if not valid_code:
        updated = query_db(
            "UPDATE otp_challenges SET attempts = attempts + 1 "
            "WHERE id = %s AND consumed_at IS NULL AND attempts < %s",
            (challenge_id, OTP_MAX_ATTEMPTS),
        )
        if not updated:
            return None, ("Too many incorrect codes. Request a new one.", 429)
        return None, ("The verification code is incorrect.", 400)

    claimed = query_db(
        "UPDATE otp_challenges SET consumed_at = %s "
        "WHERE id = %s AND consumed_at IS NULL AND attempts < %s AND expires_at > %s",
        (now.isoformat(), challenge_id, OTP_MAX_ATTEMPTS, now.isoformat()),
    )
    if claimed != 1:
        return None, ("This verification code is no longer valid. Request a new one.", 400)

    if purpose == "register":
        query_db(
            """
            INSERT INTO users (fullname, phone, phone_verified_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (phone) DO UPDATE SET
                fullname = excluded.fullname,
                phone_verified_at = excluded.phone_verified_at
            """,
            (challenge["fullname"], phone, now.isoformat()),
        )
    else:
        query_db(
            "UPDATE users SET phone_verified_at = %s WHERE phone = %s",
            (now.isoformat(), phone),
        )
    user = query_db("SELECT id, fullname, phone FROM users WHERE phone = %s", (phone,), one=True)
    if not user:
        return None, ("This customer account is no longer available.", 404)

    session.clear()
    session["customer_id"] = user["id"]
    session["customer_name"] = user["fullname"]
    session["customer_phone"] = user["phone"]
    return user, None


def _pesapal_base_url():
    environment = os.getenv("PESAPAL_ENVIRONMENT", "sandbox").casefold()
    if environment == "sandbox":
        return "https://cybqa.pesapal.com/pesapalv3"
    if environment == "live":
        return "https://pay.pesapal.com/v3"
    return None


def _pesapal_request_token():
    base_url = _pesapal_base_url()
    consumer_key = os.getenv("PESAPAL_CONSUMER_KEY")
    consumer_secret = os.getenv("PESAPAL_CONSUMER_SECRET")
    if not base_url or not consumer_key or not consumer_secret:
        return None, ("Pesapal is not configured. Please use the Till payment option.", 503)
    try:
        response = requests.post(
            f"{base_url}/api/Auth/RequestToken",
            json={"consumer_key": consumer_key, "consumer_secret": consumer_secret},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        token = response.json().get("token")
    except (requests.RequestException, ValueError, AttributeError) as error:
        app.logger.warning("Pesapal authentication failed: %s", error)
        return None, ("Pesapal could not be reached. Please try Till payment or try again later.", 503)
    if not isinstance(token, str) or not token:
        app.logger.warning("Pesapal authentication did not return an access token.")
        return None, ("Pesapal could not authenticate this payment. Please use Till payment.", 503)
    return (base_url, token), None


def _pesapal_payment_status(order_tracking_id, merchant_reference):
    payment = query_db(
        "SELECT * FROM payments WHERE provider = %s AND merchant_reference = %s",
        ("pesapal", merchant_reference),
        one=True,
    )
    if not payment or payment["order_tracking_id"] != order_tracking_id:
        return None, ("Payment reference was not found.", 404)

    credentials, error = _pesapal_request_token()
    if error:
        return None, error
    base_url, token = credentials
    try:
        response = requests.get(
            f"{base_url}/api/Transactions/GetTransactionStatus",
            params={"orderTrackingId": order_tracking_id},
            headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        provider_status = response.json()
    except (requests.RequestException, ValueError, AttributeError) as exception:
        app.logger.warning("Pesapal status lookup failed for %s: %s", merchant_reference, exception)
        return None, ("Payment status is temporarily unavailable.", 503)

    if not isinstance(provider_status, dict) or str(provider_status.get("status")) != "200":
        app.logger.warning("Pesapal returned an invalid status response for %s.", merchant_reference)
        return None, ("Payment status could not be verified.", 503)

    description = str(provider_status.get("payment_status_description") or "Pending").strip()
    normalized_status = description.casefold()
    now = datetime.now(timezone.utc).isoformat()
    if normalized_status == "completed":
        try:
            reported_amount = Decimal(str(provider_status["amount"]))
            expected_amount = Decimal(str(payment["amount"]))
        except (KeyError, InvalidOperation, TypeError, ValueError):
            reported_amount = None
            expected_amount = None
        currency = provider_status.get("currency")
        receipt = str(provider_status.get("confirmation_code") or "").strip()
        reference = provider_status.get("merchant_reference")
        if (
            reported_amount != expected_amount
            or (currency and str(currency).upper() != payment["currency"])
            or (reference and str(reference) != merchant_reference)
            or not receipt
        ):
            query_db(
                "UPDATE payments SET status = %s, provider_status = %s, updated_at = %s "
                "WHERE id = %s",
                ("Review Required", description[:200], now, payment["id"]),
            )
            app.logger.error("Pesapal payment verification mismatch for %s.", merchant_reference)
            return {"status": "Review Required"}, None
        status = "Completed"
        order_update = (
            """
            UPDATE orders SET status = %s, status_updated_at = %s
            WHERE order_number = %s
              AND status IN (
                  'Awaiting Till Payment — verify manually',
                  'Payment Prompt Sent', 'Payment Failed', 'Pending'
              )
            """,
            ("Paid", now, merchant_reference),
        )
    elif normalized_status in {"failed", "cancelled", "canceled"}:
        status = "Failed"
        order_update = (
            "UPDATE orders SET status = %s, status_updated_at = %s "
            "WHERE order_number = %s AND status IN ('Payment Prompt Sent', 'Pending')",
            ("Payment Failed", now, merchant_reference),
        )
    elif normalized_status == "reversed":
        status = "Reversed"
        order_update = (
            "UPDATE orders SET status = %s, status_updated_at = %s "
            "WHERE order_number = %s AND status = %s",
            ("Payment Reversed", now, merchant_reference, "Paid"),
        )
    elif normalized_status in {"processing", "pending"}:
        status = "Pending" if normalized_status == "pending" else "Processing"
        order_update = None
    else:
        status = "Pending"
        order_update = None

    updates = [
        (
            """
            UPDATE payments SET status = %s, provider_status = %s, receipt = %s, updated_at = %s
            WHERE id = %s
            """,
            (
                status,
                description[:200],
                provider_status.get("confirmation_code"),
                now,
                payment["id"],
            ),
        )
    ]
    if order_update:
        updates.append(order_update)
    execute_transaction(updates)
    return {"status": status, "receipt": provider_status.get("confirmation_code")}, None


def _render_customer_auth(template, purpose, error=None, status=200, fullname="", phone=""):
    csrf_token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
    pending = session.get("pending_otp")
    otp_pending = isinstance(pending, dict) and pending.get("purpose") == purpose
    if otp_pending and not phone:
        phone = pending.get("phone", "")
    return render_template(
        template,
        csrf_token=csrf_token,
        otp_pending=otp_pending,
        error=error,
        fullname=fullname,
        phone=phone,
    ), status


def _json_data():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


@app.route("/", endpoint="index")
def home():
    session.setdefault("cart_id", secrets.token_urlsafe(12))
    return render_template("index.html", menu_items=MENU_ITEMS)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return _render_customer_auth("register.html", "register")
    if not _valid_csrf_token():
        return _render_customer_auth(
            "register.html", "register", "Please refresh the page and try again.", 400
        )

    action = request.form.get("action")
    if action == "verify":
        _, error = _verify_customer_otp(request.form.get("code"), "register")
        if error:
            return _render_customer_auth("register.html", "register", error[0], error[1])
        return redirect(url_for("index"))

    if action == "resend":
        pending = session.get("pending_otp")
        challenge = query_db(
            "SELECT phone, fullname FROM otp_challenges WHERE id = %s AND purpose = %s",
            (pending.get("id"), "register"),
            one=True,
        ) if isinstance(pending, dict) else None
        if not challenge:
            return _render_customer_auth(
                "register.html", "register", "Request a new verification code.", 400
            )
        error = _send_customer_otp(challenge["phone"], "register", challenge["fullname"])
        if error:
            return _render_customer_auth("register.html", "register", error[0], error[1])
        return redirect(url_for("register", otp="sent"))

    fullname = str(request.form.get("fullname") or "").strip()
    phone = _normalize_phone(request.form.get("phone"))
    if len(fullname) < 2 or len(fullname) > 80 or not phone:
        return _render_customer_auth(
            "register.html",
            "register",
            "Enter your name and a valid Kenyan mobile number.",
            400,
            fullname,
            request.form.get("phone", ""),
        )

    error = _send_customer_otp(phone, "register", fullname)
    if error:
        return _render_customer_auth(
            "register.html", "register", error[0], error[1], fullname, request.form.get("phone", "")
        )
    return redirect(url_for("register", otp="sent"))


@app.route("/customer/login", methods=["GET", "POST"])
def customer_login():
    if request.method == "GET":
        return _render_customer_auth("customer_login.html", "login")
    if not _valid_csrf_token():
        return _render_customer_auth(
            "customer_login.html", "login", "Please refresh the page and try again.", 400
        )

    action = request.form.get("action")
    if action == "verify":
        _, error = _verify_customer_otp(request.form.get("code"), "login")
        if error:
            return _render_customer_auth(
                "customer_login.html", "login", error[0], error[1]
            )
        return redirect(url_for("index"))

    if action == "resend":
        pending = session.get("pending_otp")
        challenge = query_db(
            "SELECT phone FROM otp_challenges WHERE id = %s AND purpose = %s",
            (pending.get("id"), "login"),
            one=True,
        ) if isinstance(pending, dict) else None
        if not challenge:
            return _render_customer_auth(
                "customer_login.html", "login", "Request a new verification code.", 400
            )
        error = _send_customer_otp(challenge["phone"], "login")
        if error:
            return _render_customer_auth("customer_login.html", "login", error[0], error[1])
        return redirect(url_for("customer_login", otp="sent"))

    phone = _normalize_phone(request.form.get("phone"))
    if not phone:
        return _render_customer_auth(
            "customer_login.html",
            "login",
            "Enter a valid Kenyan mobile number.",
            400,
            phone=request.form.get("phone", ""),
        )
    user = query_db("SELECT id FROM users WHERE phone = %s", (phone,), one=True)
    if not user:
        return _render_customer_auth(
            "customer_login.html",
            "login",
            "No customer account was found for that number. Please register first.",
            404,
            phone=request.form.get("phone", ""),
        )

    error = _send_customer_otp(phone, "login")
    if error:
        return _render_customer_auth(
            "customer_login.html", "login", error[0], error[1], phone=request.form.get("phone", "")
        )
    return redirect(url_for("customer_login", otp="sent"))


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
    csrf_token = session.setdefault("csrf_token", secrets.token_urlsafe(32))
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
    return render_template("admin.html", orders=orders, csrf_token=csrf_token)


@app.route("/admin/order/<int:order_id>/status", methods=["POST"], endpoint="update_order_status")
def update_order_status(order_id):
    if not _is_admin():
        return redirect(url_for("login"))
    csrf_token = session.get("csrf_token")
    if not csrf_token or not hmac.compare_digest(
        str(request.form.get("csrf_token") or ""), csrf_token
    ):
        return "Please refresh the page and try again.", 400
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
        "Payment Reversed": set(),
        "Paid": {"Preparing"},
        "Payment Verified": {"Preparing"},
        "Preparing": {"Out for Delivery"},
        "Out for Delivery": {"Delivered"},
        "Delivered": set(),
    }
    if requested_status not in allowed.get(current, set()):
        return "This order cannot move to the requested status.", 409
    now = datetime.now(timezone.utc).isoformat()
    updates = [
        (
            "UPDATE orders SET status = %s, status_updated_at = %s WHERE id = %s",
            (requested_status, now, order_id),
        )
    ]
    if current == "Awaiting Till Payment — verify manually" and requested_status == "Payment Verified":
        updates.append((
            "UPDATE payments SET status = %s, provider_status = %s, updated_at = %s "
            "WHERE order_number = (SELECT order_number FROM orders WHERE id = %s) "
            "AND provider = %s",
            ("Completed", "Verified by staff", now, order_id, "manual_till"),
        ))
    execute_transaction(updates)
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


@app.route("/api/pesapal/pay", methods=["POST"])
@app.route("/api/mpesa/pay", methods=["POST"])
def mpesa_pay():
    data = _json_data()
    if data is None:
        return jsonify({"error": "A JSON order is required."}), 400
    _, validation_error = _validated_order(data)
    if validation_error:
        return validation_error
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        return jsonify({"error": "A checkout idempotency key is required."}), 400
    callback_url = os.getenv("PESAPAL_CALLBACK_URL")
    notification_id = os.getenv("PESAPAL_NOTIFICATION_ID")
    base_url = _pesapal_base_url()
    parsed_callback = urlparse(callback_url or "")
    if (
        not base_url
        or not callback_url
        or parsed_callback.scheme != "https"
        or not parsed_callback.netloc
        or not notification_id
    ):
        return jsonify({
            "error": "Pesapal checkout is not configured. You can place an order with the hotel Till."
        }), 503

    credentials, error = _pesapal_request_token()
    if error:
        return jsonify({"error": error[0]}), error[1]

    order, error_response = _create_order(data, idempotency_key)
    if error_response:
        return error_response

    existing_payment = query_db(
        "SELECT * FROM payments WHERE order_number = %s ORDER BY id DESC",
        (order["order_number"],),
        one=True,
    )
    if existing_payment:
        if existing_payment["provider"] == "manual_till":
            if existing_payment["status"] == "Completed":
                return jsonify({"OrderNumber": order["order_number"], "AlreadyPaid": True})
            return jsonify({
                "error": "This order is already set up for Till payment. Complete that payment or place a new order.",
                "OrderNumber": order["order_number"],
            }), 409
        if existing_payment["status"] in {"Pending", "Processing"} and existing_payment["redirect_url"]:
            return jsonify({
                "OrderNumber": order["order_number"],
                "redirect_url": existing_payment["redirect_url"],
                "PaymentStatus": existing_payment["status"],
            })
        if existing_payment["status"] == "Completed":
            return jsonify({"OrderNumber": order["order_number"], "AlreadyPaid": True})
        return jsonify({
            "error": "The payment request already exists but is not ready to retry. Use Till payment or contact the hotel.",
            "OrderNumber": order["order_number"],
        }), 409

    now = datetime.now(timezone.utc).isoformat()
    query_db(
        """
        INSERT INTO payments (
            order_number, provider, merchant_reference, amount, currency, status,
            created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            order["order_number"],
            "pesapal",
            order["order_number"],
            order["amount"],
            "KES",
            "Initiating",
            now,
            now,
        ),
    )

    name_parts = str(order["customer_name"] or "Guest").split()
    first_name = name_parts[0] if name_parts else "Guest"
    last_name = " ".join(name_parts[1:])
    request_payload = {
        "id": order["order_number"],
        "currency": "KES",
        "amount": order["amount"],
        "description": f"Vior Hotel order {order['order_number']}",
        "callback_url": callback_url,
        "notification_id": notification_id,
        "terms_and_conditions_id": "",
        "language": "EN",
        "billing_address": {
            "phone_number": f"+{order['phone']}",
            "email_address": "",
            "country_code": "KE",
            "first_name": first_name,
            "middle_name": "",
            "last_name": last_name,
            "line_1": order["delivery_address"][:100],
            "line_2": "",
            "city": "",
            "state": "",
            "postal_code": order["delivery_postal_code"][:10],
            "zip_code": "",
        },
    }
    base_url, access_token = credentials
    try:
        response = requests.post(
            f"{base_url}/api/Transactions/SubmitOrderRequest",
            json=request_payload,
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        response.raise_for_status()
        pesapal_order = response.json()
    except (requests.RequestException, ValueError, AttributeError) as exception:
        app.logger.warning("Pesapal order submission failed for %s: %s", order["order_number"], exception)
        query_db(
            "UPDATE payments SET status = %s, updated_at = %s WHERE merchant_reference = %s",
            ("Initiation Unknown", datetime.now(timezone.utc).isoformat(), order["order_number"]),
        )
        return jsonify({
            "error": "Payment initiation could not be confirmed. Do not retry this checkout; use Till payment or contact the hotel.",
            "OrderNumber": order["order_number"],
        }), 503

    if not isinstance(pesapal_order, dict):
        pesapal_order = {}
    tracking_id = pesapal_order.get("order_tracking_id")
    redirect_url = pesapal_order.get("redirect_url")
    redirect_parts = urlparse(str(redirect_url or ""))
    allowed_hosts = {"cybqa.pesapal.com", "pay.pesapal.com"}
    returned_reference = pesapal_order.get("merchant_reference")
    if (
        pesapal_order.get("error")
        or not isinstance(tracking_id, str)
        or not tracking_id
        or (returned_reference and str(returned_reference) != order["order_number"])
        or redirect_parts.scheme != "https"
        or redirect_parts.hostname not in allowed_hosts
    ):
        app.logger.warning("Pesapal rejected order submission for %s.", order["order_number"])
        query_db(
            "UPDATE payments SET status = %s, updated_at = %s WHERE merchant_reference = %s",
            ("Initiation Failed", datetime.now(timezone.utc).isoformat(), order["order_number"]),
        )
        return jsonify({
            "error": "Pesapal did not accept the payment request. You can use Till payment instead.",
            "OrderNumber": order["order_number"],
        }), 502

    now = datetime.now(timezone.utc).isoformat()
    execute_transaction([
        (
            """
            UPDATE payments
            SET order_tracking_id = %s, redirect_url = %s, status = %s, updated_at = %s
            WHERE merchant_reference = %s
            """,
            (tracking_id, redirect_url, "Pending", now, order["order_number"]),
        ),
        (
            "UPDATE orders SET status = %s, checkout_request_id = %s, status_updated_at = %s "
            "WHERE order_number = %s",
            ("Payment Prompt Sent", tracking_id, now, order["order_number"]),
        ),
    ])
    return jsonify({
        "OrderNumber": order["order_number"],
        "redirect_url": redirect_url,
        "PaymentStatus": "Pending",
    })


@app.route("/api/mpesa/till-order", methods=["POST"])
def mpesa_till_order():
    data = _json_data()
    if data is None:
        return jsonify({"error": "A JSON order is required."}), 400
    _, validation_error = _validated_order(data)
    if validation_error:
        return validation_error
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        return jsonify({"error": "A checkout idempotency key is required."}), 400
    order, error_response = _create_order(data, idempotency_key)
    if error_response:
        return error_response
    existing_payment = query_db(
        "SELECT * FROM payments WHERE order_number = %s ORDER BY id DESC",
        (order["order_number"],),
        one=True,
    )
    if existing_payment:
        if existing_payment["provider"] == "manual_till":
            if existing_payment["status"] == "Completed":
                return jsonify({
                    "error": "This order is already paid.",
                    "order_number": order["order_number"],
                }), 409
            return jsonify({
                "order_number": order["order_number"],
                "amount": order["amount"],
                "till_number": os.getenv("MPESA_TILL_NUMBER", "3484738"),
            })
        if existing_payment["provider"] != "pesapal" or existing_payment["status"] not in {
            "Failed", "Initiation Failed",
        }:
            return jsonify({
                "error": "A payment is already in progress or requires review for this order.",
                "order_number": order["order_number"],
            }), 409
    if not existing_payment or existing_payment["provider"] == "pesapal":
        now = datetime.now(timezone.utc).isoformat()
        query_db(
            """
            INSERT INTO payments (
                order_number, provider, merchant_reference, amount, currency, status,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                order["order_number"],
                "manual_till",
                f"MANUAL-{order['order_number']}",
                order["amount"],
                "KES",
                "Awaiting Verification",
                now,
                now,
            ),
        )
    return jsonify({
        "order_number": order["order_number"],
        "amount": order["amount"],
        "till_number": os.getenv("MPESA_TILL_NUMBER", "3484738"),
    }), 200 if order["existing"] else 201


@app.route("/api/pesapal/ipn", methods=["GET"])
def pesapal_ipn():
    tracking_id = request.args.get("OrderTrackingId")
    merchant_reference = request.args.get("OrderMerchantReference")
    if not tracking_id or not merchant_reference:
        return jsonify({"error": "Invalid Pesapal notification."}), 400
    result, error = _pesapal_payment_status(tracking_id, merchant_reference)
    if error:
        return jsonify({"error": error[0]}), error[1]
    return jsonify({"status": result["status"]})


@app.route("/payment/return", methods=["GET"])
def pesapal_return():
    tracking_id = request.args.get("OrderTrackingId")
    merchant_reference = request.args.get("OrderMerchantReference")
    if not tracking_id or not merchant_reference:
        return redirect(url_for("cart", payment_check="unavailable"))
    result, error = _pesapal_payment_status(tracking_id, merchant_reference)
    if error:
        app.logger.warning("Could not verify Pesapal browser return for %s: %s", merchant_reference, error[0])
        return redirect(url_for("cart", order_number=merchant_reference, payment_check="pending"))
    return redirect(url_for(
        "cart",
        order_number=merchant_reference,
        payment_check=result["status"].casefold(),
    ))


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
