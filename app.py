import hmac
import hashlib
import io
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
from flask import Flask, Response, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from database import add_loyalty_points, execute_transaction, execute_transaction_checked, query_db

load_dotenv()

app = Flask(__name__)
_secret_key = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY")
if not _secret_key and os.getenv("VERCEL") == "1":
    raise RuntimeError("Set SECRET_KEY in the deployment environment.")
if os.getenv("VERCEL") == "1" and not os.getenv("DATABASE_URL"):
    raise RuntimeError("Set DATABASE_URL to durable PostgreSQL storage in the deployment environment.")
app.secret_key = _secret_key or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("VERCEL") == "1",
)

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
        if not isinstance(name, str):
            raise ValueError(f"{name or 'An item'} is not on the current menu.")
        added_item = query_db(
            "SELECT name, price FROM menu_items WHERE name = %s AND active = %s",
            (name, True),
            one=True,
        )
        if added_item:
            trusted_items.append({"name": added_item["name"], "price": int(added_item["price"])})
        elif name in MENU_PRICES:
            trusted_items.append({"name": name, "price": MENU_PRICES[name]})
        else:
            raise ValueError(f"{name or 'An item'} is not on the current menu.")
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
        instructions = str(data.get("special_instructions") or "").strip()
        if len(instructions) > 500:
            raise ValueError("Special instructions must be 500 characters or fewer.")
        scheduled_at = None
        requested_schedule = str(data.get("scheduled_at") or "").strip()
        if requested_schedule:
            scheduled = datetime.fromisoformat(requested_schedule.replace("Z", "+00:00"))
            if scheduled.tzinfo is None:
                raise ValueError("Choose a valid order date and time.")
            scheduled = scheduled.astimezone(timezone.utc)
            now = datetime.now(timezone.utc)
            if scheduled < now + timedelta(minutes=30) or scheduled > now + timedelta(days=30):
                raise ValueError("Schedule orders at least 30 minutes ahead and within the next 30 days.")
            scheduled_at = scheduled.isoformat()
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
        "special_instructions": instructions,
        "scheduled_at": scheduled_at,
        "whatsapp_opt_in": data.get("whatsapp_opt_in") is True,
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
    instructions = validated["special_instructions"]
    scheduled_at = validated["scheduled_at"]
    whatsapp_opt_in = validated["whatsapp_opt_in"]

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
                and (existing.get("special_instructions") or "") == instructions
                and existing.get("scheduled_at") == scheduled_at
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
    customer_id = session.get("customer_id")
    status = "Awaiting Till Payment — verify manually"
    query_db(
        """
        INSERT INTO orders (
            order_number, idempotency_key, customer_name, customer_phone, items, order_type,
            delivery_address, delivery_postal_code, address_verified,
            destination_latitude, destination_longitude, delivery_time, amount,
            status, status_updated_at, customer_id, special_instructions, scheduled_at,
            whatsapp_opt_in, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
            customer_id,
            instructions,
            scheduled_at,
            whatsapp_opt_in,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    if customer_id and whatsapp_opt_in:
        query_db("UPDATE users SET whatsapp_opt_in = %s WHERE id = %s", (True, customer_id))
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


ROLE_PERMISSIONS = {
    "owner": {"orders", "content", "inventory", "finance", "customers", "staff"},
    "manager": {"orders", "content", "inventory", "finance", "customers"},
    "accountant": {"finance", "customers"},
    "kitchen": {"orders", "inventory"},
    "delivery": {"orders", "delivery"},
}


def _has_permission(permission):
    return _is_admin() and permission in ROLE_PERMISSIONS.get(
        session.get("staff_role", "owner"), set()
    )


def _valid_csrf_token():
    csrf_token = session.get("csrf_token")
    return bool(csrf_token) and hmac.compare_digest(
        str(request.form.get("csrf_token") or ""), csrf_token
    )


def _cloudinary_credentials():
    return (
        os.getenv("CLOUDINARY_CLOUD_NAME"),
        os.getenv("CLOUDINARY_API_KEY"),
        os.getenv("CLOUDINARY_API_SECRET"),
    )


def _cloudinary_signature(params, api_secret):
    signed_values = "&".join(
        f"{key}={value}" for key, value in sorted(params.items()) if value is not None
    )
    return hashlib.sha1(f"{signed_values}{api_secret}".encode()).hexdigest()


def _valid_cloudinary_media_url(value, resource_type, section):
    cloud_name, _, _ = _cloudinary_credentials()
    parsed = urlparse(value)
    if not cloud_name or parsed.scheme != "https" or parsed.netloc != "res.cloudinary.com":
        return False
    expected_prefix = f"/{cloud_name}/{resource_type}/upload/vior-hotel/{section}/"
    return parsed.path.startswith(expected_prefix)


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
    user = query_db(
        "SELECT id, fullname, phone, preferred_language FROM users WHERE phone = %s",
        (phone,),
        one=True,
    )
    if not user:
        return None, ("This customer account is no longer available.", 404)

    session.clear()
    session["customer_id"] = user["id"]
    session["customer_name"] = user["fullname"]
    session["customer_phone"] = user["phone"]
    session["preferred_language"] = user.get("preferred_language") or "en"
    return user, None


def _pesapal_base_url():
    environment = os.getenv("PESAPAL_ENVIRONMENT", "sandbox").casefold()
    if environment == "sandbox":
        return "https://cybqa.pesapal.com/pesapalv3"
    if environment == "live":
        return "https://pay.pesapal.com/v3"


def _receipt_link(order_number):
    receipt_token = URLSafeTimedSerializer(app.secret_key, salt="vior-order-receipt").dumps(
        {"order_number": order_number}
    )
    base_url = os.getenv("PUBLIC_BASE_URL", "https://vior-hotel.vercel.app").rstrip("/")
    return f"{base_url}{url_for('customer_receipt', order_number=order_number, token=receipt_token)}"


def _queue_whatsapp_order_event(order_number, event_type, event_value=None):
    order = query_db(
        "SELECT * FROM orders WHERE order_number = %s",
        (order_number,),
        one=True,
    )
    if not order or not order.get("whatsapp_opt_in"):
        return None
    payment = query_db(
        "SELECT receipt FROM payments WHERE order_number = %s AND status = %s "
        "ORDER BY id DESC",
        (order_number, "Completed"),
        one=True,
    )
    if not payment:
        app.logger.warning("WhatsApp %s event was not sent before verified payment for order %s.", event_type, order_number)
        return None
    receipt_link = _receipt_link(order_number)
    if event_type == "receipt":
        template_name = os.getenv("WHATSAPP_RECEIPT_TEMPLATE", "vior_order_receipt")
        language_code = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en")
        item_summary = ", ".join(
            f"{item.get('quantity', 1)} x {item['name']}"
            for item in json.loads(order.get("items") or "[]")
        )
        body_parameters = [
            order_number,
            item_summary[:900],
            f"KES {order['amount']:.2f}",
            str((payment or {}).get("receipt") or "Verified"),
            receipt_link,
        ]
    else:
        template_name = os.getenv("WHATSAPP_STATUS_TEMPLATE", "")
        if not template_name:
            app.logger.warning("WhatsApp status template is not configured; skipped %s notification.", event_type)
            return None
        language_code = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en")
        body_parameters = [order_number, str(event_value or ""), receipt_link]

    now = datetime.now(timezone.utc).isoformat()
    idempotency_key = f"whatsapp:{event_type}:{order_number}:{event_value or ''}"
    payload = json.dumps({"body_parameters": body_parameters})
    return query_db(
        """
        INSERT INTO notification_outbox (
            idempotency_key, channel, recipient, template_name, language_code,
            payload, status, attempts, next_attempt_at, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            idempotency_key, "whatsapp", order["customer_phone"], template_name,
            language_code, payload, "Pending", 0, now, now,
        ),
    )


