# Bot WebSocket — OrbitalCore

## Fix the 301 error

If you see:

```text
Connecting ws://server.orbitalcore.site/ws/otp
Error: Unexpected server response: 301
Disconnected (1006)
```

Use **`wss://`**, not `ws://`:

```text
❌  ws://server.orbitalcore.site/ws/otp
✅  wss://server.orbitalcore.site/ws/otp?token=YOUR_TOKEN
```

---

## Get token

1. Register device (bot API or admin)
2. Admin activates you → https://server.orbitalcore.site/admin
3. Copy **Auth token**, or call authenticate:

```bash
curl -s -X POST https://server.orbitalcore.site/api/authenticate \
  -H 'Content-Type: application/json' \
  -d '{"mac_address":"...","machine_guid":"...","motherboard_serial":"...","bios_serial":""}'
```

Response: `{ "status": "active", "ws_token": "..." }`

---

## Connect

```python
import websocket

TOKEN = "YOUR_TOKEN"
ws = websocket.create_connection(
    f"wss://server.orbitalcore.site/ws/otp?token={TOKEN}",
    header=[f"X-WS-Token: {TOKEN}"],
)
while True:
    print(ws.recv())
```

---

## Recover OTPs (after reconnect)

```bash
GET https://server.orbitalcore.site/api/messages/01712345678
```

No `message_pub` needed (IVAC-only).

Response is an array; each OTP has its own fields (`count` is 1-based order):

```json
[
  {
    "checkedAt": "2026-09-22T02:37:00.124+06:00",
    "count": 1,
    "message": "569125",
    "used": false
  },
  {
    "checkedAt": "2026-09-22T02:37:00.124+06:00",
    "count": 2,
    "message": "569127",
    "used": true
  }
]
```

## Checklist

- [ ] `wss://` (not `ws://`)
- [ ] Valid `ws_token`
- [ ] User is Active in admin
