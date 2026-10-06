# OrbitalCore WebSocket OTP Integration

This document explains how OrbitalCore integrates the **OrbitalCore OTP WebSocket** (`/ws/otp`) so bots receive SMS OTPs in real time.

Primary source file: `Core/otpListener.js`  
Related: `dashboard/deviceAuth.js`, `dashboard/server.js`, Settings UI in `dashboard/public/index.html`

---

## 1. Overview

OrbitalCore OTP has two selectable methods (Settings → OrbitalCore OTP):

| Method | Transport | Behavior |
|--------|-----------|----------|
| **HTTP** | `GET /api/messages/<phone>` | Poll the latest OTP for a number |
| **WS** | Native WebSocket `wss://…/ws/otp` | Server pushes OTPs for subscribed phones |

There is also a separate **Backup OTP Socket** (Socket.IO to third-party OTP servers). That is **not** the OrbitalCore WS system described here.

When **WS** is selected:

1. Device auth obtains a `ws_token` (`POST /api/authenticate`).
2. A shared hub (`OrbitalWsHub`) opens one WebSocket to `/ws/otp`.
3. The hub sends `{ "action": "subscribe", "phones": [...] }`.
4. The server pushes OTP frames; the hub delivers them to the matching `OtpClient`.
5. After connect, a short HTTP recovery poll fills any OTP missed during disconnect.

---

## 2. Architecture

```
┌─────────────────┐     waitForOtp()      ┌────────────┐
│   botWorker     │ ───────────────────► │ OtpClient  │
└─────────────────┘                       └─────┬──────┘
                                                │ register / unregister
                                                ▼
                                         ┌──────────────┐
                                         │ OrbitalWsHub │  (singleton)
                                         └──────┬───────┘
                                                │ wss://host/ws/otp
                                                │ Header: X-WS-Token
                                                │ Query:  ?token=
                                                ▼
                                         ┌──────────────┐
                                         │ OrbitalCore  │
                                         │  OTP Server  │
                                         └──────────────┘

Token path:
  deviceAuth.authenticateDevice() → ws_token
  fallback: config otp_ws_token / env WS_AUTH_TOKEN

UI status:
  OrbitalWsHub → setOrbitalWsStatusListener → dashboard Socket.IO
  event: orbital_ws_status { connected, phones, reason }
```

One WebSocket serves all watching phones. Multiple `OtpClient` instances for the same phone share the same subscription set.

---

## 3. Configuration keys

| Key | Purpose |
|-----|---------|
| `otp_sms_enabled` | Master OrbitalCore OTP on/off (`1` / `0`) |
| `otp_orbital_method` | `http` or `ws` |
| `sms_api_url` | HTTP SMS API base (also used to derive WS host) |
| `otp_ws_url` | Optional explicit WS URL override |
| `otp_ws_token` | Manual WS token fallback |

Defaults:

- SMS API: `https://server.orbitalcore.site/api/messages/`
- WS URL: `wss://server.orbitalcore.site/ws/otp`

WS URL derivation (`deriveOrbitalWsUrl`):

1. If `otp_ws_url` is set → use it (`wss://` added if needed).
2. Else take host from `sms_api_url` → `wss://<host>/ws/otp`.

---

## 4. Authentication (`ws_token`)

Per OrbitalCore docs: authenticate the device, then use the returned `ws_token` on `/ws/otp`.

Implementation (`dashboard/deviceAuth.js` → `resolveOtpWsToken`):

1. Prefer cached / fresh `POST /api/authenticate` → `ws_token`.
2. Fallback: Settings `otp_ws_token` or `process.env.WS_AUTH_TOKEN`.

Connection auth (both accepted by the server):

- Header: `X-WS-Token: <ws_token>`
- Query: `?token=<ws_token>` (added by `_buildWsUrl` if missing)

Close code **4001** = unauthorized → clear device auth cache, clear token, reconnect (re-authenticate).

---

## 5. Shared hub: `OrbitalWsHub`

### Register / unregister

- `OtpClient` calls `orbitalWsHub.register(this, { url, token, smsApiUrl })` when method is WS.
- Phone → `Set<OtpClient>` map tracks watchers.
- On register: ensure socket open, then `_subscribePhones()`.
- On last client for a phone removed: unsubscribe update; if no phones left → close socket.

### Connect

Uses Node `ws` package:

```js
new WebSocket(wsUrlWithToken, {
  headers: { 'X-WS-Token': token }
});
```

On `open`:

1. Emit status (Connected).
2. Send subscribe JSON (required — server pushes nothing until subscribed).
3. Run `_recoverMissedOtps()` (HTTP poll per phone).

### Subscribe payload

```json
{ "action": "subscribe", "phones": ["01XXXXXXXXX", "..."] }
```

Sent:

- Immediately after socket open
- Whenever the phone set changes on an already-open socket

Server ack (logged, not delivered as OTP):

```json
{ "type": "subscribed", "phones": ["01XXXXXXXXX"] }
```

### Reconnect

Exponential backoff (≈1s … 15s) while phones are still watching and close was not manual.

---