def _dispatch_whatsapp_notification(notification_id):
    now = datetime.now(timezone.utc).isoformat()
    claimed = query_db(
        """
        UPDATE notification_outbox
        SET status = %s, attempts = attempts + 1, next_attempt_at = %s
        WHERE id = %s AND status IN (%s, %s) AND next_attempt_at <= %s
        """,
        ("Sending", now, notification_id, "Pending", "Retry", now),
    )
    if claimed != 1:
        return False
    notification = query_db(
        "SELECT * FROM notification_outbox WHERE id = %s",
        (notification_id,),
        one=True,
    )
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    if not access_token or not phone_number_id:
        error = "WhatsApp Cloud API credentials are not configured."
        retry_at = (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()
        query_db(
            "UPDATE notification_outbox SET status = %s, last_error = %s, next_attempt_at = %s WHERE id = %s",
            ("Retry", error, retry_at, notification_id),
        )
        app.logger.error("WhatsApp notification %s failed: %s", notification_id, error)
        return False

    try:
        content = json.loads(notification["payload"])
        response = requests.post(
            f"https://graph.facebook.com/{os.getenv('WHATSAPP_GRAPH_VERSION', 'v22.0')}/{phone_number_id}/messages",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "messaging_product": "whatsapp",
                "to": notification["recipient"],
                "type": "template",
                "biz_opaque_callback_data": notification["idempotency_key"],
                "template": {
                    "name": notification["template_name"],
                    "language": {"code": notification["language_code"]},
                    "components": [{
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": value}
                            for value in content["body_parameters"]
                        ],
                    }],
                },
            },
            timeout=12,
        )
        response.raise_for_status()
        provider_response = response.json()
        messages = provider_response.get("messages") if isinstance(provider_response, dict) else None
        message_id = messages[0].get("id") if messages and isinstance(messages[0], dict) else None
        if not message_id:
            raise ValueError("WhatsApp accepted no message identifier.")
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        attempts = int(notification["attempts"])
        retry = attempts < 8
        next_attempt = (
            datetime.now(timezone.utc) + timedelta(minutes=min(2 ** attempts, 360))
        ).isoformat()
        query_db(
            """
            UPDATE notification_outbox
            SET status = %s, last_error = %s, next_attempt_at = %s
            WHERE id = %s
            """,
            ("Retry" if retry else "Failed", str(error)[:500], next_attempt, notification_id),
        )
        app.logger.warning("WhatsApp delivery failed for outbox entry %s: %s", notification_id, error)
        return False

    query_db(
        """
        UPDATE notification_outbox
        SET status = %s, provider_message_id = %s, sent_at = %s, last_error = NULL
        WHERE id = %s
        """,
        ("Sent", message_id, datetime.now(timezone.utc).isoformat(), notification_id),
    )
    return True


def _deliver_order_whatsapp_event(order_number, event_type, event_value=None):
    inserted = _queue_whatsapp_order_event(order_number, event_type, event_value)
    if inserted == 1:
        entry = query_db(
            "SELECT id FROM notification_outbox WHERE idempotency_key = %s",
            (f"whatsapp:{event_type}:{order_number}:{event_value or ''}",),
            one=True,
        )
        if entry:
            _dispatch_whatsapp_notification(entry["id"])


def _record_verified_payment(order_number, amount, reference):
    now = datetime.now(timezone.utc).isoformat()
    query_db(
        """
        INSERT INTO financial_transactions
            (idempotency_key, transaction_type, category, amount, currency, reference, notes, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            f"payment:{order_number}", "income", "sales", amount, "KES", reference,
            "Verified payment", now,
        ),
    )
    customer = query_db(
        "SELECT customer_id FROM orders WHERE order_number = %s",
        (order_number,),
        one=True,
    )
    if not customer or not customer.get("customer_id"):
        return
    customer_id = customer["customer_id"]
    points = int(Decimal(str(amount)) // Decimal("100"))
    if points <= 0:
        return
    add_loyalty_points(customer_id, order_number, points, now)


def _inventory_deduction_statements(order):
    try:
        order_items = json.loads(order.get("items") or "[]")
    except (TypeError, ValueError):
        raise ValueError("Order items could not be read for inventory deduction.") from None
    requested = {}
    for item in order_items:
        name = item.get("name")
        if not isinstance(name, str):
            raise ValueError("Order contains an invalid menu item.")
        quantity = Decimal(str(item.get("quantity", 1)))
        if quantity <= 0 or quantity > 100:
            raise ValueError("Order contains an invalid item quantity.")
        recipes = query_db(
            "SELECT inventory_item_id, quantity_per_serving FROM inventory_recipes "
            "WHERE menu_item_name = %s",
            (name,),
        )
        for recipe in recipes:
            inventory_id = recipe["inventory_item_id"]
            used = Decimal(str(recipe["quantity_per_serving"])) * quantity
            requested[inventory_id] = requested.get(inventory_id, Decimal("0")) + used

    statements = []
    checked_indexes = {}
    now = datetime.now(timezone.utc).isoformat()
    for inventory_id, quantity in requested.items():
        statements.append((
            "UPDATE inventory_items SET quantity = quantity - %s, updated_at = %s "
            "WHERE id = %s AND quantity >= %s",
            (float(quantity), now, inventory_id, float(quantity)),
        ))
        checked_indexes[len(statements) - 1] = 1
        statements.append((
            """
            INSERT INTO inventory_movements
                (item_id, quantity, movement_type, reference, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                inventory_id, -float(quantity), "order_usage",
                order["order_number"], f"Ingredients used for {order['order_number']}", now,
            ),
        ))
    return statements, checked_indexes


