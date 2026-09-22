# OTP WebSocket Integration

Give this guide to your bot developer or coding agent.

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

Use `phone` to identify which number received the OTP.

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
