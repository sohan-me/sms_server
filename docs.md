# OTP WebSocket Integration

Give this guide to your bot developer or coding agent.

## Get an auth token

Call `POST /api/authenticate` with any two or more device fingerprint fields:

```json
{
  "mac_address": "DEVICE_MAC",
  "machine_guid": "DEVICE_GUID"
}
```

At least two supplied fields must match the same registered user. Use the
returned `ws_token` to connect. WebSocket connections use the token only.

## 1. Connect

```text
wss://server.orbitalcore.site/ws/otp?token=YOUR_TOKEN
```

Use `wss://`, not `ws://`.

## 2. Send phone numbers

Immediately after connecting, send:

```json
{
  "action": "subscribe",
  "phones": ["01712345678", "01612345678"]
}
```

The server replies:

```json
{
  "type": "subscribed",
  "phones": ["01612345678", "01712345678"]
}
```

The bot will now receive OTPs only for these numbers.

## 3. Receive OTPs

Example message from the server:

```json
{
  "id": 123,
  "otp": "129000",
  "phone": "01612345678",
  "used": false,
  "created_at": "2026-09-23T02:20:15.421+06:00"
}
```

Every OTP response includes `phone`. Use it to identify which number received
the OTP, even when several subscribed numbers receive OTPs at the same time.

## Update numbers without reconnecting

Send another subscription on the same open WebSocket:

```json
{
  "action": "subscribe",
  "phones": ["01612345678", "01512345678"]
}
```

This replaces the old list completely. New numbers start receiving OTPs, and
removed numbers stop receiving OTPs immediately.

## Python example

```python
import json
import websocket

token = "YOUR_TOKEN"
phones = ["01712345678", "01612345678"]

ws = websocket.create_connection(
    f"wss://server.orbitalcore.site/ws/otp?token={token}"
)

ws.send(json.dumps({
    "action": "subscribe",
    "phones": phones,
}))

while True:
    data = json.loads(ws.recv())
    print(data)
```

## Important

- Send the subscription again after every reconnect.
- Sending a new subscription replaces the previous phone list.
- Multiple bots can connect at the same time.
- A bot receives nothing until it sends its phone list.
- If disconnected, recover messages with:

```text
GET https://server.orbitalcore.site/api/messages/01712345678
```

## Server deployment

When running multiple server workers or instances, set the same `REDIS_URL` for
every worker:

```text
REDIS_URL=redis://127.0.0.1:6379/0
```

Redis forwards each saved OTP to every worker. Each worker then sends it only
to its local WebSocket connections subscribed to that phone number. Without
`REDIS_URL`, in-memory delivery is suitable only for a single server process.