def _write_audit(actor, action, entity_type, entity_id, details=None):
    query_db(
        """
        INSERT INTO audit_logs (actor, action, entity_type, entity_id, details, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            str(actor or "system")[:120], action[:80], entity_type[:80],
            str(entity_id)[:120],
            json.dumps(details, ensure_ascii=True) if details is not None else None,
            datetime.now(timezone.utc).isoformat(),
        ),
    )


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
    rowcounts = execute_transaction(updates)
    if status == "Completed" and order_update and rowcounts[-1] == 1:
        _record_verified_payment(
            merchant_reference,
            payment["amount"],
            str(provider_status.get("confirmation_code") or ""),
        )
        _deliver_order_whatsapp_event(merchant_reference, "receipt")
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
    added_menu_items = query_db(
        "SELECT id, name, description, category, price, media_url, media_type "
        "FROM menu_items WHERE active = %s ORDER BY id DESC",
        (True,),
    )
    menu_items = MENU_ITEMS + [
        {"name": item["name"], "price": item["price"]} for item in added_menu_items
    ]
    now = datetime.now(timezone.utc).isoformat()
    stories = query_db(
        """
        SELECT id, title, caption, media_url, media_type, button_text, button_url
        FROM stories
        WHERE published = %s
          AND (starts_at IS NULL OR starts_at <= %s)
          AND (ends_at IS NULL OR ends_at >= %s)
        ORDER BY id DESC
        """,
        (True, now, now),
    )
    return render_template(
        "index.html",
        language=(
            request.args.get("lang")
            if request.args.get("lang") in {"en", "sw"}
            else session.get("preferred_language", "en")
        ),
        menu_items=menu_items,
        added_menu_items=added_menu_items,
        stories=stories,
    )


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory("static", "service-worker.js", mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


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
        csrf_token = session.get("csrf_token")
        if not csrf_token or not _valid_csrf_token():
            return render_template(
                "login.html",
                error="Please refresh the page and try again.",
                csrf_token=session.setdefault("csrf_token", secrets.token_urlsafe(32)),
            ), 400
        request_ip = request.remote_addr or "unknown"
        now = datetime.now(timezone.utc)
        attempt_cutoff = (now - timedelta(minutes=15)).isoformat()
        query_db("DELETE FROM auth_attempts WHERE attempted_at < %s", ((now - timedelta(days=1)).isoformat(),))
        attempts = query_db(
            "SELECT COUNT(*) AS total FROM auth_attempts WHERE request_ip = %s AND attempted_at >= %s",
            (request_ip, attempt_cutoff),
            one=True,
        )
        if attempts["total"] >= 10:
            return render_template(
                "login.html",
                error="Too many sign-in attempts. Please wait 15 minutes before trying again.",
                csrf_token=session.get("csrf_token"),
            ), 429
        configured_phone = _normalize_phone(os.getenv("ADMIN_PHONE"))
        submitted_phone = _normalize_phone(request.form.get("phone"))
        configured_password = os.getenv("ADMIN_PASSWORD")
        submitted_password = str(request.form.get("admin_password") or "")
        primary_admin = bool(
            configured_phone
            and configured_password
            and submitted_phone == configured_phone
            and hmac.compare_digest(submitted_password, configured_password)
        )
        staff = query_db(
            "SELECT id, fullname, phone, password_hash, role FROM staff_accounts "
            "WHERE phone = %s AND active = %s",
            (submitted_phone, True),
            one=True,
        ) if submitted_phone else None
        staff_valid = bool(
            staff
            and staff["role"] in ROLE_PERMISSIONS
            and check_password_hash(staff["password_hash"], submitted_password)
        )
        if primary_admin or staff_valid:
            query_db("DELETE FROM auth_attempts WHERE request_ip = %s", (request_ip,))
            session.clear()
            session["is_admin"] = True
            session["staff_role"] = "owner" if primary_admin else staff["role"]
            session["staff_phone"] = submitted_phone
            session["staff_name"] = "Vior Administrator" if primary_admin else staff["fullname"]
            return redirect(url_for("admin_dashboard"))
        query_db(
            "INSERT INTO auth_attempts (request_ip, attempted_at) VALUES (%s, %s)",
            (request_ip, now.isoformat()),
        )
        return render_template(
            "login.html",
            error="Staff sign in is unavailable or the credentials are incorrect.",
            csrf_token=csrf_token,
        ), 401
    return render_template("login.html", csrf_token=session.setdefault("csrf_token", secrets.token_urlsafe(32)))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/account")
def customer_account():
    customer_id = session.get("customer_id")
    if not customer_id:
        return redirect(url_for("customer_login"))
    profile = query_db(
        "SELECT id, fullname, phone, email, preferred_language, whatsapp_opt_in FROM users WHERE id = %s",
        (customer_id,),
        one=True,
    )
    if not profile:
        session.clear()
        return redirect(url_for("customer_login"))
    orders = query_db(
        "SELECT order_number, items, order_type, amount, status, status_updated_at "
        "FROM orders WHERE customer_id = %s ORDER BY id DESC LIMIT 50",
        (customer_id,),
    )
    for order in orders:
        paid = query_db(
            "SELECT id FROM payments WHERE order_number = %s AND status = %s ORDER BY id DESC",
            (order["order_number"], "Completed"),
            one=True,
        )
        if paid:
            receipt_token = URLSafeTimedSerializer(
                app.secret_key, salt="vior-order-receipt"
            ).dumps({"order_number": order["order_number"]})
            order["receipt_url"] = url_for(
                "customer_receipt",
                order_number=order["order_number"],
                token=receipt_token,
            )
    loyalty = query_db(
        "SELECT points_balance FROM loyalty_accounts WHERE customer_id = %s",
        (customer_id,),
        one=True,
    )
    addresses = query_db(
        "SELECT * FROM customer_addresses WHERE customer_id = %s ORDER BY is_default DESC, id DESC",
        (customer_id,),
    )
    return render_template(
        "account.html",
        profile=profile,
        orders=orders,
        loyalty=loyalty,
        addresses=addresses,
        csrf_token=session.setdefault("csrf_token", secrets.token_urlsafe(32)),
    )


@app.route("/account/profile", methods=["POST"])
def update_customer_profile():
    customer_id = session.get("customer_id")
    if not customer_id:
        return redirect(url_for("customer_login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    email = str(request.form.get("email") or "").strip().casefold()
    language = str(request.form.get("preferred_language") or "en")
    whatsapp_opt_in = request.form.get("whatsapp_opt_in") == "on"
    if email and (
        len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
    ):
        return "Enter a valid email address.", 400
    if language not in {"en", "sw"}:
        return "Choose English or Kiswahili.", 400
    query_db(
        "UPDATE users SET email = %s, preferred_language = %s, whatsapp_opt_in = %s WHERE id = %s",
        (email or None, language, whatsapp_opt_in, customer_id),
    )
    session["preferred_language"] = language
    return redirect(url_for("customer_account"))


@app.route("/account/addresses", methods=["POST"])
def save_customer_address():
    customer_id = session.get("customer_id")
    if not customer_id:
        return redirect(url_for("customer_login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    label = str(request.form.get("label") or "").strip()
    address = str(request.form.get("address") or "").strip()
    locality = str(request.form.get("locality") or "").strip()
    region = str(request.form.get("region") or "").strip()
    postal_code = str(request.form.get("postal_code") or "").strip()
    country = str(request.form.get("country") or "").strip()
    if (
        len(label) < 1 or len(label) > 40 or len(address) > 250 or not address
        or len(locality) > 100 or not locality or len(region) > 100
        or len(postal_code) > 20 or not postal_code or len(country) > 80 or not country
    ):
        return "Check the address details and try again.", 400
    now = datetime.now(timezone.utc).isoformat()
    is_default = request.form.get("is_default") == "on"
    statements = []
    if is_default:
        statements.append(
            ("UPDATE customer_addresses SET is_default = %s WHERE customer_id = %s", (False, customer_id))
        )
    statements.append((
        """
        INSERT INTO customer_addresses
            (customer_id, label, address, locality, region, postal_code, country, is_default, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (customer_id, label, address, locality, region, postal_code, country, is_default, now),
    ))
    execute_transaction(statements)
    return redirect(url_for("customer_account"))


