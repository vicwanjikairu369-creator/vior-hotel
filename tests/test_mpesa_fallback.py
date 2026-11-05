import re
import sqlite3
from unittest.mock import Mock, patch

import pytest
from werkzeug.security import generate_password_hash

from app import app
import database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'vior-hotel-test.db'))
    for key in ('MPESA_CONSUMER_KEY', 'MPESA_CONSUMER_SECRET', 'MPESA_SHORTCODE', 'MPESA_PASSKEY', 'CALLBACK_URL'):
        monkeypatch.delenv(key, raising=False)
    for key in (
        'PESAPAL_CONSUMER_KEY',
        'PESAPAL_CONSUMER_SECRET',
        'PESAPAL_ENVIRONMENT',
        'PESAPAL_CALLBACK_URL',
        'PESAPAL_NOTIFICATION_ID',
    ):
        monkeypatch.delenv(key, raising=False)
    for key in ('AFRICASTALKING_USERNAME', 'AFRICASTALKING_API_KEY', 'AFRICASTALKING_SENDER_ID'):
        monkeypatch.delenv(key, raising=False)
    for key in ('ADMIN_PHONE', 'ADMIN_PASSWORD', 'STAFF_SETUP_TOKEN'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(database, '_initialized', False)
    app.config['TESTING'] = True
    app.debug = True
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['customer_name'] = 'Test User'
            sess['customer_phone'] = '254712345678'
        yield client


def test_pesapal_requires_credentials_and_manual_till_is_explicit(client):
    invalid = client.post(
        '/api/pesapal/pay',
        json={
            'items': [{'name': 'Unknown meal'}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
        headers={'Idempotency-Key': 'checkout-key-invalid-pay'},
    )
    assert invalid.status_code == 400
    assert 'not on the current menu' in invalid.get_json()['error']

    response = client.post(
        '/api/pesapal/pay',
        json={
            'items': [{'name': 'Tea'}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
        headers={'Idempotency-Key': 'checkout-key-missing1'},
    )
    assert response.status_code == 503
    assert 'not configured' in response.get_json()['error']
    assert database.query_db('SELECT COUNT(*) AS total FROM orders', one=True)['total'] == 0

    till = client.post(
        '/api/mpesa/till-order',
        json={'items': [{'name': 'Tea'}], 'phone': '0712345678', 'order_type': 'Pickup'},
        headers={'Idempotency-Key': 'checkout-key-till-0001'},
    )
    assert till.status_code == 201
    assert till.get_json()['till_number'] == '3484738'


def test_main_pages_render_and_admin_requires_staff_login(client):
    for path in ('/', '/cart', '/register', '/login'):
        response = client.get(path)
        assert response.status_code == 200, path
    response = client.get('/admin')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')


def test_till_order_uses_server_menu_prices_and_can_be_tracked(client):
    payload = {
        'items': [{'name': 'Tea', 'price': 1}],
        'phone': '0712345678',
        'order_type': 'Pickup',
    }
    key = {'Idempotency-Key': 'checkout-key-till-0002'}
    response = client.post('/api/mpesa/till-order', json=payload, headers=key)
    assert response.status_code == 201, response.get_data(as_text=True)
    order = response.get_json()
    assert order['amount'] == 50
    assert order['till_number'] == '3484738'
    retry = client.post('/api/mpesa/till-order', json=payload, headers=key)
    assert retry.status_code == 200
    assert retry.get_json()['order_number'] == order['order_number']
    assert database.query_db('SELECT COUNT(*) AS total FROM payments', one=True)['total'] == 1

    tracking = client.get(f"/api/order/status/{order['order_number']}")
    assert tracking.status_code == 200
    assert tracking.get_json()['status'] == 'Pending'


def test_mpesa_rejects_items_not_on_the_current_menu(client):
    response = client.post(
        '/api/mpesa/till-order',
        json={
            'items': [{'name': 'Unknown meal', 'price': 1}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
        headers={'Idempotency-Key': 'checkout-key-invalid-001'},
    )
    assert response.status_code == 400
    assert 'not on the current menu' in response.get_json()['error']


def _staff_login(client, monkeypatch):
    monkeypatch.setenv('ADMIN_PHONE', '254700000000')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password')
    if not database.query_db(
        'SELECT id FROM staff_accounts WHERE phone = %s',
        ('254700000000',),
        one=True,
    ):
        database.query_db(
            'INSERT INTO staff_accounts (fullname, phone, password_hash, role, active, created_at) '
            'VALUES (%s, %s, %s, %s, %s, %s)',
            (
                'Test Owner',
                '254700000000',
                generate_password_hash('test-password'),
                'owner',
                True,
                '2026-11-05T07:00:00+00:00',
            ),
        )
    client.get('/login')
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']
    response = client.post(
        '/login',
        data={
            'phone': '0700000000',
            'staff_password': 'test-password',
            'csrf_token': csrf_token,
        },
    )
    if response.status_code == 302:
        client.get('/admin')
    return response


def test_staff_login_opens_admin_dashboard(client, monkeypatch):
    response = _staff_login(client, monkeypatch)
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin')
    assert client.get('/admin').status_code == 200


def test_staff_login_is_rate_limited_by_request_ip(client, monkeypatch):
    monkeypatch.setenv('ADMIN_PHONE', '254700000000')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password')
    client.get('/login')
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']

    for _ in range(10):
        response = client.post(
            '/login',
            data={
                'phone': '0700000000',
                'admin_password': 'incorrect',
                'csrf_token': csrf_token,
            },
        )
        assert response.status_code == 401

    blocked = client.post(
        '/login',
        data={
            'phone': '0700000000',
            'admin_password': 'incorrect',
            'csrf_token': csrf_token,
        },
    )
    assert blocked.status_code == 429
    assert database.query_db(
        'SELECT COUNT(*) AS total FROM auth_attempts',
        one=True,
    )['total'] == 10


def test_first_staff_setup_creates_one_owner_then_closes(client, monkeypatch):
    setup_token = 'a-secure-one-time-token-with-more-than-32-characters'
    monkeypatch.setenv('STAFF_SETUP_TOKEN', setup_token)
    login_page = client.get('/login')
    assert b'/staff/setup' in login_page.data
    setup_page = client.get('/staff/setup')
    assert setup_page.status_code == 200
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']

    created = client.post(
        '/staff/setup',
        data={
            'csrf_token': csrf_token,
            'fullname': 'Hotel Owner',
            'phone': '0701234567',
            'setup_token': setup_token,
            'password': 'a-long-strong-owner-password',
            'password_confirmation': 'a-long-strong-owner-password',
        },
    )
    assert created.status_code == 302
    assert created.headers['Location'].endswith('/admin')
    owner = database.query_db(
        'SELECT fullname, phone, role, active FROM staff_accounts',
        one=True,
    )
    assert owner == {
        'fullname': 'Hotel Owner',
        'phone': '254701234567',
        'role': 'owner',
        'active': 1,
    }
    assert client.get('/admin').status_code == 200
    assert client.get('/staff/setup').status_code == 404


def test_legacy_admin_credentials_do_not_bypass_staff_login(client, monkeypatch):
    monkeypatch.setenv('ADMIN_PHONE', '254700000000')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password')
    client.get('/login')
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']
    failed = client.post(
        '/login',
        data={
            'phone': '0700000000',
            'staff_password': 'test-password',
            'csrf_token': csrf_token,
        },
    )
    assert failed.status_code == 401
    assert database.query_db('SELECT COUNT(*) AS total FROM staff_accounts', one=True)['total'] == 0


def test_first_staff_setup_requires_private_token_even_with_legacy_admin_env(client, monkeypatch):
    monkeypatch.setenv('STAFF_SETUP_TOKEN', 'a-secure-one-time-token-with-more-than-32-characters')
    monkeypatch.setenv('ADMIN_PHONE', '254700000000')
    monkeypatch.setenv('ADMIN_PASSWORD', 'existing-admin-password')
    client.get('/staff/setup')
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']
    failed = client.post(
        '/staff/setup',
        data={
            'csrf_token': csrf_token,
            'fullname': 'Untrusted Visitor',
            'phone': '0701234567',
            'setup_token': 'incorrect setup token',
            'password': 'a-long-strong-owner-password',
            'password_confirmation': 'a-long-strong-owner-password',
        },
    )
    assert failed.status_code == 401
    assert database.query_db('SELECT COUNT(*) AS total FROM staff_accounts', one=True)['total'] == 0

    assert client.get('/staff/setup').status_code == 200


def test_existing_database_migration_adds_order_created_at(tmp_path):
    connection = sqlite3.connect(tmp_path / 'legacy.db')
    connection.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, phone TEXT)')
    connection.execute('CREATE TABLE orders (id INTEGER PRIMARY KEY, order_number TEXT)')

    database._create_or_migrate_schema(connection)

    order_columns = {
        row[1] for row in connection.execute('PRAGMA table_info(orders)').fetchall()
    }
    assert 'created_at' in order_columns
    connection.close()


def test_admin_order_status_requires_csrf_token(client, monkeypatch):
    _staff_login(client, monkeypatch)
    order = client.post(
        '/api/mpesa/till-order',
        json={
            'items': [{'name': 'Tea'}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
        headers={'Idempotency-Key': 'checkout-key-admin-0001'},
    ).get_json()
    order_row = database.query_db(
        'SELECT id FROM orders WHERE order_number = %s',
        (order['order_number'],),
        one=True,
    )
    assert client.get('/admin').status_code == 200

    response = client.post(
        f"/admin/order/{order_row['id']}/status",
        data={'status': 'Payment Verified'},
    )

    assert response.status_code == 400
    assert database.query_db(
        'SELECT status FROM orders WHERE id = %s',
        (order_row['id'],),
        one=True,
    )['status'] == 'Awaiting Till Payment — verify manually'
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']
    accepted = client.post(
        f"/admin/order/{order_row['id']}/status",
        data={'status': 'Payment Verified', 'csrf_token': csrf_token},
    )
    assert accepted.status_code == 302
    assert database.query_db(
        'SELECT status FROM orders WHERE id = %s',
        (order_row['id'],),
        one=True,
    )['status'] == 'Payment Verified'
    assert database.query_db(
        'SELECT status, provider_status FROM payments WHERE order_number = %s',
        (order['order_number'],),
        one=True,
    ) == {'status': 'Completed', 'provider_status': 'Verified by staff'}


def test_admin_can_publish_cloudinary_dish_and_story(client, monkeypatch):
    monkeypatch.setenv('ADMIN_PHONE', '254700000000')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password')
    monkeypatch.setenv('CLOUDINARY_CLOUD_NAME', 'vior-test')
    monkeypatch.setenv('CLOUDINARY_API_KEY', 'public-test-key')
    monkeypatch.setenv('CLOUDINARY_API_SECRET', 'private-test-secret')
    _staff_login(client, monkeypatch)
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']

    signature = client.post(
        '/api/admin/media-signature',
        json={'csrf_token': csrf_token, 'section': 'menu', 'media_type': 'image'},
    )
    assert signature.status_code == 200
    assert 'api_secret' not in signature.get_json()
    assert signature.get_json()['folder'] == 'vior-hotel/menu'

    dish = client.post(
        '/admin/menu',
        data={
            'csrf_token': csrf_token,
            'name': 'Test Special',
            'description': 'A test dish',
            'category': 'Lunch',
            'price': '275',
            'media_type': 'image',
            'media_url': 'https://res.cloudinary.com/vior-test/image/upload/vior-hotel/menu/test.jpg',
        },
    )
    assert dish.status_code == 302
    homepage = client.get('/')
    assert b'Test Special' in homepage.data
    assert b'Ugali Beef Stew' in homepage.data

    order = client.post(
        '/api/mpesa/till-order',
        json={
            'items': [{'name': 'Test Special', 'price': 1}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
        headers={'Idempotency-Key': 'checkout-added-dish-001'},
    )
    assert order.status_code == 201
    assert order.get_json()['amount'] == 275

    story = client.post(
        '/admin/stories',
        data={
            'csrf_token': csrf_token,
            'title': 'Weekend special',
            'caption': 'A new story',
            'media_type': 'video',
            'media_url': 'https://res.cloudinary.com/vior-test/video/upload/vior-hotel/stories/test.mp4',
            'published': 'on',
        },
    )
    assert story.status_code == 302
    assert b'Weekend special' in client.get('/').data


def test_admin_media_upload_needs_login_and_csrf(client):
    unauthorized = client.post(
        '/api/admin/media-signature',
        json={'section': 'stories', 'media_type': 'image'},
    )
    assert unauthorized.status_code == 401


def test_whatsapp_receipt_is_sent_only_after_verified_payment(client, monkeypatch):
    monkeypatch.setenv('ADMIN_PHONE', '254700000000')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password')
    monkeypatch.setenv('WHATSAPP_ACCESS_TOKEN', 'test-access-token')
    monkeypatch.setenv('WHATSAPP_PHONE_NUMBER_ID', '123456789')
    monkeypatch.setenv('WHATSAPP_RECEIPT_TEMPLATE', 'vior_order_receipt')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://vior.example')
    whatsapp_response = Mock()
    whatsapp_response.json.return_value = {'messages': [{'id': 'wamid.test-message'}]}
    with patch('app.requests.post', return_value=whatsapp_response) as send_whatsapp:
        order_response = client.post(
            '/api/mpesa/till-order',
            json={
                'items': [{'name': 'Tea'}],
                'phone': '0712345678',
                'order_type': 'Pickup',
                'whatsapp_opt_in': True,
            },
            headers={'Idempotency-Key': 'checkout-whatsapp-receipt-01'},
        )
        assert order_response.status_code == 201
        order = order_response.get_json()
        assert database.query_db(
            'SELECT COUNT(*) AS total FROM notification_outbox',
            one=True,
        )['total'] == 0

        assert _staff_login(client, monkeypatch).status_code == 302
        order_row = database.query_db(
            'SELECT id FROM orders WHERE order_number = %s',
            (order['order_number'],),
            one=True,
        )
        with client.session_transaction() as sess:
            csrf_token = sess['csrf_token']
        verified = client.post(
            f"/admin/order/{order_row['id']}/status",
            data={'status': 'Payment Verified', 'csrf_token': csrf_token},
        )
        assert verified.status_code == 302

    notification = database.query_db(
        'SELECT * FROM notification_outbox WHERE idempotency_key = %s',
        (f"whatsapp:receipt:{order['order_number']}:",),
        one=True,
    )
    assert notification['status'] == 'Sent'
    assert notification['provider_message_id'] == 'wamid.test-message'
    send_whatsapp.assert_called_once()
    assert send_whatsapp.call_args.kwargs['headers']['Authorization'] == 'Bearer test-access-token'
    template = send_whatsapp.call_args.kwargs['json']['template']
    assert template['name'] == 'vior_order_receipt'
    assert template['components'][0]['parameters'][0]['text'] == order['order_number']
    receipt_url = template['components'][0]['parameters'][-1]['text']
    receipt_response = client.get(receipt_url.replace('https://vior.example', ''))
    assert receipt_response.status_code == 200
    assert b'Payment receipt' in receipt_response.data
    assert order['order_number'].encode() in receipt_response.data
    assert database.query_db(
        'SELECT COUNT(*) AS total FROM financial_transactions WHERE idempotency_key = %s',
        (f"payment:{order['order_number']}",),
        one=True,
    )['total'] == 1


def test_paid_orders_deduct_configured_recipe_stock_once(client, monkeypatch):
    now = '2026-11-05T07:00:00+00:00'
    database.query_db(
        'INSERT INTO inventory_items (name, unit, quantity, reorder_level, unit_cost, updated_at) '
        'VALUES (%s, %s, %s, %s, %s, %s)',
        ('Milk', 'litre', 1.0, 0.2, 100.0, now),
    )
    item = database.query_db('SELECT id FROM inventory_items WHERE name = %s', ('Milk',), one=True)
    database.query_db(
        'INSERT INTO inventory_recipes (menu_item_name, inventory_item_id, quantity_per_serving) '
        'VALUES (%s, %s, %s)',
        ('Tea', item['id'], 0.1),
    )
    order = client.post(
        '/api/mpesa/till-order',
        json={'items': [{'name': 'Tea'}], 'phone': '0712345678', 'order_type': 'Pickup'},
        headers={'Idempotency-Key': 'checkout-inventory-deduct-1'},
    ).get_json()

    assert _staff_login(client, monkeypatch).status_code == 302
    order_row = database.query_db(
        'SELECT id FROM orders WHERE order_number = %s',
        (order['order_number'],),
        one=True,
    )
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']
    verified = client.post(
        f"/admin/order/{order_row['id']}/status",
        data={'status': 'Payment Verified', 'csrf_token': csrf_token},
    )
    assert verified.status_code == 302
    preparing = client.post(
        f"/admin/order/{order_row['id']}/status",
        data={'status': 'Preparing', 'csrf_token': csrf_token},
    )
    assert preparing.status_code == 302
    stock = database.query_db(
        'SELECT quantity FROM inventory_items WHERE id = %s',
        (item['id'],),
        one=True,
    )
    assert stock['quantity'] == pytest.approx(0.9)
    movement_count = database.query_db(
        'SELECT COUNT(*) AS total FROM inventory_movements WHERE reference = %s',
        (order['order_number'],),
        one=True,
    )['total']
    assert movement_count == 1


def test_verified_customer_order_earns_loyalty_points_idempotently(client, monkeypatch):
    database.query_db(
        'INSERT INTO users (fullname, phone, phone_verified_at) VALUES (%s, %s, %s)',
        ('Loyal Customer', '254712300000', '2026-11-05T07:00:00+00:00'),
    )
    customer = database.query_db(
        'SELECT id FROM users WHERE phone = %s',
        ('254712300000',),
        one=True,
    )
    with client.session_transaction() as sess:
        sess['customer_id'] = customer['id']
        sess['customer_name'] = 'Loyal Customer'
        sess['customer_phone'] = '254712300000'
    order = client.post(
        '/api/mpesa/till-order',
        json={'items': [{'name': 'Ugali Beef Stew'}], 'phone': '0712300000', 'order_type': 'Pickup'},
        headers={'Idempotency-Key': 'checkout-loyalty-earned-001'},
    ).get_json()

    assert _staff_login(client, monkeypatch).status_code == 302
    order_row = database.query_db(
        'SELECT id FROM orders WHERE order_number = %s',
        (order['order_number'],),
        one=True,
    )
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']
    response = client.post(
        f"/admin/order/{order_row['id']}/status",
        data={'status': 'Payment Verified', 'csrf_token': csrf_token},
    )
    assert response.status_code == 302
    balance = database.query_db(
        'SELECT points_balance FROM loyalty_accounts WHERE customer_id = %s',
        (customer['id'],),
        one=True,
    )
    assert balance['points_balance'] == 2
    assert database.add_loyalty_points(customer['id'], order['order_number'], 2, '2026-11-05T07:01:00+00:00') is False
    assert database.query_db(
        'SELECT points_balance FROM loyalty_accounts WHERE customer_id = %s',
        (customer['id'],),
        one=True,
    )['points_balance'] == 2


def test_new_order_number_has_unpredictable_suffix(client):
    response = client.post(
        '/api/mpesa/till-order',
        json={
            'items': [{'name': 'Tea'}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
        headers={'Idempotency-Key': 'checkout-key-order-0001'},
    )

    assert response.status_code == 201
    assert re.fullmatch(r'VH-\d{14}[A-F0-9]{32}', response.get_json()['order_number'])


def test_customer_registration_requires_and_consumes_sms_otp(client, monkeypatch):
    monkeypatch.setenv('AFRICASTALKING_USERNAME', 'test-username')
    monkeypatch.setenv('AFRICASTALKING_API_KEY', 'test-api-key')
    monkeypatch.setattr('app.secrets.randbelow', lambda _: 123456)
    sms_response = Mock()
    sms_response.json.return_value = {
        'SMSMessageData': {
            'Recipients': [{'statusCode': 101, 'status': 'Success'}],
        },
    }

    with patch('app.requests.post', return_value=sms_response) as send_sms:
        client.get('/register')
        with client.session_transaction() as sess:
            csrf_token = sess['csrf_token']
        response = client.post(
            '/register',
            data={
                'action': 'request',
                'csrf_token': csrf_token,
                'fullname': 'Test Customer',
                'phone': '0712345678',
            },
        )

    assert response.status_code == 302
    assert response.headers['Location'].endswith('/register?otp=sent')
    assert send_sms.call_args.kwargs['data']['to'] == '+254712345678'
    assert send_sms.call_args.kwargs['headers']['apiKey'] == 'test-api-key'
    assert '123456' in send_sms.call_args.kwargs['data']['message']
    code_page = client.get(response.headers['Location'])
    assert 'value="123456"' not in code_page.get_data(as_text=True)

    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']
    verification = client.post(
        '/register',
        data={'action': 'verify', 'csrf_token': csrf_token, 'code': '123456'},
    )

    assert verification.status_code == 302
    assert client.get('/').status_code == 200
    customer = database.query_db(
        'SELECT fullname, phone, phone_verified_at FROM users WHERE phone = %s',
        ('254712345678',),
        one=True,
    )
    assert customer['fullname'] == 'Test Customer'
    assert customer['phone_verified_at']
    with client.session_transaction() as sess:
        assert sess['customer_phone'] == '254712345678'
        assert 'pending_otp' not in sess


def test_customer_otp_rate_limit_and_attempt_limit(client, monkeypatch):
    monkeypatch.setenv('AFRICASTALKING_USERNAME', 'test-username')
    monkeypatch.setenv('AFRICASTALKING_API_KEY', 'test-api-key')
    monkeypatch.setattr('app.secrets.randbelow', lambda _: 123456)
    sms_response = Mock()
    sms_response.json.return_value = {
        'SMSMessageData': {'Recipients': [{'statusCode': 101}]},
    }

    with patch('app.requests.post', return_value=sms_response) as send_sms:
        client.get('/register')
        with client.session_transaction() as sess:
            csrf_token = sess['csrf_token']
        first = client.post(
            '/register',
            data={
                'action': 'request',
                'csrf_token': csrf_token,
                'fullname': 'Test Customer',
                'phone': '0712345678',
            },
        )
        limited = client.post(
            '/register',
            data={
                'action': 'request',
                'csrf_token': csrf_token,
                'fullname': 'Test Customer',
                'phone': '0712345678',
            },
        )

    assert first.status_code == 302
    assert limited.status_code == 429
    assert send_sms.call_count == 1

    for _ in range(5):
        response = client.post(
            '/register',
            data={'action': 'verify', 'csrf_token': csrf_token, 'code': '000000'},
        )
        assert response.status_code == 400
    locked = client.post(
        '/register',
        data={'action': 'verify', 'csrf_token': csrf_token, 'code': '000000'},
    )
    assert locked.status_code == 429


def test_customer_login_uses_verified_phone_otp(client, monkeypatch):
    database.query_db(
        'INSERT INTO users (fullname, phone, phone_verified_at) VALUES (%s, %s, %s)',
        ('Existing Customer', '254712345678', '2026-01-01T00:00:00+00:00'),
    )
    monkeypatch.setenv('AFRICASTALKING_USERNAME', 'test-username')
    monkeypatch.setenv('AFRICASTALKING_API_KEY', 'test-api-key')
    monkeypatch.setattr('app.secrets.randbelow', lambda _: 654321)
    sms_response = Mock()
    sms_response.json.return_value = {
        'SMSMessageData': {'Recipients': [{'statusCode': 101}]},
    }

    with patch('app.requests.post', return_value=sms_response):
        client.get('/customer/login')
        with client.session_transaction() as sess:
            csrf_token = sess['csrf_token']
        requested = client.post(
            '/customer/login',
            data={
                'action': 'request',
                'csrf_token': csrf_token,
                'phone': '0712345678',
            },
        )
        assert requested.status_code == 302
        verified = client.post(
            '/customer/login',
            data={
                'action': 'verify',
                'csrf_token': csrf_token,
                'code': '654321',
            },
        )

    assert verified.status_code == 302
    with client.session_transaction() as sess:
        assert sess['customer_name'] == 'Existing Customer'
        assert sess['customer_phone'] == '254712345678'


def test_registration_does_not_create_account_without_otp_provider(client):
    client.get('/register')
    with client.session_transaction() as sess:
        csrf_token = sess['csrf_token']

    response = client.post(
        '/register',
        data={
            'action': 'request',
            'csrf_token': csrf_token,
            'fullname': 'Unverified Customer',
            'phone': '0712345678',
        },
    )

    assert response.status_code == 503
    assert database.query_db(
        'SELECT id FROM users WHERE phone = %s',
        ('254712345678',),
        one=True,
    ) is None


def test_verified_delivery_address_can_be_ordered_and_tracked(client):
    address = {
        'delivery_address': '123 Main Street Nairobi Kenya',
        'delivery_locality': 'Nairobi',
        'delivery_region': '',
        'delivery_postal_code': '00100',
        'delivery_country': 'Kenya',
    }
    geocoder_response = Mock()
    geocoder_response.json.return_value = [{
        'lat': '-1.2864',
        'lon': '36.8172',
        'address': {'postcode': '00100'},
    }]
    with patch('app.requests.get', return_value=geocoder_response):
        verification = client.post('/api/address/verify', json=address)
    assert verification.status_code == 200, verification.get_data(as_text=True)
    payload = verification.get_json()

    order = client.post(
        '/api/mpesa/till-order',
        json={
            **address,
            'address_verification_token': payload['verification_token'],
            'items': [{'name': 'Tea'}],
            'phone': '0712345678',
            'order_type': 'Online',
        },
        headers={'Idempotency-Key': 'checkout-key-address-001'},
    )
    assert order.status_code == 201, order.get_data(as_text=True)
    tracking = client.get(f"/api/order/status/{order.get_json()['order_number']}")
    assert tracking.get_json()['destination']['verified'] is True
    assert tracking.get_json()['destination']['postal_code'] == '00100'


def test_pesapal_checkout_uses_trusted_total_and_is_idempotent(client, monkeypatch):
    monkeypatch.setenv('PESAPAL_CONSUMER_KEY', 'test-key')
    monkeypatch.setenv('PESAPAL_CONSUMER_SECRET', 'test-secret')
    monkeypatch.setenv('PESAPAL_ENVIRONMENT', 'sandbox')
    monkeypatch.setenv('PESAPAL_CALLBACK_URL', 'https://example.com/payment/return')
    monkeypatch.setenv('PESAPAL_NOTIFICATION_ID', 'test-notification-id')
    auth_response = Mock()
    auth_response.json.return_value = {'token': 'test-access-token'}
    payment_response = Mock()
    payment_response.json.return_value = {
        'order_tracking_id': 'pesapal-track-test',
        'merchant_reference': None,
        'redirect_url': 'https://cybqa.pesapal.com/pesapalv3/redirect/test',
    }
    payload = {
        'items': [{'name': 'Tea', 'price': 1}],
        'phone': '0712345678',
        'order_type': 'Pickup',
    }
    headers = {'Idempotency-Key': 'checkout-key-00000001'}

    with patch(
        'app.requests.post',
        side_effect=[auth_response, payment_response, auth_response],
    ) as payment_request:
        response = client.post('/api/pesapal/pay', json=payload, headers=headers)
        retry = client.post('/api/pesapal/pay', json=payload, headers=headers)

    assert response.status_code == 200
    assert retry.status_code == 200
    assert retry.get_json()['redirect_url'] == response.get_json()['redirect_url']
    assert payment_request.call_args_list[1].kwargs['headers']['Authorization'] == 'Bearer test-access-token'
    assert payment_request.call_args_list[1].kwargs['json']['amount'] == 50
    assert payment_request.call_args_list[1].kwargs['json']['currency'] == 'KES'
    assert database.query_db('SELECT COUNT(*) AS total FROM orders', one=True)['total'] == 1
    assert database.query_db('SELECT COUNT(*) AS total FROM payments', one=True)['total'] == 1
    till_retry = client.post(
        '/api/mpesa/till-order',
        json=payload,
        headers=headers,
    )
    assert till_retry.status_code == 409


def test_pesapal_ipn_verifies_amount_before_marking_order_paid(client, monkeypatch):
    monkeypatch.setenv('PESAPAL_CONSUMER_KEY', 'test-key')
    monkeypatch.setenv('PESAPAL_CONSUMER_SECRET', 'test-secret')
    monkeypatch.setenv('PESAPAL_ENVIRONMENT', 'sandbox')
    monkeypatch.setenv('PESAPAL_CALLBACK_URL', 'https://example.com/payment/return')
    monkeypatch.setenv('PESAPAL_NOTIFICATION_ID', 'test-notification-id')
    auth_response = Mock()
    auth_response.json.return_value = {'token': 'test-access-token'}
    payment_response = Mock()
    payment_response.json.return_value = {
        'order_tracking_id': 'pesapal-track-test',
        'merchant_reference': None,
        'redirect_url': 'https://cybqa.pesapal.com/pesapalv3/redirect/test',
    }
    with patch('app.requests.post', side_effect=[auth_response, payment_response]):
        checkout = client.post(
            '/api/pesapal/pay',
            json={'items': [{'name': 'Tea'}], 'phone': '0712345678', 'order_type': 'Pickup'},
            headers={'Idempotency-Key': 'checkout-key-00000002'},
        )
    assert checkout.status_code == 200
    order_number = checkout.get_json()['OrderNumber']

    provider_status = Mock()
    provider_status.json.return_value = {
        'status': '200',
        'payment_status_description': 'Completed',
        'amount': 51.0,
        'currency': 'KES',
        'confirmation_code': 'PESA123',
        'merchant_reference': order_number,
    }
    with patch('app.requests.post', return_value=auth_response), patch(
        'app.requests.get', return_value=provider_status
    ):
        mismatch = client.get(
            '/api/pesapal/ipn',
            query_string={
                'OrderTrackingId': 'pesapal-track-test',
                'OrderMerchantReference': order_number,
            },
        )
    assert mismatch.status_code == 200
    assert mismatch.get_json()['status'] == 'Review Required'
    assert database.query_db(
        'SELECT status FROM orders WHERE order_number = %s', (order_number,), one=True
    )['status'] == 'Payment Prompt Sent'

    provider_status.json.return_value['amount'] = 50.0
    with patch('app.requests.post', return_value=auth_response), patch(
        'app.requests.get', return_value=provider_status
    ):
        verified = client.get(
            '/api/pesapal/ipn',
            query_string={
                'OrderTrackingId': 'pesapal-track-test',
                'OrderMerchantReference': order_number,
            },
        )
    assert verified.status_code == 200
    assert verified.get_json()['status'] == 'Completed'
    assert database.query_db(
        'SELECT status FROM orders WHERE order_number = %s', (order_number,), one=True
    )['status'] == 'Paid'
    payment = database.query_db(
        'SELECT status, receipt FROM payments WHERE merchant_reference = %s',
        (order_number,),
        one=True,
    )
    assert payment == {'status': 'Completed', 'receipt': 'PESA123'}

    with patch('app.requests.post', return_value=auth_response), patch(
        'app.requests.get', return_value=provider_status
    ):
        duplicate_ipn = client.get(
            '/api/pesapal/ipn',
            query_string={
                'OrderTrackingId': 'pesapal-track-test',
                'OrderMerchantReference': order_number,
            },
        )
    assert duplicate_ipn.status_code == 200
    assert duplicate_ipn.get_json()['status'] == 'Completed'
    assert database.query_db(
        'SELECT COUNT(*) AS total FROM payments WHERE order_number = %s',
        (order_number,),
        one=True,
    )['total'] == 1
