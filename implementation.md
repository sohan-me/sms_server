# Implementation Brief — SMS Channel Client

This is the implementation side of the system: a self-contained brief to paste
into an AI coding agent, or to read yourself, when building or updating a client.

[`integration.md`](integration.md) is the **protocol reference** — every endpoint,
frame shape, status code and close code comes from there. Use it whenever the
brief is ambiguous, and do not add endpoints, fields or events it does not
describe.

## How to use it

1. Replace `<BASE_URL>` with the deployment you are targeting (the example host
   in the brief is `https://server-v2.orbitalcore.site`) and `<host>` in the
   WebSocket URLs with the same host, without the scheme.
2. Paste the block below into the agent as its task description.
3. Have the agent work through the acceptance checklist at the end of the block
   before calling the integration done.

## The brief

```text
You are integrating a client with the SMS OTP + payment server at <BASE_URL>
(example host: https://server-v2.orbitalcore.site). Read the whole brief before
writing code. Do not invent fields, endpoints or events that are not listed.

GOAL
The client runs on a device that receives bank/wallet SMS (IVAC sign-in codes,
bKash/Nagad/Rocket payment OTPs and transaction receipts). It must show the
right code to the right screen, and must never show one user's payment data to
another user.

STEP 1 — IDENTIFY THE DEVICE (once, then cache the token)
  POST /api/register  {name, mac_address, motherboard_serial, machine_guid,
                       bios_serial}            (at least 2 signature fields)
    -> 201 {user_id}   the device is now PENDING: no sockets, no data.
    -> 409            this MAC is already registered.
  POST /api/authenticate {same signature fields}
    -> 200 {ws_token}  store it; it authenticates BOTH HTTP and WebSocket calls.
    -> 403             pending approval or expired licence.
    -> 404             fingerprint not registered.
  Never poll /api/authenticate in a loop. Retry on user action or after 401/403.

STEP 2 — CLAIM THE PAYMENT NUMBERS (before the first payment, every run)
  POST /api/payment/numbers?token=<ws_token>
       {"numbers": ["01712345678","01876543210"],
        "mac_address": "...", "motherboard_serial": "..."}
    -> 200 {numbers: [{phone, payment_count}], reclaimed?: [phone]}
    -> 409 {error, numbers: [...]}  another device owns one of them; do not
                                     retry, surface it to the user.
  Safe to call before every payment run: claiming again is a no-op and the
  counters are preserved. GET /api/payment/numbers?token=... lists them;
  DELETE /api/payment/numbers/<phone>?token=... releases one (counter resets).

STEP 3 — OPEN ONE SOCKET PER CHANNEL
  wss://<host>/ws/otp/signin?token=<ws_token>        sign-in codes (IVAC)
  wss://<host>/ws/otp/payment?token=<ws_token>       payment OTPs
  wss://<host>/ws/otp/transaction?token=<ws_token>   payment receipts
  Then subscribe: {"action":"subscribe","phones":["01712345678"]}
  Ack: {"type":"subscribed","phones":["01712345678"]} (phones normalised).
  A socket only ever receives its own channel. Never multiplex channels on one
  socket; never send a subscribe frame before the connection is open.

STEP 4 — HANDLE FRAMES
  signin:      {kind:"signin", phone, receiver, sender, sender_number,
                last_updated, otp}
  payment:     {kind:"payment", ..., otp, payment_count}
  transaction: {kind:"transaction", ..., trx, amount?, payment_count}
  - Dispatch on `kind`.
  - Read `otp` for sign-in/payment, `trx` for a transaction. A transaction has
    NO `otp` key; do not read one.
  - `phone` is the receiving number (canonical 01XXXXXXXXX); `receiver` repeats
    it; `sender` is the wallet/provider label ("bKash", "Nagad", "Rocket",
    "IVAC"); `sender_number` is the raw forwarder address and may be "".
  - `payment_count` is per number: from a transaction it counts that payment
    (1 for the first file paid with the number, 2 for the next); from a payment
    OTP it is how many payments had completed when the OTP arrived.
  - An error frame {"type":"error","error":"..."} does not close the socket and
    keeps the previous subscription.

STEP 5 — HTTP FALLBACK (when a frame was missed or the app was closed)
  GET /api/signin/<phone>                  open, no token
  GET /api/payment/<phone>?token=...       owner only
  GET /api/transaction/<phone>?token=...   owner only, latest only
  GET /api/transaction/<phone>/history?limit=50&token=...   owner only
  Each returns the same object the socket would have pushed, without `type`.
  {} means "nothing has arrived yet" — keep polling, it is not an error.
  401 = missing/unknown token (re-authenticate); 403 = number not yours or
   device not approved.

INVARIANTS YOU MUST KEEP
  1. Claim a number before paying from it. Unclaimed numbers are stored and
     delivered to nobody, which looks like silence, not an error.
  2. One number may pay many files; keep its counter and label the files with it.
     Never reset it locally — take it from the frames or the numbers endpoint.
  3. Never assume a wallet from the SMS body alone: the server resolves the brand
     from the body and the sender shortcode and reports it as `sender`.
  4. A retried transaction SMS is not a second payment (`duplicate: true`, no
     frame, no counter change): make your client idempotent on `trx`.
  5. On WebSocket close 4001, re-authenticate and reconnect; on 4003, stop and
     tell the user their device needs approval. Back off on 1011/1000 (1s→15s).
  6. After a token rotation the old token stops working everywhere and live
     sockets are closed. Re-authenticate, reconnect, and re-claim your numbers
     with the same fingerprint — they come back with their counters.

ACCEPTANCE CHECKLIST FOR YOUR CODE
  [ ] register -> pending -> authenticate -> claim -> connect -> subscribe
  [ ] three sockets, one per channel, each subscribed to the user's numbers
  [ ] a sign-in OTP renders as a code; a payment OTP goes to the payment screen
  [ ] a transaction shows trx + amount and the counter it settled
  [ ] two files paid with one number show 1 then 2; a second number starts at 1
  [ ] a second device never receives the first device's payment frames
  [ ] {} from HTTP renders "waiting", not an error
  [ ] reconnect + re-authenticate after 4001, and after a token rotation
```