@app.route("/receipt/<order_number>")
def customer_receipt(order_number):
    token = request.args.get("token", "")
    try:
        receipt_claim = URLSafeTimedSerializer(
            app.secret_key, salt="vior-order-receipt"
        ).loads(token, max_age=30 * 24 * 60 * 60)
    except (BadSignature, SignatureExpired):
        return "This receipt link is invalid or has expired.", 403
    if not isinstance(receipt_claim, dict) or receipt_claim.get("order_number") != order_number:
        return "This receipt link is invalid.", 403
    order = query_db(
        "SELECT order_number, items, order_type, amount, status, delivery_time "
        "FROM orders WHERE order_number = %s",
        (order_number,),
        one=True,
    )
    payment = query_db(
        "SELECT provider, receipt, status, updated_at FROM payments "
        "WHERE order_number = %s AND status = %s ORDER BY id DESC",
        (order_number, "Completed"),
        one=True,
    )
    if not order or not payment:
        return "A verified receipt is not available for this order.", 404
    try:
        items = json.loads(order["items"] or "[]")
    except (TypeError, ValueError):
        app.logger.exception("Could not decode receipt items for order %s", order_number)
        return "Receipt details are temporarily unavailable.", 500
    return render_template(
        "receipt.html",
        order=order,
        payment=payment,
        items=items,
        item_total=sum(Decimal(str(item.get("price", 0))) for item in items),
        delivery_fee=Decimal("0"),
    )


@app.route("/api/cron/notifications", methods=["GET", "POST"])
def retry_notifications():
    cron_secret = os.getenv("CRON_SECRET")
    authorization = request.headers.get("Authorization", "")
    if not cron_secret or not hmac.compare_digest(authorization, f"Bearer {cron_secret}"):
        return jsonify({"error": "Not authorized."}), 401
    now = datetime.now(timezone.utc).isoformat()
    stale_before = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    query_db(
        "UPDATE notification_outbox SET status = %s, next_attempt_at = %s "
        "WHERE status = %s AND next_attempt_at < %s",
        ("Retry", now, "Sending", stale_before),
    )
    pending = query_db(
        "SELECT id FROM notification_outbox WHERE status IN (%s, %s) "
        "AND next_attempt_at <= %s ORDER BY id LIMIT 25",
        ("Pending", "Retry", now),
    )
    sent = sum(_dispatch_whatsapp_notification(entry["id"]) for entry in pending)
    return jsonify({"processed": len(pending), "sent": sent})


@app.route("/cart")
def cart():
    session.setdefault("cart_id", secrets.token_urlsafe(12))
    saved_addresses = query_db(
        "SELECT id, label, address, locality, region, postal_code, country "
        "FROM customer_addresses WHERE customer_id = %s ORDER BY is_default DESC, id DESC",
        (session["customer_id"],),
    ) if session.get("customer_id") else []
    return render_template(
        "cart.html",
        saved_addresses=saved_addresses,
        language=session.get("preferred_language", "en"),
    )


@app.route("/admin")
def admin_dashboard():
    if not _is_admin():
        return redirect(url_for("login"))
    role = session.get("staff_role", "owner")
    can_manage_orders = _has_permission("orders")
    can_manage_content = _has_permission("content")
    can_manage_inventory = _has_permission("inventory")
    can_view_finance = _has_permission("finance")
    can_manage_customers = _has_permission("customers")
    can_manage_staff = _has_permission("staff")
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
    menu_items = query_db("SELECT * FROM menu_items ORDER BY id DESC") if can_manage_content else []
    recipe_menu_items = MENU_ITEMS + menu_items if can_manage_inventory else []
    stories = query_db("SELECT * FROM stories ORDER BY id DESC") if can_manage_content else []
    inventory_items = query_db("SELECT * FROM inventory_items ORDER BY name") if can_manage_inventory else []
    suppliers = query_db("SELECT * FROM suppliers ORDER BY name") if can_manage_inventory else []
    recipes = query_db("SELECT * FROM inventory_recipes ORDER BY menu_item_name") if can_manage_inventory else []
    financial_transactions = query_db(
        "SELECT * FROM financial_transactions ORDER BY id DESC LIMIT 100"
    ) if can_view_finance else []
    delivery_assignments = query_db(
        "SELECT * FROM delivery_assignments ORDER BY id DESC LIMIT 50"
    ) if _has_permission("delivery") else []
    notification_entries = query_db(
        "SELECT id, recipient, template_name, status, attempts, last_error, created_at "
        "FROM notification_outbox ORDER BY id DESC LIMIT 50"
    ) if role in {"owner", "manager"} else []
    totals = query_db(
        """
        SELECT
            COALESCE(SUM(CASE WHEN transaction_type = %s THEN amount ELSE 0 END), 0) AS revenue,
            COALESCE(SUM(CASE WHEN transaction_type = %s THEN amount ELSE 0 END), 0) AS expenses,
            COALESCE(SUM(CASE WHEN transaction_type = %s THEN amount ELSE 0 END), 0) AS refunds
        FROM financial_transactions
        """,
        ("income", "expense", "refund"),
        one=True,
    ) if can_view_finance else {"revenue": 0, "expenses": 0, "refunds": 0}
    staff_accounts = query_db(
        "SELECT id, fullname, phone, role, active, created_at FROM staff_accounts ORDER BY id DESC"
    ) if can_manage_staff else []
    customers = query_db(
        """
        SELECT u.id, u.fullname, u.phone, u.email, u.preferred_language,
            COALESCE(l.points_balance, 0) AS points_balance,
            (SELECT COUNT(*) FROM orders o WHERE o.customer_id = u.id) AS order_count
        FROM users u LEFT JOIN loyalty_accounts l ON l.customer_id = u.id
        ORDER BY u.id DESC LIMIT 100
        """
    ) if can_manage_customers else []
    low_stock_items = [
        item for item in inventory_items
        if float(item["quantity"]) <= float(item["reorder_level"])
    ]
    return render_template(
        "admin.html",
        orders=orders if can_manage_orders else [],
        menu_items=menu_items,
        recipe_menu_items=recipe_menu_items,
        stories=stories,
        inventory_items=inventory_items,
        recipes=recipes,
        low_stock_items=low_stock_items,
        suppliers=suppliers,
        financial_transactions=financial_transactions,
        delivery_assignments=delivery_assignments,
        notification_entries=notification_entries,
        finance_totals=totals,
        staff_accounts=staff_accounts,
        customers=customers,
        staff_role=role,
        staff_name=session.get("staff_name", "Staff"),
        can_manage_orders=can_manage_orders,
        can_manage_content=can_manage_content,
        can_manage_inventory=can_manage_inventory,
        can_view_finance=can_view_finance,
        can_manage_customers=can_manage_customers,
        can_manage_staff=can_manage_staff,
        expense_idempotency_key=secrets.token_urlsafe(24),
        csrf_token=csrf_token,
    )


