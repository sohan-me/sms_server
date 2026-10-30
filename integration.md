# Integration Guide — SMS Channels

The SMS server splits inbound SMS into three channels and delivers each one on its
own WebSocket and its own HTTP endpoint. A sign-in OTP, a payment OTP and a
transaction record can never be confused for one another, because they live on
different sockets and different rows.

Every example below is a real request/response captured from a running server.

This file is the **protocol reference**. For a ready-to-paste brief that turns it
into a client, see [`implementation.md`](implementation.md).

---

## 1. At a glance

| Channel | What it carries | WebSocket | HTTP (latest) |
|---|---|---|---|
| **signin** | IVAC account verification codes | `wss://<host>/ws/otp/signin` | `GET /api/signin/<phone>` |
| **payment** | Wallet/app OTPs (bKash, Nagad, DBBL, Rocket, Upay) | `wss://<host>/ws/otp/payment` | `GET /api/payment/<phone>` |
| **transaction** | Money received + TRX ID + amount | `wss://<host>/ws/otp/transaction` | `GET /api/transaction/<phone>` |
| — | Legacy sign-in socket (do not use for new clients) | `wss://<host>/ws/otp` | `GET /api/messages/<phone>` |
| — | Full transaction history | — | `GET /api/transaction/<phone>/history` |
| — | Ingest an SMS (SMS forwarder only) | — | `POST /api/messages` |
| — | Claim / list your payment numbers | — | `POST` / `GET /api/payment/numbers` |
| — | Release a payment number | — | `DELETE /api/payment/numbers/<phone>` |
| — | Worker health | — | `GET /api/health` |

A WebSocket is **bound to one channel by its URL**. There is no "all channels"
socket: a client that needs sign-in and transaction codes opens two sockets.

---

## 2. Conventions

**Phone numbers.** Every endpoint normalises with the same rule: strip `+`,
spaces, dashes and brackets; drop a leading `880`; add a leading `0` to a bare
10-digit `1XXXXXXXXX`. So all of these are the same number:

```
01712345678   8801712345678   +880 1712-345678   (017) 12345678
```

The canonical form (`01XXXXXXXXX`) is what appears in `phone`, in the subscribe
ack and in every frame.

**`last_updated`.** BDT, ISO-8601 with milliseconds and a `+06:00` offset:

```
2026-10-29T05:17:21.618+06:00
```

**Retention.** A stored code is readable for `OTP_RETENTION_MINUTES` (default
**30**) after arrival. Older rows are deleted lazily by the read endpoints, so an
old code returns `{}` rather than an error.

**`sender` vs `sender_number`.** `sender_number` is the raw address the SMS
forwarder reported (may be a shortcode `16216` or a text ID like `bKash`); `sender`
is the provider label the server resolved (`IVAC`, `bKash`, `Nagad`, `Rocket`, ...).
Either can be `""` when the server could not tell. The wallet shortcodes `16216`,
`16247` and `16167` resolve to Rocket, bKash and Nagad respectively, which is how a
transaction template that never names its wallet gets a label.

**`receiver`** is the phone the SMS belongs to — the same value as `phone`, kept
as a separate key for clients that read that name.

**Payment numbers are claimed first.** The `payment` and `transaction` channels
are private: a device declares the numbers it will pay from with
`POST /api/payment/numbers`, and those frames then reach only that device's
sockets and token-authenticated HTTP reads. `signin` is unchanged — no claim
needed, and its HTTP read stays open.

**`payment_count`** is per claimed number: how many payments that number has
completed. The first file paid with a number gives `1`, the second file with the
same number `2`, and so on. A repeated TRX ID is the same payment and does not
count again. A payment OTP carries the count *before* that payment.

---

## 3. Authentication

### 3.1 Device token (required for every WebSocket)

`POST /api/register` once, then `POST /api/authenticate` to obtain `ws_token`.

```http
POST /api/register
Content-Type: application/json

{
  "name": "bot-01",
  "mac_address": "AA:BB:CC:DD:EE:01",
  "motherboard_serial": "MB-1234",
  "machine_guid": "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0",
  "bios_serial": "BIOS-99"
}
```

At least **two** of the four signature fields are required. Response `201`:

```json
{ "message": "Registration successful. Waiting for admin approval.", "user_id": 7 }
```

A new device starts inactive; an admin must approve it before it can authenticate.
`409` means that MAC is already registered.