## 6. Incoming OTP frames

Hub parses JSON messages and ignores control / non-OTP frames.

Typical OTP push fields:

| Field | Use |
|-------|-----|
| `phone` | Route to the correct `OtpClient` set |
| `otp` | 6–8 digit code |
| `last_updated` | Freshness + dedup key: must be within **5 minutes** (`OTP_MAX_AGE_MS`) |

Delivery:

```text
OrbitalWsHub._deliver(phone, code)
  → OtpClient._onOrbitalWsOtp(code)
  → otpEmitter.emit('otp_received', code)
```

`waitForOtp()` listens for `otp_received` (and may also use cached last OTP within 5 minutes on retry).

---

## 7. Missed-OTP recovery (HTTP assist)

After WS `open`, `_recoverMissedOtps()` calls `fetchOtpSms(phone)` for each watched phone.

HTTP rules (current):

- Age window: **5 minutes** (`last_updated`)
- One object per number: `{ message, last_updated }` — the
  latest code only, no `used` flag and no list

This is recovery / HTTP-method logic — not a second WebSocket.

---

## 8. Bot usage flow

1. Worker creates / reuses `OtpClient(phone)`.
2. On Send OTP / listen: `client.connect()` → `refreshSources()` → hub register if WS.
3. `waitForOtp(timeoutMs)`:
   - Rejects if no OTP source enabled
   - Rejects if WS selected but token empty
   - Waits for `otp_received` or timeout
4. On new Send OTP cycle, worker may call `clearCachedOtp()` so a previous code is not reused.

---

## 9. Dashboard UI status

`dashboard/server.js`:

- `setOrbitalWsStatusListener` → broadcasts `orbital_ws_status` over dashboard Socket.IO
- On dashboard client connect → emit current `getOrbitalWsStatus()`

UI (Settings OTP panel):

- HTTP / WS radio (`otp_orbital_method`)
- WS label green/red from connection status
- Phone badge = number of phones currently watching via the hub
- Token display from device authenticate (not required for HTTP mode)

---

## 10. Quick enable checklist

1. Activate device (so `POST /api/authenticate` can return `ws_token`), **or** set manual WS Token.
2. Settings → OrbitalCore OTP **ON**.
3. Method → **WS**.
4. Confirm SMS API host is correct (or set `otp_ws_url`).
5. Start a bot through Send OTP / OTP wait — hub should show Connected and phone count ≥ 1.
6. When SMS arrives, OTP should appear without HTTP polling.

### Server-side requirements

These are the server's responsibility, but they are what makes step 6 actually hold:

| Requirement | If missing |
|---|---|
| Same `DATABASE_URL` on every worker | **Delivery breaks.** A worker only sees OTPs in the DB it shares. This is how one worker reaches a client's connection held by another. |
| `SECRET_KEY` identical on every worker | Admin sessions break |
| `proxy_read_timeout 3600s` in nginx | Idle WebSockets dropped by the proxy |
| Concurrent WS < `WEB_CONCURRENCY × WEB_THREADS` | New connections queue; ingest stalls |

No Redis is required. Redis is an optional accelerator only (`REDIS_URL`): setting it
takes delivery from ~188 ms to ~11 ms median, but it is not what makes it work.

Verify per worker — each response is a *different* worker, so repeat it:

```bash
for i in $(seq 10); do curl -s localhost:8002/api/health; echo; done
```

- `pid` must differ between calls (proves multiple workers are being served).
- `db_poller_alive: true` on every worker — this is what carries cross-worker OTPs.
- `otp_fanout` should be `db-poller` (or `redis` if configured). **`local-only` means
  delivery is broken on that worker.**
- `ws_clients` summed across all workers should equal your number of live bots.
- `database` must be identical on every worker.

**Symptom → cause.** If *most* users get nothing but the admin works, the usual cause
is that workers are not sharing one `DATABASE_URL`, or the poller is not running
(`db_poller_alive: false`, `otp_fanout: local-only`). Each worker's WebSocket
registry is in-process only, so a client is only reachable from the worker holding
its connection.

---

## 11. Key functions (reference)

| Symbol | Role |
|--------|------|
| `resolveOrbitalWsToken` | Device auth → `ws_token` |
| `deriveOrbitalWsUrl` | Build `/ws/otp` URL |
| `OrbitalWsHub` | Shared native WebSocket + subscribe + deliver |
| `orbitalWsHub.register` / `unregister` | Phone watchers |
| `OtpClient._syncOrbitalWs` | Bind client to hub when method is WS |
| `OtpClient.waitForOtp` | Promise that resolves on OTP |
| `fetchOtpSms` | HTTP poll (method HTTP + WS recovery) |
| `getOrbitalWsStatus` / `setOrbitalWsStatusListener` | Dashboard status bridge |

---

## 12. What this is not

- **Not** Socket.IO Backup OTP (`joinOTPRoom` / `new_otp`) — different servers and protocol.
- **Not** dashboard Socket.IO (that only shows status to the UI).
- Selecting **HTTP** does not open `/ws/otp`; it only polls `/api/messages/<phone>`.
