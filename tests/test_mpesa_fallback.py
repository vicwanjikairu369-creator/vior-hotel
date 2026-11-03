import pytest
from unittest.mock import Mock, patch

from app import app
import database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_PATH', str(tmp_path / 'vior-hotel-test.db'))
    for key in ('MPESA_CONSUMER_KEY', 'MPESA_CONSUMER_SECRET', 'MPESA_SHORTCODE', 'MPESA_PASSKEY', 'CALLBACK_URL'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(database, '_initialized', False)
    app.config['TESTING'] = True
    app.debug = True
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['customer_name'] = 'Test User'
            sess['customer_phone'] = '254712345678'
        yield client


def test_mpesa_pay_has_fallback_when_credentials_missing(client, monkeypatch):
    monkeypatch.delenv('MPESA_CONSUMER_KEY', raising=False)
    monkeypatch.delenv('MPESA_CONSUMER_SECRET', raising=False)
    monkeypatch.delenv('MPESA_SHORTCODE', raising=False)
    monkeypatch.delenv('MPESA_PASSKEY', raising=False)
    monkeypatch.delenv('CALLBACK_URL', raising=False)

    response = client.post(
        '/api/mpesa/pay',
        json={
            'items': [{'name': 'Tea'}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
    )

    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload['ResponseCode'] == '0'
    assert payload['CheckoutRequestID']
    assert payload['OrderNumber'].startswith('VH-')


def test_mpesa_pay_has_fallback_when_auth_fails(client, monkeypatch):
    monkeypatch.setenv('MPESA_CONSUMER_KEY', 'test-key')
    monkeypatch.setenv('MPESA_CONSUMER_SECRET', 'test-secret')
    monkeypatch.setenv('MPESA_SHORTCODE', '174379')
    monkeypatch.setenv('MPESA_PASSKEY', 'test-passkey')
    monkeypatch.setenv('CALLBACK_URL', 'https://example.com/mpesa/callback')
    with patch('app.requests.get', side_effect=Exception('network down')):
        response = client.post(
            '/api/mpesa/pay',
            json={
                'items': [{'name': 'Tea'}],
                'phone': '0712345678',
                'order_type': 'Pickup',
            },
        )

    assert response.status_code == 200, response.get_data(as_text=True)
    payload = response.get_json()
    assert payload['ResponseCode'] == '0'
    assert payload['CheckoutRequestID']


def test_main_pages_render_and_admin_requires_staff_login(client):
    for path in ('/', '/cart', '/register', '/login'):
        response = client.get(path)
        assert response.status_code == 200, path
    response = client.get('/admin')
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/login')


def test_till_order_uses_server_menu_prices_and_can_be_tracked(client):
    response = client.post(
        '/api/mpesa/till-order',
        json={
            'items': [{'name': 'Tea', 'price': 1}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    order = response.get_json()
    assert order['amount'] == 50
    assert order['till_number'] == '3484738'

    tracking = client.get(f"/api/order/status/{order['order_number']}")
    assert tracking.status_code == 200
    assert tracking.get_json()['status'] == 'Pending'


def test_mpesa_rejects_items_not_on_the_current_menu(client):
    response = client.post(
        '/api/mpesa/pay',
        json={
            'items': [{'name': 'Unknown meal', 'price': 1}],
            'phone': '0712345678',
            'order_type': 'Pickup',
        },
    )
    assert response.status_code == 400
    assert 'not on the current menu' in response.get_json()['error']


def test_staff_login_opens_admin_dashboard(client, monkeypatch):
    monkeypatch.setenv('ADMIN_PHONE', '254700000000')
    monkeypatch.setenv('ADMIN_PASSWORD', 'test-password')
    response = client.post(
        '/login',
        data={'phone': '0700000000', 'admin_password': 'test-password'},
    )
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin')
    assert client.get('/admin').status_code == 200


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
    )
    assert order.status_code == 201, order.get_data(as_text=True)
    tracking = client.get(f"/api/order/status/{order.get_json()['order_number']}")
    assert tracking.get_json()['destination']['verified'] is True
    assert tracking.get_json()['destination']['postal_code'] == '00100'


def test_mpesa_success_uses_bearer_token_and_trusted_total(client, monkeypatch):
    monkeypatch.setenv('MPESA_CONSUMER_KEY', 'test-key')
    monkeypatch.setenv('MPESA_CONSUMER_SECRET', 'test-secret')
    monkeypatch.setenv('MPESA_SHORTCODE', '174379')
    monkeypatch.setenv('MPESA_PASSKEY', 'test-passkey')
    monkeypatch.setenv('CALLBACK_URL', 'https://example.com/mpesa/callback')
    auth_response = Mock()
    auth_response.json.return_value = {'access_token': 'test-access-token'}
    payment_response = Mock()
    payment_response.json.return_value = {
        'ResponseCode': '0',
        'CheckoutRequestID': 'ws_CO_test',
        'CustomerMessage': 'Success. Request accepted for processing',
    }
    with patch('app.requests.get', return_value=auth_response), patch(
        'app.requests.post', return_value=payment_response
    ) as payment_request:
        response = client.post(
            '/api/mpesa/pay',
            json={
                'items': [{'name': 'Tea', 'price': 1}],
                'phone': '0712345678',
                'order_type': 'Pickup',
            },
        )
    assert response.status_code == 200
    assert response.get_json()['Fallback'] is False
    assert response.get_json()['OrderNumber'].startswith('VH-')
    assert payment_request.call_args.kwargs['headers']['Authorization'] == 'Bearer test-access-token'
    assert payment_request.call_args.kwargs['json']['Amount'] == 50
