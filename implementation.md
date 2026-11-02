# Implementation Brief — Sign-in OTP Client

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
You are integrating a client with the SMS sign-in OTP server at <BASE_URL>
(example host: https://server-v2.orbitalcore.site). Read the whole brief before
writing code. Do not invent fields, endpoints or events that are not listed.

GOAL
The client runs on a device that receives IVAC sign-in SMS, and must show the
latest sign-in code for a phone number it is watching, in real time.

STEP 1 — IDENTIFY THE DEVICE (once, then cache the token)
  POST /api/register  {name, mac_address, motherboard_serial, machine_guid,
                       bios_serial}            (at least 2 signature fields)
    -> 201 {user_id}   the device is now PENDING: no sockets, no data.
    -> 409            this MAC is already registered.
  POST /api/authenticate {same signature fields}
    -> 200 {ws_token}  store it; it authenticates the WebSocket.
    -> 403             pending approval or expired licence.
    -> 404             fingerprint not registered.
  Never poll /api/authenticate in a loop. Retry on user action or after 401/403.

STEP 2 — OPEN THE SOCKET
  wss://<host>/ws/otp/signin?token=<ws_token>
  Then subscribe: {"action":"subscribe","phones":["01712345678"]}
  Ack: {"type":"subscribed","phones":["01712345678"]} (phones normalised).
  Nothing is pushed before the subscribe frame arrives, so never wait for an OTP
  on an unsubscribed socket.

STEP 3 — HANDLE FRAMES
  {kind:"signin", phone, receiver, sender, sender_number, last_updated, otp}
  - `otp` is the code; it is 4–8 digits or alphanumeric characters.
  - `phone` is the receiving number (canonical 01XXXXXXXXX); `receiver` repeats
    it; `sender` is the provider label ("IVAC"); `sender_number` is the raw
    forwarder address and may be "".
  - An error frame {"type":"error","error":"..."} does not close the socket and
    keeps the previous subscription.
  - A newer OTP for the same number simply arrives as another frame; it replaces
    the code and the previous subscription stays in place.

STEP 4 — HTTP FALLBACK (when a frame was missed or the app was closed)
  GET /api/signin/<phone>        open, no token
    -> 200 {kind, phone, receiver, sender, sender_number, last_updated, otp}
    -> 200 {}   nothing has arrived in the retention window (30 min default)
  GET /api/messages/<phone>      open, legacy two-key shape {message, last_updated}
  {} is "nothing yet", not an error: keep polling.

INVARIANTS YOU MUST KEEP
  1. The server only delivers sign-in OTPs. There is no payment or transaction
     channel, so do not call /api/payment/* or /api/transaction/*.
  2. Never assume the code's shape: read `otp` and show it as text.
  3. On WebSocket close 4001, re-authenticate and reconnect; on 4003, stop and
     tell the user their device needs approval. Back off on 1011/1000 (1s→15s).
  4. After a token rotation the old token stops working everywhere and live
     sockets are closed. Re-authenticate and reconnect.
  5. Send the token as ?token=<ws_token> or the X-WS-Token header, never both
     mismatched.

ACCEPTANCE CHECKLIST FOR YOUR CODE
  [ ] register -> pending -> authenticate -> connect -> subscribe
  [ ] a sign-in OTP frame renders as a code for the right phone number
  [ ] a newer OTP for the same number replaces the shown code
  [ ] {} from HTTP renders "waiting", not an error
  [ ] reconnect + re-authenticate after 4001, and after a token rotation
```
