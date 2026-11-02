# Integration Guide — Sign-in OTP

The SMS server ingests inbound SMS, decides whether a message carries a sign-in
OTP, and delivers that code to subscribed clients over a WebSocket and over HTTP.
Anything that is not a sign-in OTP is stored as `unknown` and delivered nowhere.

Every example below is a real request/response captured from a running server.

This file is the **protocol reference**. For a ready-to-paste brief that turns it
into a client, see [`implementation.md`](implementation.md).

---

## 1. At a glance

| What | WebSocket | HTTP |
|---|---|---|
| Sign-in OTP (IVAC) | `wss://<host>/ws/otp/signin` | `GET /api/signin/<phone>` |
| Legacy sign-in socket (existing clients only) | `wss://<host>/ws/otp` | `GET /api/messages/<phone>` |
| Ingest an SMS (SMS forwarder only) | — | `POST /api/messages` |
| Worker health | — | `GET /api/health` |

Payment OTPs and transaction receipts are no longer served: there is no
`/ws/otp/payment`, `/ws/otp/transaction`, `/api/payment/*` or `/api/transaction/*`
endpoint. A wallet receipt has no OTP keyword and is classified `unknown`, so it
is stored and pushed to no socket.

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
forwarder reported (may be a gateway number or a text ID like `IVACBD`); `sender`
is the provider label the server resolved (`IVAC`). Either can be `""` when the
server could not tell. An unmapped sender is labelled `IVAC`, because IVAC is the
only sign-in provider.

**`receiver`** is the phone the SMS belongs to — the same value as `phone`, kept
as a separate key for clients that read that name.

**One row per phone.** A newer OTP for a number replaces the stored one, so a
read always returns the latest code. Unrecognised messages are kept for
inspection but never overwrite a stored OTP.

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

**HTTP authentication.** The read endpoints (`/api/signin/<phone>`,
`/api/messages/<phone>`) are open, as they always were. `POST /api/messages` is
the forwarder's endpoint. Keep the HTTP API on the private network or behind the
reverse proxy.

---

## 4. WebSocket protocol

### 4.1 Connect → subscribe → receive

```
client                                              server
  │  GET wss://host/ws/otp/signin?token=…             │
  │───────────────────────────────────────────────────►│  auth check
  │◄──────────── 101 Switching Protocols ──────────────│
  │                                                     │
  │  {"action":"subscribe","phones":["01712345678"]}    │
  │───────────────────────────────────────────────────►│
  │  {"type":"subscribed","phones":["01712345678"]}     │
  │◄───────────────────────────────────────────────────│
  │                                                     │
  │◄──── {"kind":"signin", … "otp":"456789"} ──────────│  on SMS arrival
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

### 4.3 The delivered frame

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

| Field | Notes |
|---|---|
| `kind` | always `signin` |
| `phone` | canonical `01XXXXXXXXX` |
| `receiver` | duplicate of `phone` |
| `sender` | provider label, may be `""` |
| `sender_number` | raw forwarder address, may be `""` |
| `otp` | the code, 4–8 characters (digits or alphanumeric) |
| `last_updated` | arrival time, BDT |

### 4.4 Error frames

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

### 4.5 Close codes

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

### 4.6 Keepalive

The server sends a WebSocket ping every `WS_PING_INTERVAL` (default **25s**).
Browser and Node clients answer pongs automatically; a client that does not will
be dropped because it looks dead. No application-level heartbeat is needed, and
sending an empty text frame is not required.

### 4.7 Legacy `/ws/otp`

Still supported for existing clients: the same sign-in channel, and frames
contain exactly the three original keys — no `kind`, `sender` etc.

```json
{ "otp": "123456", "phone": "01712345678", "last_updated": "2026-10-29T05:17:24.769+06:00" }
```

Subscribe works identically. New clients should use `/ws/otp/signin` instead.

### 4.8 Minimal client

```python
import json
from simple_websocket import Client

ws = Client("wss://server-v2.orbitalcore.site/ws/otp/signin?token=" + TOKEN)
ws.send(json.dumps({"action": "subscribe", "phones": ["01712345678"]}))
print(ws.receive())                      # {"type": "subscribed", ...}
while True:
    frame = json.loads(ws.receive())     # blocks until an OTP arrives
    if frame.get("type") in ("subscribed", "error"):
        continue
    print(frame["kind"], frame["otp"])
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