@app.route("/api/admin/media-signature", methods=["POST"])
def admin_media_signature():
    if not _has_permission("content"):
        return jsonify({"error": "Staff sign-in is required."}), 401
    data = _json_data()
    if not data:
        return jsonify({"error": "A media upload request is required."}), 400
    csrf_token = session.get("csrf_token")
    submitted_csrf = str(data.get("csrf_token") or "")
    if not csrf_token or not hmac.compare_digest(submitted_csrf, csrf_token):
        return jsonify({"error": "Refresh the page and try again."}), 400
    cloud_name, api_key, api_secret = _cloudinary_credentials()
    if not cloud_name or not api_key or not api_secret:
        return jsonify({"error": "Cloudinary uploads are not configured. Contact the site owner."}), 503

    section = data.get("section")
    media_type = data.get("media_type")
    if section not in {"menu", "stories"} or media_type not in {"image", "video"}:
        return jsonify({"error": "Choose a valid upload type."}), 400
    folder = f"vior-hotel/{section}"
    allowed_formats = "jpg,jpeg,png,webp" if media_type == "image" else "mp4,mov,webm"
    params = {
        "allowed_formats": allowed_formats,
        "folder": folder,
        "public_id": secrets.token_hex(16),
        "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
    }
    return jsonify({
        **params,
        "api_key": api_key,
        "cloud_name": cloud_name,
        "resource_type": media_type,
        "signature": _cloudinary_signature(params, api_secret),
    })