```http
POST /api/authenticate
Content-Type: application/json

{ "mac_address": "AA:BB:CC:DD:EE:01", "motherboard_serial": "MB-1234" }
```

Response `200`:

```json
{
  "message": "Authentication successful",
  "status": "active",
  "user": { "id": 7, "name": "bot-01" },
  "ws_token": "5vT3n...64-chars..."
}
```

| Status | Body | Meaning |
|---|---|---|
| `400` | `{"error": "At least 2 device signature fields are required"}` | fewer than 2 signatures |
| `404` | `{"error": "Device not found"}` | no matching device |
| `403` | `{"error": "Account pending admin approval", "status": "inactive"}` | not approved yet |
| `403` | `{"error": "Access expired", "status": "expired"}` | licence expired (open sockets are closed) |
| `409` | `{"error": "Ambiguous device fingerprint"}` | 2+ devices match |

The token is stable per device; re-authenticating returns the same one. An admin
can rotate it, which invalidates the old value immediately.

### 3.2 Sending the token on a WebSocket

Either place works and both are checked:

```
wss://<host>/ws/otp/signin?token=<ws_token>
X-WS-Token: <ws_token>
```

A server-wide `WS_AUTH_TOKEN` env value is also accepted when set; that is a
deployment convenience for internal bots, not a per-device licence.

**HTTP authentication.** Sign-in and legacy reads are open. Payment and
transaction reads and the payment-number endpoints require the device token, and
answer only for numbers that device claimed (the server-wide `WS_AUTH_TOKEN` is
unrestricted). Either way, keep the HTTP API on the private network or behind the
reverse proxy.

---

## 4. WebSocket protocol

### 4.1 Connect → subscribe → receive

```
client                                              server
  │  GET wss://host/ws/otp/payment?token=…             │
  │───────────────────────────────────────────────────►│  auth check
  │◄──────────── 101 Switching Protocols ──────────────│
  │                                                     │
  │  {"action":"subscribe","phones":["01712345678"]}    │
  │───────────────────────────────────────────────────►│
  │  {"type":"subscribed","phones":["01712345678"]}     │
  │◄───────────────────────────────────────────────────│
  │                                                     │
  │◄──── {"kind":"payment", … "otp":"456789"} ──────────│  on SMS arrival
```

**Nothing is delivered until you subscribe.** A connected but unsubscribed socket
receives no frames at all. This is the most common integration mistake.

### 4.2 Subscribe frame

```json
{ "action": "subscribe", "phones": ["01712345678", "01876543210"] }
```

- Replaces the connection's phone set; send it again to change the set.
- There is no `unsubscribe` action — send a new list, or send
  `{"action":"subscribe","phones":["01700000000"]}` to watch something else.
- Every subscription, including a re-subscribe, is acknowledged:

```json
{ "type": "subscribed", "phones": ["01712345678"] }
```

Phones are normalised before echoing, so you get canonical `01XXXXXXXXX` values.

### 4.3 The three delivered frames

**signin** — IVAC verification code:

```json
{
  "kind": "signin",
  "phone": "01712345678",
  "receiver": "01712345678",
  "sender": "IVAC",
  "sender_number": "8801999999999",
  "last_updated": "2026-10-29T05:17:21.618+06:00",
  "otp": "123456"
}
```

**payment** — wallet OTP:

```json
{
  "kind": "payment",
  "phone": "01712345678",
  "receiver": "01712345678",
  "sender": "bKash",
  "sender_number": "bKash",
  "last_updated": "2026-10-29T05:17:21.623+06:00",
  "otp": "456789",
  "payment_count": 0
}
```

**transaction** — money received:

```json
{
  "kind": "transaction",
  "phone": "01712345678",
  "receiver": "01712345678",
  "sender": "bKash",
  "sender_number": "bKash",
  "last_updated": "2026-10-29T05:17:21.628+06:00",
  "trx": "9HABCDEFG1",
  "amount": "500.00",
  "payment_count": 1
}
```

Field reference:

| Field | signin | payment | transaction | Notes |
|---|---|---|---|---|
| `kind` | ✓ | ✓ | ✓ | `signin` \| `payment` \| `transaction` |
| `phone` | ✓ | ✓ | ✓ | canonical `01XXXXXXXXX` |
| `receiver` | ✓ | ✓ | ✓ | duplicate of `phone` |
| `sender` | ✓ | ✓ | ✓ | provider label, may be `""` |
| `sender_number` | ✓ | ✓ | ✓ | raw forwarder address, may be `""` |
| `otp` | ✓ | ✓ | **never** | digits only |
| `trx` | — | — | ✓ | transaction ID |
| `amount` | — | — | usually | decimal string, e.g. `"500.00"`; absent if the SMS had no parseable amount (e.g. a `Txn ID:`-only message) |
| `payment_count` | — | ✓ | ✓ | completed payments on this number; the transaction includes its own, a payment OTP is the count before it |
| `last_updated` | ✓ | ✓ | ✓ | arrival time, BDT |

**A transaction frame has no `otp` key at all**, so a client that only looks for
`otp` can never pick up an amount as a code.

### 4.4 Channel isolation

Each socket only ever sees its own channel. With one phone subscribed on all
three sockets, a single bKash payment message produced exactly one frame, on the
payment socket only:

```
signin      <- (nothing)
payment     <- {"kind":"payment", …, "otp":"456789"}
transaction <- (nothing)
```

The same applies per channel: a bKash payment OTP never lands on a signin socket,
and it never overwrites a stored IVAC code for the same number.

### 4.5 Error frames

The socket stays open; the previous subscription remains in force.

```json
{ "type": "error", "error": "Invalid JSON" }
```

| Subscribe problem | `error` value |
|---|---|
| body is not JSON | `Invalid JSON` |
| missing/incorrect `action` | `Expected a subscribe action` |
| `phones` not a non-empty array | `phones must be a non-empty array` |
| an entry is not a string | `Each phone must be a string` |
| an entry has no digits | `Each phone must contain digits` |

### 4.6 Close codes

| Code | Reason | What the client should do |
|---|---|---|
| `4001` | `Unauthorized` | token missing or unknown → re-authenticate, reconnect |
| `4003` | `Expired` | licence expired (device was also deactivated) → contact admin |
| `4003` | `Inactive` | device not approved / deactivated → contact admin |
| `4003` | `Access revoked` | admin action on a live socket → re-authenticate later |
| `1011` | — | server failed to write to the socket; reconnect |
| `1000` / `1005` | normal / no status | closed by peer or proxy; reconnect if you still need frames |

Do **not** retry `4001` in a tight loop: re-run `/api/authenticate` first. Retry
`1011` and `1000` with backoff (1s → 15s).

### 4.7 Keepalive

The server sends a WebSocket ping every `WS_PING_INTERVAL` (default **25s**).
Browser and Node clients answer pongs automatically; a client that does not will
be dropped because it looks dead. No application-level heartbeat is needed, and
sending an empty text frame is not required.

### 4.8 Legacy `/ws/otp`

Still supported for existing clients: sign-in channel only, and frames contain
exactly the three original keys — no `kind`, `sender` etc.

```json
{ "otp": "123456", "phone": "01712345678", "last_updated": "2026-10-29T05:17:24.769+06:00" }
```

Subscribe works identically. New clients should use `/ws/otp/signin` instead.

### 4.9 Minimal client

```python
import json
from simple_websocket import Client

ws = Client("wss://server-v2.orbitalcore.site/ws/otp/payment?token=" + TOKEN)
ws.send(json.dumps({"action": "subscribe", "phones": ["01712345678"]}))
print(ws.receive())                      # {"type": "subscribed", ...}
while True:
    frame = json.loads(ws.receive())     # blocks until an OTP arrives
    if frame.get("type") in ("subscribed", "error"):
        continue
    print(frame["kind"], frame.get("otp") or frame.get("trx"))
```

```javascript
const ws = new WebSocket(`wss://server-v2.orbitalcore.site/ws/otp/signin?token=${token}`);
ws.onopen = () => ws.send(JSON.stringify({ action: "subscribe", phones: ["01712345678"] }));
ws.onmessage = (e) => {
  const frame = JSON.parse(e.data);
  if (frame.type === "subscribed" || frame.type === "error") return;
  console.log(frame.kind, frame.otp);
};
```

---

## 5. HTTP endpoints

### 5.1 Ingest — `POST /api/messages`

Called by the SMS forwarder, not by clients. The server classifies the body and
decides the channel; the sender is a label and a hint only.

```http
POST /api/messages
Content-Type: application/json