Called by the SMS forwarder, not by clients.

```http
POST /api/messages
Content-Type: application/json

{
  "receiver": "01712345678",
  "message": "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX",
  "sender": "IVACBD"
}
```

- `receiver` (the SIM the SMS arrived on) and `message` are required; `message` is
  truncated to 199 characters. `phone` is accepted as the older name for
  `receiver`, and `to` as an alias, so both forwarder shapes work.
- `sender` is the SMS sender address. It is optional and only labels the message,
  so a provider changing its sender number cannot break routing.

Response `201`:

```json
{ "success": true, "message": "Message saved successfully", "routed": "signin" }
```

`routed` is `signin` or `unknown`. An `unknown` message is stored for inspection
and delivered nowhere — check this field when a template stops routing. A
receipt ("Payment of Tk 3,000.00 … Balance Tk 11,620.91") is `unknown` even
though it contains digit runs, because it carries no OTP keyword.

| Status | Body |
|---|---|
| `400` | `{"error": "Invalid payload"}` (no JSON body) |
| `400` | `{"error": "Missing phone or message"}` |

A retried POST is safe: the latest row is upserted and delivered again only if it
is a sign-in OTP.

### 5.2 Read the latest code

```
GET /api/signin/<phone>
```

Returns the same object the WebSocket would have pushed, without any `type`
wrapper. No token is required.

When nothing has arrived for that number within the retention window the response
is `200` with an empty object:

```json
{}
```

Treat `{}` as "no code yet", not as an error — poll again.

### 5.3 Legacy read — `GET /api/messages/<phone>`

Original two-key shape:

```json
{ "message": "123456", "last_updated": "2026-10-29T05:17:21.618+06:00" }
```

Kept for existing bots. `GET /api/signin/<phone>` is the same thing with full
field names.

### 5.4 Health — `GET /api/health`

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
  "database": "sqlite"
}
```

`db_poller_alive` must be `true` on every worker — it is what carries an OTP from
the worker that received the SMS to the worker holding the client's socket.
`otp_fanout: "local-only"` means delivery is broken on that worker.

---

## 6. End-to-end example

```bash
curl -s -X POST https://server-v2.orbitalcore.site/api/messages \
  -H 'Content-Type: application/json' \
  -d '{"receiver":"01712345678","message":"Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX","sender":"8801999999999"}'
# {"success":true,"message":"Message saved successfully","routed":"signin"}

curl -s https://server-v2.orbitalcore.site/api/signin/01712345678
# {"kind":"signin","otp":"123456", …}
```

On `/ws/otp/signin` the same arrival pushes `"otp": "123456"`. The sender number
`8801999999999` had never been seen before — the body decided the classification,
so a new IVAC sender ID cannot break routing.

A newer code for the same number replaces the stored one and is pushed to every
subscriber:

```bash
curl -s -X POST https://server-v2.orbitalcore.site/api/messages \
  -H 'Content-Type: application/json' \
  -d '{"receiver":"01712345678","message":"Your IVAC verification code is 883322"}'
# {"success":true,...,"routed":"signin"}
# GET /api/signin/01712345678 now returns "otp": "883322"
```

---

## 7. Client checklist

1. **Subscribe after connecting.** No subscribe frame, no frames. Re-subscribe
   whenever the phone list changes.
2. **Auth before connect** for WebSockets; refresh the token on close code `4001`.
3. **Treat `{}` from HTTP as "nothing yet"** and keep polling within the retention
   window (30 min default).
4. **Read `otp`** — that is the only code field.
5. **Reconnect with backoff** on `1011`/`1000`; don't hammer on `4001`/`4003`.
6. **Respect the capacity ceiling**: each open socket holds one server thread, so
   concurrent sockets must stay below `WEB_CONCURRENCY × WEB_THREADS`.
7. **Keep nginx `proxy_read_timeout` long** (3600s) on `/ws/`; a 60s default drops
   idle sockets between OTPs.

---

## 8. Not this system

- **Socket.IO / Backup OTP room** (`joinOTPRoom`, `new_otp`) — a different
  third-party server and protocol; unrelated to the endpoints above.
- **Dashboard Socket.IO** — admin UI status only.
- **`/ws/otp`** — legacy sign-in socket, kept byte-compatible for old clients.

---

The protocol above is the reference. For a ready-to-paste brief that turns it
into a client, see [`implementation.md`](implementation.md).