@app.route("/admin/menu", methods=["POST"])
def save_admin_menu_item():
    if not _has_permission("content"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    name = str(request.form.get("name") or "").strip()
    description = str(request.form.get("description") or "").strip()
    category = str(request.form.get("category") or "").strip()
    media_type = str(request.form.get("media_type") or "")
    media_url = str(request.form.get("media_url") or "").strip()
    try:
        price = int(request.form.get("price") or "")
    except ValueError:
        return "Enter a whole-number price in KES.", 400
    if (
        len(name) < 2 or len(name) > 80
        or not description or len(description) > 500
        or not category or len(category) > 50
        or price < 1 or price > 1_000_000
    ):
        return "Check the dish name, description, category, and price.", 400
    if name in MENU_PRICES:
        return "That dish already exists on the current menu.", 409
    if media_type not in {"image", "video"} or not _valid_cloudinary_media_url(media_url, media_type, "menu"):
        return "Upload a dish image or video using the provided media uploader.", 400

    existing = query_db("SELECT id FROM menu_items WHERE name = %s", (name,), one=True)
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        query_db(
            """
            UPDATE menu_items SET description = %s, category = %s, price = %s,
                media_url = %s, media_type = %s, active = %s WHERE id = %s
            """,
            (description, category, price, media_url, media_type, True, existing["id"]),
        )
    else:
        query_db(
            """
            INSERT INTO menu_items
                (name, description, category, price, media_url, media_type, active, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (name, description, category, price, media_url, media_type, True, now),
        )
        existing = query_db("SELECT id FROM menu_items WHERE name = %s", (name,), one=True)
    _write_audit(
        session.get("staff_phone", "primary-admin"),
        "menu_item_saved",
        "menu_item",
        existing["id"],
        {"name": name, "price": price, "active": True},
    )
    return redirect(url_for("admin_dashboard") + "#menu-management")


@app.route("/admin/menu/<int:item_id>/availability", methods=["POST"])
def toggle_admin_menu_item(item_id):
    if not _has_permission("content"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    active = request.form.get("active") == "true"
    changed = query_db(
        "UPDATE menu_items SET active = %s WHERE id = %s",
        (active, item_id),
    )
    if changed != 1:
        return "Dish not found.", 404
    _write_audit(session.get("staff_phone", "primary-admin"), "menu_item_availability_changed", "menu_item", item_id, {"active": active})
    return redirect(url_for("admin_dashboard") + "#menu-management")


@app.route("/admin/stories", methods=["POST"])
def save_admin_story():
    if not _has_permission("content"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    title = str(request.form.get("title") or "").strip()
    caption = str(request.form.get("caption") or "").strip()
    media_type = str(request.form.get("media_type") or "")
    media_url = str(request.form.get("media_url") or "").strip()
    button_text = str(request.form.get("button_text") or "").strip()
    button_url = str(request.form.get("button_url") or "").strip()
    starts_at = str(request.form.get("starts_at") or "").strip() or None
    ends_at = str(request.form.get("ends_at") or "").strip() or None
    published = request.form.get("published") == "on"

    if len(title) < 2 or len(title) > 100 or not caption or len(caption) > 1000:
        return "Enter a story title and a caption of up to 1,000 characters.", 400
    if media_type not in {"image", "video"} or not _valid_cloudinary_media_url(media_url, media_type, "stories"):
        return "Upload a story image or video using the provided media uploader.", 400
    if bool(button_text) != bool(button_url) or len(button_text) > 40:
        return "Provide both a short button label and its destination, or leave both blank.", 400
    if button_url:
        destination = urlparse(button_url)
        if destination.scheme not in {"http", "https"} or not destination.netloc:
            return "The story button destination must be an absolute HTTP or HTTPS URL.", 400
    try:
        if starts_at:
            starts_at = datetime.strptime(starts_at, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).isoformat()
        if ends_at:
            ends_at = datetime.strptime(ends_at, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            ).isoformat()
        if starts_at and ends_at and ends_at < starts_at:
            return "The story end date must be on or after its start date.", 400
    except ValueError:
        return "Enter valid story publication dates.", 400

    query_db(
        """
        INSERT INTO stories
            (title, caption, media_url, media_type, button_text, button_url,
             published, starts_at, ends_at, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            title, caption, media_url, media_type, button_text or None,
            button_url or None, published, starts_at, ends_at,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    saved_story = query_db(
        "SELECT id FROM stories WHERE title = %s ORDER BY id DESC",
        (title,),
        one=True,
    )
    _write_audit(
        session.get("staff_phone", "primary-admin"),
        "story_published",
        "story",
        saved_story["id"],
        {"title": title, "published": published},
    )
    return redirect(url_for("admin_dashboard") + "#story-management")


@app.route("/admin/stories/<int:story_id>/availability", methods=["POST"])
def toggle_admin_story(story_id):
    if not _has_permission("content"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    published = request.form.get("published") == "true"
    changed = query_db(
        "UPDATE stories SET published = %s WHERE id = %s",
        (published, story_id),
    )
    if changed != 1:
        return "Story not found.", 404
    _write_audit(session.get("staff_phone", "primary-admin"), "story_visibility_changed", "story", story_id, {"published": published})
    return redirect(url_for("admin_dashboard") + "#story-management")


@app.route("/admin/suppliers", methods=["POST"])
def save_supplier():
    if not _has_permission("inventory"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    name = str(request.form.get("name") or "").strip()
    phone = str(request.form.get("phone") or "").strip()
    email = str(request.form.get("email") or "").strip()
    notes = str(request.form.get("notes") or "").strip()
    if not name or len(name) > 120 or len(phone) > 40 or len(email) > 254 or len(notes) > 500:
        return "Check the supplier details and try again.", 400
    query_db(
        "INSERT INTO suppliers (name, phone, email, notes, created_at) VALUES (%s, %s, %s, %s, %s)",
        (name, phone or None, email or None, notes or None, datetime.now(timezone.utc).isoformat()),
    )
    supplier = query_db("SELECT id FROM suppliers WHERE name = %s ORDER BY id DESC", (name,), one=True)
    _write_audit(session.get("staff_phone", "primary-admin"), "supplier_created", "supplier", supplier["id"], {"name": name})
    return redirect(url_for("admin_dashboard") + "#inventory-management")


@app.route("/admin/inventory/items", methods=["POST"])
def save_inventory_item():
    if not _has_permission("inventory"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    name = str(request.form.get("name") or "").strip()
    unit = str(request.form.get("unit") or "").strip()
    try:
        quantity = Decimal(str(request.form.get("quantity") or "0"))
        reorder_level = Decimal(str(request.form.get("reorder_level") or "0"))
        unit_cost = Decimal(str(request.form.get("unit_cost") or "0"))
        if not all(value.is_finite() for value in (quantity, reorder_level, unit_cost)):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return "Enter valid stock quantities and unit cost.", 400
    if (
        len(name) < 2 or len(name) > 120 or not unit or len(unit) > 30
        or quantity < 0 or reorder_level < 0 or unit_cost < 0
    ):
        return "Check the stock item details and try again.", 400
    if query_db("SELECT id FROM inventory_items WHERE name = %s", (name,), one=True):
        return "An inventory item with that name already exists.", 409
    now = datetime.now(timezone.utc).isoformat()
    query_db(
        """
        INSERT INTO inventory_items (name, unit, quantity, reorder_level, unit_cost, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (name, unit, float(quantity), float(reorder_level), float(unit_cost), now),
    )
    saved_item = query_db("SELECT id FROM inventory_items WHERE name = %s", (name,), one=True)
    if not saved_item:
        raise RuntimeError("The new inventory item could not be loaded after it was saved.")
    item_id = saved_item["id"]
    if quantity > 0:
        query_db(
            """
            INSERT INTO inventory_movements
                (item_id, quantity, movement_type, reference, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (item_id, float(quantity), "opening_balance", None, "Opening stock", now),
        )
    _write_audit(session.get("staff_phone", "primary-admin"), "inventory_item_created", "inventory_item", item_id, {"name": name})
    return redirect(url_for("admin_dashboard") + "#inventory-management")


@app.route("/admin/inventory/purchase", methods=["POST"])
def record_inventory_purchase():
    if not _has_permission("inventory"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    try:
        item_id = int(request.form.get("item_id") or "")
        supplier_id = int(request.form.get("supplier_id") or "")
        quantity = Decimal(str(request.form.get("quantity") or ""))
        unit_cost = Decimal(str(request.form.get("unit_cost") or ""))
        if not quantity.is_finite() or not unit_cost.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return "Enter a valid item, supplier, quantity, and unit cost.", 400
    reference = str(request.form.get("reference") or "").strip()
    if (
        quantity <= 0 or unit_cost < 0 or quantity > Decimal("1000000")
        or unit_cost > Decimal("100000000") or not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", reference)
    ):
        return "Enter a positive quantity, valid unit cost, and unique purchase reference.", 400
    if not query_db("SELECT id FROM inventory_items WHERE id = %s", (item_id,), one=True):
        return "Inventory item not found.", 404
    if not query_db("SELECT id FROM suppliers WHERE id = %s", (supplier_id,), one=True):
        return "Supplier not found.", 404

    now = datetime.now(timezone.utc).isoformat()
    total_cost = quantity * unit_cost
    statements = [
        (
            """
            INSERT INTO financial_transactions
                (idempotency_key, transaction_type, category, amount, currency, reference, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (idempotency_key) DO NOTHING
            """,
            (
                f"supplier_purchase:{reference}", "expense", "inventory",
                float(total_cost), "KES", reference, f"Supplier purchase · supplier {supplier_id}", now,
            ),
        ),
        (
            "UPDATE inventory_items SET quantity = quantity + %s, unit_cost = %s, updated_at = %s WHERE id = %s",
            (float(quantity), float(unit_cost), now, item_id),
        ),
        (
            """
            INSERT INTO inventory_movements
                (item_id, supplier_id, quantity, unit_cost, movement_type, reference, notes, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                item_id, supplier_id, float(quantity), float(unit_cost),
                "purchase", reference, "Supplier stock purchase", now,
            ),
        ),
    ]
    try:
        execute_transaction_checked(statements, {0: 1, 1: 1})
    except RuntimeError:
        return "That purchase reference has already been recorded; stock was not changed.", 409
    _write_audit(session.get("staff_phone", "primary-admin"), "inventory_purchase_recorded", "inventory_item", item_id, {"reference": reference, "quantity": float(quantity)})
    return redirect(url_for("admin_dashboard") + "#inventory-management")


@app.route("/admin/inventory/movement", methods=["POST"])
def record_inventory_movement():
    if not _has_permission("inventory"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    try:
        item_id = int(request.form.get("item_id") or "")
        quantity = Decimal(str(request.form.get("quantity") or ""))
        if not quantity.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return "Enter a valid inventory item and quantity.", 400
    movement_type = str(request.form.get("movement_type") or "")
    notes = str(request.form.get("notes") or "").strip()
    if movement_type not in {"waste", "adjustment"} or quantity <= 0 or quantity > Decimal("1000000"):
        return "Choose wastage or a positive stock correction quantity.", 400
    item = query_db("SELECT id, quantity FROM inventory_items WHERE id = %s", (item_id,), one=True)
    if not item:
        return "Inventory item not found.", 404
    adjustment = -float(quantity) if movement_type == "waste" else float(quantity)
    now = datetime.now(timezone.utc).isoformat()
    statements = [
        (
            "UPDATE inventory_items SET quantity = quantity + %s, updated_at = %s WHERE id = %s AND quantity + %s >= 0",
            (adjustment, now, item_id, adjustment),
        ),
        (
            """
            INSERT INTO inventory_movements
                (item_id, quantity, movement_type, notes, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (item_id, adjustment, movement_type, notes or None, now),
        ),
    ]
    try:
        execute_transaction_checked(statements, {0: 1})
    except RuntimeError:
        return "This stock movement would make the inventory balance negative.", 409
    _write_audit(session.get("staff_phone", "primary-admin"), "inventory_movement_recorded", "inventory_item", item_id, {"type": movement_type, "quantity": adjustment})
    return redirect(url_for("admin_dashboard") + "#inventory-management")


@app.route("/admin/inventory/recipes", methods=["POST"])
def save_inventory_recipe():
    if not _has_permission("inventory"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    menu_item_name = str(request.form.get("menu_item_name") or "").strip()
    try:
        inventory_item_id = int(request.form.get("inventory_item_id") or "")
        quantity = Decimal(str(request.form.get("quantity_per_serving") or ""))
        if not quantity.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return "Enter a valid ingredient and quantity per serving.", 400
    if (
        (menu_item_name not in MENU_PRICES and not query_db(
            "SELECT id FROM menu_items WHERE name = %s AND active = %s",
            (menu_item_name, True),
            one=True,
        ))
        or quantity <= 0 or quantity > Decimal("1000000")
    ):
        return "Choose an available menu item and a positive ingredient quantity.", 400
    if not query_db("SELECT id FROM inventory_items WHERE id = %s", (inventory_item_id,), one=True):
        return "Inventory item not found.", 404
    query_db(
        """
        INSERT INTO inventory_recipes (menu_item_name, inventory_item_id, quantity_per_serving)
        VALUES (%s, %s, %s)
        ON CONFLICT (menu_item_name, inventory_item_id)
        DO UPDATE SET quantity_per_serving = excluded.quantity_per_serving
        """,
        (menu_item_name, inventory_item_id, float(quantity)),
    )
    _write_audit(session.get("staff_phone", "primary-admin"), "recipe_updated", "menu_item", menu_item_name, {"inventory_item_id": inventory_item_id, "quantity_per_serving": float(quantity)})
    return redirect(url_for("admin_dashboard") + "#inventory-management")


@app.route("/admin/expenses", methods=["POST"])
def record_expense():
    if not _has_permission("finance"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    category = str(request.form.get("category") or "").strip()
    notes = str(request.form.get("notes") or "").strip()
    reference = str(request.form.get("reference") or "").strip()
    try:
        amount = Decimal(str(request.form.get("amount") or ""))
        if not amount.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return "Enter a valid expense amount.", 400
    if (
        amount <= 0 or amount > Decimal("1000000000")
        or not category or len(category) > 80 or len(notes) > 500
        or not re.fullmatch(r"[A-Za-z0-9._:-]{16,100}", reference)
    ):
        return "Check the expense category, amount, notes, and reference.", 400
    query_db(
        """
        INSERT INTO financial_transactions
            (idempotency_key, transaction_type, category, amount, currency, reference, notes, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            f"expense:{reference}", "expense", category, float(amount),
            "KES", reference, notes or None, datetime.now(timezone.utc).isoformat(),
        ),
    )
    if result != 1:
        return "That expense reference has already been recorded.", 409
    _write_audit(session.get("staff_phone", "primary-admin"), "expense_recorded", "financial_transaction", reference, {"category": category, "amount": float(amount)})
    return redirect(url_for("admin_dashboard") + "#finance-management")


@app.route("/admin/refunds", methods=["POST"])
def record_refund():
    if not _has_permission("finance"):
        return "Finance access is required.", 403
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    order_number = str(request.form.get("order_number") or "").strip()
    reference = str(request.form.get("reference") or "").strip()
    try:
        amount = Decimal(str(request.form.get("amount") or ""))
        if not amount.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        return "Enter a valid refund amount.", 400
    payment = query_db(
        "SELECT amount FROM payments WHERE order_number = %s AND status = %s ORDER BY id DESC",
        (order_number, "Completed"),
        one=True,
    )
    if not payment:
        return "A verified payment is required before recording a refund.", 409
    if amount <= 0 or amount > Decimal(str(payment["amount"])):
        return "The refund must be positive and no greater than the verified payment.", 400
    if (
        len(reference) < 8 or len(reference) > 100
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", reference)
    ):
        return "Enter the provider's refund reference.", 400
    result = query_db(
        """
        INSERT INTO financial_transactions
            (idempotency_key, transaction_type, category, amount, currency, reference, notes, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            f"refund:{order_number}", "refund", "customer_refund",
            float(amount), "KES", reference, order_number,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    if result != 1:
        return "A refund has already been recorded against this order.", 409
    _write_audit(
        session.get("staff_phone", "primary-admin"),
        "refund_recorded",
        "order",
        order_number,
        {"reference": reference, "amount": float(amount)},
    )
    return redirect(url_for("admin_dashboard") + "#finance-management")


@app.route("/admin/order/<order_number>/delivery", methods=["POST"])
def assign_delivery(order_number):
    if not _has_permission("delivery"):
        return redirect(url_for("login"))
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    driver_name = str(request.form.get("driver_name") or "").strip()
    driver_phone = _normalize_phone(request.form.get("driver_phone"))
    order = query_db("SELECT order_number, status FROM orders WHERE order_number = %s", (order_number,), one=True)
    if not order:
        return "Order not found.", 404
    if order["status"] not in {"Preparing", "Out for Delivery"}:
        return "Assign a driver when the order is being prepared or dispatched.", 409
    if len(driver_name) < 2 or len(driver_name) > 80 or not driver_phone:
        return "Enter a driver name and valid Kenyan phone number.", 400
    now = datetime.now(timezone.utc).isoformat()
    query_db(
        """
        INSERT INTO delivery_assignments
            (order_number, driver_name, driver_phone, status, assigned_by, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (order_number) DO UPDATE SET
            driver_name = excluded.driver_name, driver_phone = excluded.driver_phone,
            status = excluded.status, assigned_by = excluded.assigned_by,
            updated_at = excluded.updated_at
        """,
        (
            order_number, driver_name, driver_phone, "Assigned",
            session.get("staff_phone", "primary-admin"), now, now,
        ),
    )
    _write_audit(session.get("staff_phone", "primary-admin"), "delivery_assigned", "order", order_number, {"driver": driver_name})
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/finance.csv")
def export_financial_records():
    if not _has_permission("finance"):
        return redirect(url_for("login"))
    transactions = query_db(
        "SELECT idempotency_key, transaction_type, category, amount, currency, reference, notes, created_at "
        "FROM financial_transactions ORDER BY created_at DESC"
    )
    output = io.StringIO()
    output.write("idempotency_key,transaction_type,category,amount,currency,reference,notes,created_at\n")
    for row in transactions:
        output.write(",".join(
            '"' + str(row.get(key) or "").replace('"', '""') + '"'
            for key in ("idempotency_key", "transaction_type", "category", "amount", "currency", "reference", "notes", "created_at")
        ) + "\n")
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=vior-financial-records.csv"},
    )


@app.route("/admin/staff", methods=["POST"])
def create_staff_account():
    if not _has_permission("staff"):
        return "Only an owner can manage staff accounts.", 403
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    fullname = str(request.form.get("fullname") or "").strip()
    phone = _normalize_phone(request.form.get("phone"))
    password = str(request.form.get("password") or "")
    role = str(request.form.get("role") or "")
    if len(fullname) < 2 or len(fullname) > 80 or not phone:
        return "Enter a staff name and valid Kenyan mobile number.", 400
    if role not in ROLE_PERMISSIONS or role == "owner":
        return "Choose a supported non-owner staff role.", 400
    if len(password) < 12 or len(password) > 256:
        return "Staff passwords must be at least 12 characters.", 400
    if query_db("SELECT id FROM staff_accounts WHERE phone = %s", (phone,), one=True):
        return "A staff account already exists for that number.", 409
    result = query_db(
        """
        INSERT INTO staff_accounts
            (fullname, phone, password_hash, role, active, created_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            fullname, phone, generate_password_hash(password), role, True,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    staff = query_db("SELECT id FROM staff_accounts WHERE phone = %s", (phone,), one=True)
    _write_audit(session.get("staff_phone", "primary-admin"), "staff_created", "staff", staff["id"], {"role": role})
    return redirect(url_for("admin_dashboard") + "#staff-management")


@app.route("/admin/staff/<int:staff_id>/availability", methods=["POST"])
def toggle_staff_account(staff_id):
    if not _has_permission("staff"):
        return "Only an owner can manage staff accounts.", 403
    if not _valid_csrf_token():
        return "Please refresh the page and try again.", 400
    active = request.form.get("active") == "true"
    changed = query_db(
        "UPDATE staff_accounts SET active = %s WHERE id = %s",
        (active, staff_id),
    )
    if changed != 1:
        return "Staff account not found.", 404
    _write_audit(session.get("staff_phone", "primary-admin"), "staff_access_changed", "staff", staff_id, {"active": active})
    return redirect(url_for("admin_dashboard") + "#staff-management")


@app.route("/admin/order/<int:order_id>/status", methods=["POST"], endpoint="update_order_status")
def update_order_status(order_id):
    if not _has_permission("orders"):
        return redirect(url_for("login"))
    csrf_token = session.get("csrf_token")
    if not csrf_token or not hmac.compare_digest(
        str(request.form.get("csrf_token") or ""), csrf_token
    ):
        return "Please refresh the page and try again.", 400
    requested_status = request.form.get("status")
    if requested_status not in ORDER_STATUSES:
        return "Invalid order status.", 400

    order = query_db(
        "SELECT status, order_number, amount, items FROM orders WHERE id = %s",
        (order_id,),
        one=True,
    )
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
    checked_indexes = {0: 1}
    if requested_status == "Preparing":
        try:
            inventory_updates, inventory_checks = _inventory_deduction_statements(order)
        except ValueError as error:
            return str(error), 409
        offset = len(updates)
        updates.extend(inventory_updates)
        checked_indexes.update({
            index + offset: expected for index, expected in inventory_checks.items()
        })
    if current == "Awaiting Till Payment — verify manually" and requested_status == "Payment Verified":
        updates.append((
            "UPDATE payments SET status = %s, provider_status = %s, updated_at = %s "
            "WHERE order_number = (SELECT order_number FROM orders WHERE id = %s) "
            "AND provider = %s",
            ("Completed", "Verified by staff", now, order_id, "manual_till"),
        ))
    try:
        execute_transaction_checked(updates, checked_indexes)
    except RuntimeError as error:
        app.logger.info("Order or inventory changed during status update %s: %s", order_id, error)
        return "Insufficient inventory or order status changed; refresh and try again.", 409
    _write_audit(
        session.get("staff_phone", "primary-admin"),
        "order_status_changed",
        "order",
        order["order_number"],
        {"from": current, "to": requested_status},
    )
    if current == "Awaiting Till Payment — verify manually" and requested_status == "Payment Verified":
        payment = query_db(
            "SELECT receipt FROM payments WHERE order_number = %s AND provider = %s "
            "AND status = %s ORDER BY id DESC",
            (order["order_number"], "manual_till", "Completed"),
            one=True,
        )
        if payment:
            _record_verified_payment(order["order_number"], order["amount"], payment.get("receipt") or "Till verified by staff")
            _deliver_order_whatsapp_event(order["order_number"], "receipt")
    elif requested_status in {"Preparing", "Out for Delivery", "Delivered"}:
        _deliver_order_whatsapp_event(order["order_number"], "status", requested_status)
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
