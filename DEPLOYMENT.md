# Deployment configuration

## Required production settings

Configure these values in the hosting provider's environment-variable settings.
For Vercel, use **Project Settings → Environment Variables**, choose the
environments that need the values, save, and redeploy.

| Variable | Required | Purpose |
| --- | --- | --- |
| `SECRET_KEY` | Yes | Stable Flask session and OTP-verification signing key. Generate a random value, for example with `python -c "import secrets; print(secrets.token_hex(32))"`. |
| `DATABASE_URL` | Yes for production | PostgreSQL connection string. SQLite is the local-development fallback and is not durable storage for Vercel functions. |
| `STAFF_SETUP_TOKEN` | First-time owner setup | Random secret of at least 32 characters, used once to register the first owner account. |
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
| `WHATSAPP_ACCESS_TOKEN` | For WhatsApp receipts | Meta WhatsApp Cloud API access token. Keep it server-side. |
| `WHATSAPP_PHONE_NUMBER_ID` | For WhatsApp receipts | Meta phone-number ID for the WhatsApp Business sender. |
| `WHATSAPP_RECEIPT_TEMPLATE` | For WhatsApp receipts | Name of the approved receipt template, with five body parameters configured as described below. |
| `WHATSAPP_TEMPLATE_LANGUAGE` | For WhatsApp receipts | Language code configured on the approved Meta template (defaults to `en`). |
| `WHATSAPP_STATUS_TEMPLATE` | Optional | Approved order-status template that accepts three body parameters. |
| `WHATSAPP_GRAPH_VERSION` | Optional | Meta Graph API version used by the sender. |
| `PUBLIC_BASE_URL` | For WhatsApp receipt links | Public HTTPS origin used to construct signed receipt links, without a trailing slash. |
| `CRON_SECRET` | For outbox retries | Long random bearer secret required by the protected notification retry endpoint. |

Do not commit these values, add them to frontend code, or paste them into source
files. `.env` is ignored for local development; use the hosting dashboard for
deployed values. Keep `SECRET_KEY` unchanged between deployments so customer
sessions and pending OTP challenges remain valid. Rotating it signs out users
and invalidates pending verifications.

## Staff dashboard and media publishing

Staff sign-in uses individual accounts stored in the application database;
the legacy `ADMIN_PHONE` and `ADMIN_PASSWORD` environment variables are no
longer used for authentication. Remove those old variables from the hosting
environment after deploying this version. The `/admin` dashboard remains
protected and requires an authorized staff account.

To register the first owner, add a random `STAFF_SETUP_TOKEN` of at least 32
characters to the hosting environment and redeploy. Open `/staff/setup` and
register the owner using that token, a Kenyan mobile number, and a strong
password. The first successful registration claims setup and closes the page.
The owner can then add manager, accountant, kitchen, or delivery staff from the
dashboard. Keep the token private and remove it from hosting settings after
the owner account is successfully created.

For quick access, open `/login` (or `/admin`, which redirects to sign-in).
On first setup, follow the owner-setup link on the sign-in page, choose the
owner password there, and use that phone number and password for future sign-ins.
Owners can change their password from the Staff section in the dashboard after
confirming the current password. Do not hard-code a password in application
source or share it in deployment configuration.

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

## Receipts and WhatsApp notifications

After Pesapal verifies a payment, or an authorized staff member verifies a Till
payment, the application records the payment and creates a receipt notification
only when the customer explicitly opted in to WhatsApp at checkout. The message
contains the order number, item summary, total, payment reference, and a
time-limited signed receipt link. The receipt is an HTML page customers can
print or save as PDF from their browser; the application does not currently
generate an attached PDF.

To enable delivery:

1. Configure the WhatsApp Business Cloud API sender and set
   `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, and `PUBLIC_BASE_URL`
   in the hosting environment.
2. Create and obtain Meta approval for a utility message template with five
   body placeholders, in this order: order number, item summary, total,
   payment reference, secure receipt URL. Set its exact name in
   `WHATSAPP_RECEIPT_TEMPLATE` and approved language in
   `WHATSAPP_TEMPLATE_LANGUAGE`.
3. If order-status WhatsApp messages are desired, configure a separate
   approved template accepting three body placeholders and set
   `WHATSAPP_STATUS_TEMPLATE`.
4. Configure a random `CRON_SECRET`. Schedule an external HTTPS scheduler to
   call `POST /api/cron/notifications` with
   `Authorization: Bearer <CRON_SECRET>` regularly. This endpoint retries
   pending notifications with backoff; the initial send is attempted when the
   payment is verified. Vercel Cron is not configured in this repository.
5. Test using a Meta test recipient and a test order. Confirm payment
   verification, explicit opt-in, template delivery, receipt link expiry, and a
   retry after a temporary provider error before enabling live traffic.

Missing credentials or an unapproved/misconfigured template prevents successful
delivery; configuring the application does not provision a Meta Business
account, approve templates, or guarantee message delivery. Check the
notification outbox in the staff dashboard for delivery state and errors.

## Scope and operational notes

The application includes customer accounts and order history, basic staff
roles, inventory/recipe tracking, delivery assignments, and finance records in
addition to opt-in WhatsApp receipts. Verify these features in the deployed
environment and configure the required credentials and business data first.

This is not yet a full hotel ERP: loyalty point redemption and configurable
rewards, delivery-zone pricing, comprehensive bilingual coverage, advanced
accounting/analytics, and staff shifts/attendance are not implemented. Inventory
deduction requires staff to configure recipes for menu items; it does not infer
ingredients automatically.