{
  "receiver": "01712345678",
  "message": "Your bKash OTP for PAYMENT of Tk.3,000.00 to IVAC is 319805.",
  "sender": "16247"
}
```

- `receiver` (the SIM the SMS arrived on) and `message` are required; `message` is
  truncated to 199 characters. `phone` is accepted as the older name for
  `receiver`, and `to` as an alias, so both forwarder shapes work.
- `sender` is the SMS sender address. It is optional but recommended: a shortcode
  labels a template that never names its provider and resolves its channel, and it
  is what identifies the wallet when the body names only the merchant.

Response `201`:

```json
{ "success": true, "message": "Message saved successfully", "routed": "payment" }
```

`routed` is `signin`, `payment`, `transaction` or `unknown`. An `unknown` message
is stored for inspection and delivered nowhere — check this field when a template
stops routing.

| Status | Body |
|---|---|
| `400` | `{"error": "Invalid payload"}` (no JSON body) |
| `400` | `{"error": "Missing phone or message"}` |

A retried POST is safe: the latest row is upserted, and for transactions the
`(phone, trx)` pair is unique, so the same TRX ID is never recorded twice. A
transaction retry answers `201` with `"duplicate": true` and is neither counted
nor pushed again.

### 5.2 Claim the numbers you will pay from

Payment and transaction traffic is private per device, so declare the numbers
before paying. The response is your full list with each number's counter.

```http
POST /api/payment/numbers?token=<ws_token>
Content-Type: application/json

{ "numbers": ["01712345678", "01876543210"] }
```

```json
{
  "numbers": [
    { "phone": "01712345678", "payment_count": 0 },
    { "phone": "01876543210", "payment_count": 0 }
  ]
}
```

- Auth is the same `ws_token` the sockets use (`?token=`, `X-WS-Token` or
  `Authorization: Bearer`). A missing or unknown token is `401`; a pending or
  expired device is `403`.
- Numbers are normalised, deduplicated and claimed once: posting the same list
  again is a no-op, so a client can call this before every payment run without
  losing counters.
- A number already claimed by another device fails the whole request:
  `409 {"error": "Number already registered", "numbers": ["01712345678"]}`.
- **Reclaim after a token rotation.** Numbers belong to the device, not to a
  token, so rotating the `ws_token` never costs a device its claim. Include the
  device fingerprint in the claim and the number is bound to that machine:

  ```json
  {
    "numbers": ["01712345678"],
    "mac_address": "AA:BB:CC:DD:EE:FF",
    "motherboard_serial": "MB1234567"
  }
  ```

  If rotation re-issued the machine as a new device row (new `user_id`, new
  token), send the same fingerprint and the number comes back —
  `payment_count` continues where it was, and the response adds
  `"reclaimed": ["01712345678"]`. A different fingerprint, or a claim made with
  no fingerprint at all, still answers `409`; ownership moved only by proving
  the same machine. (These are the same signature fields `/api/register` and
  `/api/authenticate` use, so nothing new is trusted.)
- `GET /api/payment/numbers?token=…` lists your numbers and counters at any time.
- `DELETE /api/payment/numbers/<phone>?token=…` releases one and resets its
  counter so another device can claim it; a number you do not own is `404`.

An arrival for an unclaimed number is stored but delivered to no device, so a
missing claim looks like silence rather than a leak.

### 5.3 Read the latest code — one endpoint per channel

```
GET /api/signin/<phone>        → signin frame body (open)
GET /api/payment/<phone>       → payment frame body
GET /api/transaction/<phone>   → transaction frame body
```

`payment` and `transaction` require `?token=<ws_token>` (or `X-WS-Token` /
`Authorization: Bearer`) and answer only for a number your device claimed:
`401` without a token, `403` for a number you do not own. Callers using the
server-wide `WS_AUTH_TOKEN` are unrestricted.

Each returns the **same object the WebSocket would have pushed**, without any
`type` wrapper. Example:

```json
{
  "kind": "transaction",
  "phone": "01712345678",
  "receiver": "01712345678",
  "sender": "bKash",
  "sender_number": "bKash",
  "last_updated": "2026-10-29T05:17:21.628+06:00",
  "trx": "9HABCDEFG1",
  "amount": "500.00"
}
```

When nothing has arrived for that channel within the retention window the
response is `200` with an empty object:

```json
{}
```

Treat `{}` as "no code yet", not as an error — poll again.

`/api/transaction/<phone>` returns only the **latest** transaction. Use the
history endpoint for all of them.

### 5.4 Legacy read — `GET /api/messages/<phone>`

Sign-in channel only, original two-key shape:

```json
{ "message": "123456", "last_updated": "2026-10-29T05:17:21.618+06:00" }
```

Kept for existing bots. `GET /api/signin/<phone>` is the same thing with full
field names.

### 5.5 Transaction history — `GET /api/transaction/<phone>/history`

```
GET /api/transaction/01712345678/history?limit=50&token=<ws_token>
```

Private like the latest-transaction read: token required, owner only.

```json
{
  "transactions": [
    {
      "trx": "9HABCDEFG1",
      "amount": "500.00",
      "sender": "bKash",
      "sender_number": "bKash",
      "phone": "01712345678",
      "receiver": "01712345678",
      "last_updated": "2026-10-29T05:17:21.628+06:00"
    }
  ]
}
```

- Newest first.
- `limit` defaults to `50`, clamped to `1..200`; a non-integer gives
  `400 {"error": "limit must be an integer"}`.
- Optional `TRX_HISTORY_RETENTION_DAYS` prunes old rows on read; unset means keep
  everything.

### 5.6 Health — `GET /api/health`

Each request is answered by one worker, so call it several times to see the pool.

```json
{
  "service": "sms",
  "pid": 31710,
  "redis_configured": false,
  "redis_subscriber_alive": false,
  "otp_fanout": "db-poller",
  "db_poller_alive": true,
  "ws_clients": 3,
  "ws_clients_by_channel": { "payment": 1, "signin": 1, "transaction": 1 },
  "database": "sqlite"
}
```

`db_poller_alive` must be `true` on every worker — it is what carries an OTP from
the worker that received the SMS to the worker holding the client's socket.
`otp_fanout: "local-only"` means delivery is broken on that worker.

---

## 6. End-to-end examples

### 6.1 Sign-in OTP

```bash
curl -s -X POST https://server-v2.orbitalcore.site/api/messages \
  -H 'Content-Type: application/json' \
  -d '{"receiver":"01712345678","message":"Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX","sender":"8801999999999"}'
