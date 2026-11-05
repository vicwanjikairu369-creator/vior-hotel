# Deployment configuration

## Required production settings

Configure these values in the hosting provider's environment-variable settings.
For Vercel, use **Project Settings → Environment Variables**, choose the
environments that need the values, save, and redeploy.

| Variable | Required | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | Yes | Stable Flask session and OTP-verification signing key. Generate a random value, for example with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `DATABASE_URL` | Yes for production | PostgreSQL connection string. SQLite is the local-development fallback and is not durable storage for Vercel functions. |
| `ADMIN_PHONE` | For staff access | The Kenyan mobile number authorized for the current administrator login. |
| `ADMIN_PASSWORD` | For staff access | A unique, long administrator password. Store it only in the hosting provider's environment settings. |
| `CLOUDINARY_CLOUD_NAME` | For admin media uploads | Cloudinary cloud name. |
| `CLOUDINARY_API_KEY` | For admin media uploads | Cloudinary API key; used to sign short-lived uploads. |
| `CLOUDINARY_API_SECRET` | For admin media uploads | Cloudinary API secret. Server-side only; never expose or commit it. |
| `AFRICASTALKING_USERNAME` | For phone verification | Africa's Talking account username for the selected environment. |
| `AFRICASTALKING_API_KEY` | For phone verification | Africa's Talking API key. Keep it server-side. |
| `AFRICASTALKING_SENDER_ID` | Optional | An approved sender ID configured in Africa's Talking. |
| `PESAPAL_CONSUMER_KEY` | For online payments | Pesapal API consumer key for the selected environment. |
| `PESAPAL_CONSUMER_SECRET` | For online payments | Pesapal API consumer secret. Keep it server-side. |
| `PESAPAL_ENVIRONMENT` | For online payments | `sandbox` for testing or `live` for production. |
| `PESAPAL_CALLBACK_URL` | For online payments | Public HTTPS customer-return URL, normally `https://<domain>/payment/return`. |
| `PESAPAL_NOTIFICATION_ID` | For online payments | IPN ID returned when registering `https://<domain>/api/pesapal/ipn` with Pesapal. |
| `MPESA_TILL_NUMBER` | Optional | Till number displayed for manual Till orders; defaults to the current configured business Till. |

Do not commit these values, add them to frontend code, or paste them into source
files. `.env` is ignored for local development; use the hosting dashboard for
deployed values. Keep `SECRET_KEY` unchanged between deployments so customer
sessions and pending OTP challenges remain valid. Rotating it signs out users
and invalidates pending verifications.

## Staff dashboard and media publishing

The staff login uses `ADMIN_PHONE` and `ADMIN_PASSWORD`; there is no built-in
or recoverable default password. Generate a strong password locally, then set
both values in Vercel **Project Settings → Environment Variables** and redeploy.
Do not share the password in chat, source code, or a public issue. The protected
`/admin` dashboard retains order operations and adds database-backed dish and
story publishing.

Dish/story images and videos are uploaded directly from the staff browser to
Cloudinary. The Flask server creates a signed upload request; the Cloudinary
secret remains server-side. Configure the three Cloudinary variables above in
Vercel to enable uploads. Without them, the admin upload endpoint reports a
configuration error rather than accepting an unpersisted local file. Story
publication dates and visibility are stored in the database; new menu items
are priced and checked server-side during checkout.

The current upload form accepts JPG, PNG, and WebP images and MP4, MOV, and
WebM videos. Apply an appropriate upload-size limit in the Cloudinary account
for your plan and content needs.

## Phone verification

Registration and customer sign-in send a six-digit OTP through the Africa's
Talking SMS API. Challenges expire after five minutes, allow at most five
verification attempts, and are rate-limited per phone number and request IP.
The account is created or signed in only after a valid code is confirmed.

The application returns a configuration error instead of creating an
unverified account when the SMS credentials are missing or the provider does
not accept the message. Test the provider account and sender ID before enabling
registration for customers.

## Pesapal M-Pesa checkout

The customer checkout submits orders server-side to Pesapal API 3.0 and redirects
the customer to Pesapal's hosted payment page. The browser return and IPN
endpoints query Pesapal's transaction-status API before marking an order paid.
Missing configuration or provider failures do not produce a payment-success
response; customers can choose the separate Till flow instead.

1. Obtain the consumer key and secret from the correct Pesapal sandbox or live
   merchant account. Set `PESAPAL_ENVIRONMENT` to match those credentials.
2. Deploy the application on its final HTTPS domain and set
   `PESAPAL_CALLBACK_URL` to `https://<domain>/payment/return`.
3. Register `https://<domain>/api/pesapal/ipn` as a GET IPN with Pesapal API 3.0
   (`POST /api/URLSetup/RegisterIPN`) using that environment's access token.
   Store the returned `ipn_id` as `PESAPAL_NOTIFICATION_ID`.
4. Add all values as Vercel environment variables and redeploy. Test payment
   initiation, a completed transaction, a failed transaction, and repeated IPN
   delivery in sandbox before switching to live credentials.

Pesapal's official [API 3.0 reference](https://developer.pesapal.com/how-to-integrate/e-commerce/api-30-json/api-reference)
documents the sandbox and live base URLs. Never expose the consumer secret or
access token to browser code.

## Still not implemented

WhatsApp Cloud API messaging, approved templates, notification retries, and
receipts are not wired yet. Loyalty, full customer/staff roles, accounting,
inventory/suppliers, and a complete finance/analytics dashboard also remain
future work. Do not configure production WhatsApp credentials until that
server-side integration is implemented.
