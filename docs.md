# OTP WebSocket Integration

## 1. Get the token

Authenticate with at least two matching device fingerprints:

```http
POST /api/authenticate
Content-Type: application/json
```

```json
{
  "mac_address": "DEVICE_MAC",
  "machine_guid": "DEVICE_GUID"
}
```

Use the returned `ws_token`.

## 2. Connect

```text
wss://server.orbitalcore.site/ws/otp?token=YOUR_TOKEN
```

## 3. Subscribe to numbers

Send this after connecting:

```json
{
  "action": "subscribe",
  "phones": ["01712345678", "01612345678"]
}
```

The server confirms:

```json
{
  "type": "subscribed",
  "phones": ["01612345678", "01712345678"]
}
```

Send another `subscribe` message to replace the phone list without reconnecting.

## 4. Receive OTPs

Each OTP arrives as a separate WebSocket message:

```json
{
  "id": 123,
  "otp": "129000",
  "phone": "01612345678",
  "used": false,
  "created_at": "2026-09-23T15:48:23.095+06:00"
}
```

Use `phone` to identify which number received the OTP.

## Important

- Subscribe again after every reconnect.
- A connection receives OTPs only for its current phone list.
- Multiple users and connections can run together.
- Recover missed OTPs with `GET /api/messages/<phone>`.
- Multi-worker servers must use the same `REDIS_URL`.