# {"success":true,"message":"Message saved successfully","routed":"signin"}

curl -s https://server-v2.orbitalcore.site/api/signin/01712345678
# {"kind":"signin","otp":"123456", …}
```

On `/ws/otp/signin` the same arrival pushes `"otp": "123456"`. Note the sender
number `8801999999999` had never been seen before — the body decided the channel,
so a new IVAC sender ID cannot break routing.

### 6.2 Payment OTP

```bash
curl -s -X POST https://server-v2.orbitalcore.site/api/messages \
  -H 'Content-Type: application/json' \
  -d '{"receiver":"01712345678","message":"Your bKash OTP is 456789. Do not share with anyone.","sender":"bKash"}'
# {"success":true,"message":"Message saved successfully","routed":"payment"}
```

`/ws/otp/payment` then delivers `"otp": "456789"`; `/api/payment/01712345678?token=<ws_token>`
returns it on demand, once the device has claimed the number. The sign-in code
stored for the same number is untouched.

### 6.3 Transaction

```bash
curl -s -X POST https://server-v2.orbitalcore.site/api/messages \
  -H 'Content-Type: application/json' \
  -d '{"receiver":"01712345678","message":"You have received Tk 500.00 from 01711111111. TrxID 9HABCDEFG1. Your new balance is Tk 1500.00.","sender":"bKash"}'
# {"success":true,"message":"Message saved successfully","routed":"transaction"}
```

`/ws/otp/transaction` delivers:

```json
{"kind":"transaction", … "trx":"9HABCDEFG1","amount":"500.00"}
```

The balance (`1500.00`) is ignored — only the amount received is reported. This
arrival is also appended to the history endpoint.

### 6.4 Several files paid with the same number

One number can pay any number of files. The counter is **per number**, so the
second file paid with `01712345678` continues at `2` — it does not restart, and
another number's payments do not touch it.

```
claim 01712345678, 01876543210

file 1  (01712345678)  payment OTP  -> payment_count 0   (0 completed before it)
                       TRX DJM4DNBFXE -> payment_count 1
file 2  (01712345678)  payment OTP  -> payment_count 1   (1 completed before it)
                       TRX A1B2C3D4E5 -> payment_count 2
file 3  (01876543210)  payment OTP  -> payment_count 0   (its own counter)
                       TRX 75PRV4ZK   -> payment_count 1
