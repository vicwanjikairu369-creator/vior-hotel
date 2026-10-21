import pytest
from unittest.mock import patch

from app import app


@pytest.fixture
def client():
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


def test_mpesa_pay_has_fallback_when_auth_fails(client):
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