```

Frames, in the order they arrive (fields cut for width):

```json
{"kind":"payment","phone":"01712345678","otp":"319805","payment_count":0}
{"kind":"transaction","phone":"01712345678","trx":"DJM4DNBFXE","amount":"3000.00","payment_count":1}
{"kind":"payment","phone":"01712345678","otp":"771204","payment_count":1}
{"kind":"transaction","phone":"01712345678","trx":"A1B2C3D4E5","amount":"1500.00","payment_count":2}
{"kind":"payment","phone":"01876543210","otp":"035568","payment_count":0}
{"kind":"transaction","phone":"01876543210","trx":"75PRV4ZK","amount":"6000.00","payment_count":1}
```

`GET /api/payment/numbers?token=<ws_token>` now answers:

```json
{
  "numbers": [
    { "phone": "01712345678", "payment_count": 2 },
    { "phone": "01876543210", "payment_count": 1 }
  ]
}
```

Double-pay protection is automatic: re-posting a transaction the server already
recorded answers `201 {"duplicate": true}`, moves no counter and pushes no frame,
so a forwarder retry can never turn file 1 into two payments.

A client that wants to drive this loop explicitly can wait on the two frames it
already receives:

```python
count_before = counters["01712345678"]          # from GET /api/payment/numbers
start_payment(phone="01712345678", file_id=42)   # your own flow

payment = wait_frame("payment", phone="01712345678", timeout=120)
assert payment["payment_count"] == count_before  # OTP belongs to this payment
submit_otp(payment["otp"])

trx = wait_frame("transaction", phone="01712345678", timeout=120)
assert trx["payment_count"] == count_before + 1  # the payment is now counted
counters["01712345678"] = trx["payment_count"]
record_receipt(trx["trx"], trx["amount"])
```

Rules that follow from the above:

- The counter is **per number, not per user**: pay files 1 and 2 with the same
  number and you get `1` then `2`; add a second number and it starts at `0`.
- The counter advances on the **transaction SMS**, not on the OTP. An OTP that is
  never turned into a payment leaves the counter where it was.
- Read `payment_count` from a transaction to know which file it settled; read it
  from a payment OTP to know how many payments had already completed when that
  OTP was sent.
- Releasing a number (`DELETE /api/payment/numbers/<phone>`) resets its counter
  to `0`, because the next owner starts a fresh history.

---

## 7. Client checklist

1. **Subscribe after connecting.** No subscribe frame, no frames. Re-subscribe
   whenever the phone list changes.
2. **One socket per channel.** A signin socket will never deliver a payment OTP.
3. **Auth before connect** for WebSockets; refresh the token on close code `4001`.
4. **Treat `{}` from HTTP as "nothing yet"** and keep polling within the retention
   window (30 min default).
5. **Read `otp` for sign-in/payment, `trx` for transactions.** A transaction has
   no `otp` field, by design.
6. **Dispatch on `kind`**, not on which fields are present, when you receive from
   a shared code path.
7. **Reconnect with backoff** on `1011`/`1000`; don't hammer on `4001`/`4003`.
8. **Respect the capacity ceiling**: each open socket holds one server thread, so
   concurrent sockets must stay below `WEB_CONCURRENCY × WEB_THREADS`.
9. **Keep nginx `proxy_read_timeout` long** (3600s) on `/ws/`; a 60s default drops
   idle sockets between OTPs.
10. **Claim before you pay.** `POST /api/payment/numbers` is what a payment OTP
    or transaction for that number is routed to; an unclaimed number is stored
    and delivered to nobody.
11. **Send the device fingerprint with the claim** (`mac_address`,
    `motherboard_serial`, …). Numbers belong to the machine, so a later token
    rotation can reclaim them with their counters instead of losing them.
12. **Track `payment_count` per number** to number your files: `1` for the first
    completed payment on that number, `2` for the next, and so on.

---

## 8. Not this system

- **Socket.IO / Backup OTP room** (`joinOTPRoom`, `new_otp`) — a different
  third-party server and protocol; unrelated to the channels above.
- **Dashboard Socket.IO** — admin UI status only.
- **`/ws/otp`** — legacy sign-in socket, kept byte-compatible for old clients.

---

The protocol above is the reference. For a ready-to-paste brief that turns it
into a client, see [`implementation.md`](implementation.md).
