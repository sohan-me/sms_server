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

## The two services

| | Path | Port | Stack |
|---|---|---|---|
| Flask (legacy) | `app.py` | 8002 | Flask + gunicorn (`gthread`) |
| FastAPI (current) | `fastapi_app/main.py` | 3002 | FastAPI + Uvicorn + Tortoise |

They serve an identical client-facing contract, so you can run both during a cutover.
Both import `otp_core.py` and `otp_broker.py` from the server root — that shared code
is what guarantees identical phone normalisation, OTP extraction and fan-out.

Start the FastAPI service:

```bash
venv/bin/python -m pip install -r requirements.txt
pm2 start fastapi_app/ecosystem.config.js
pm2 save
```

Set `SECRET_KEY` and `DATABASE_URL` in that config first, and keep `SECRET_KEY`
identical to the Flask app's or admin sessions break across the cutover.

Verify:

```bash
curl -s localhost:3002/api/health; echo
```

`redis_subscriber_alive` must be `true`. With `--workers 1` delivery is local-only by
nature; add `REDIS_URL` and raise `--workers` before running more than one process.

### Contract conformance

`fastapi_app/test_contract.py` asserts the exact status codes, error strings, response
key sets, WebSocket frames and close codes the clients depend on — 37 cases. It is the
gate that makes "no client changes" verifiable rather than aspirational.

```bash
venv/bin/python -m unittest fastapi_app.test_contract
```

### Admin: OTP delivery log

`GET /admin/otps` lists which user received which OTP, over which channel, within the
retention window. Backed by a new `otp_delivery` table written when a frame is handed
to the socket — so it records *sent*, not *read*.

### Known dependency pins

- `aiosqlite==0.21.0` — Tortoise 0.25 calls `Connection.start()`, removed in 0.22+.
- `tortoise-orm==0.25.x` — 1.x replaced the global API with a context object.

## How an OTP reaches a client

1. `POST /api/messages` stores the message and normalizes the OTP to digits.
2. `OTPBroker.publish()` delivers it to WebSocket clients **in this process**, then
   publishes to the Redis channel `orbitalcore:otp`.
3. Every worker's subscriber receives it and sends it to its own clients whose
   subscribed phone list contains the OTP's `phone`.

Step 2's local delivery happens whether or not Redis is configured. So with Redis
missing, only the clients sharing the process that ingested the SMS receive anything —
which looks like "it works for one user but not the others".

`OTPMessage` is the retention store (purged after `OTP_RETENTION_MINUTES` on each
`GET /api/messages/<phone>`). Redis Pub/Sub is fire-and-forget transport only — it
cannot store or expire anything, so OTPs are deliberately not kept there.

## Deployment (gunicorn + pm2)

```bash
pm2 start ecosystem.config.js
pm2 logs orbitalcore-sms
```

`ecosystem.config.js` launches gunicorn through `venv/bin/python -m gunicorn` rather
than the `venv/bin/gunicorn` script on purpose: a venv that was copied between
machines keeps hardcoded absolute-path shebangs in its `bin/` scripts, so those can
point somewhere that no longer exists. The same applies to `venv/bin/pip` — use
`venv/bin/python -m pip install ...`.

Two settings are load-bearing and easy to get wrong:

- **`worker_class = "gthread"`** (see `gunicorn.conf.py`). gunicorn's default `sync`
  worker handles one request at a time, so one open WebSocket blocks that worker and
  stalls every other client sharing it.
- **`REDIS_URL` in the pm2 `env` block.** A `.env` file is not read by gunicorn or
  pm2. The app logs a warning at startup when it is missing.

Never start `app.py` directly — its `__main__` block runs the Werkzeug dev server with
`debug=True`, whose reloader spawns a second process that re-imports the app and opens
a duplicate Redis subscriber.

The app listens on **port 8002** by default (`BIND` in `ecosystem.config.js` and
`gunicorn.conf.py`). Point your reverse proxy upstream at `127.0.0.1:8002`, and make
sure it forwards WebSocket upgrade headers:

```nginx
location /ws/ {
    proxy_pass http://127.0.0.1:8002;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;   # long-lived WS; default 60s would drop them
}
```

Every worker runs the schema/admin bootstrap at import, so that setup is wrapped in a
savepoint and retried: otherwise all workers race to create the default admin row, the
losers hit a UNIQUE violation, and the master shuts the whole app down with
"Worker failed to boot".

### Diagnosing fan-out

`GET /api/health` reports the state of whichever worker answers, which is the fastest
way to see a misconfiguration:

```json
{
  "service": "sms",
  "pid": 1234,
  "redis_configured": true,
  "redis_subscriber_alive": true,
  "ws_clients": 2,
  "database": "sqlite"
}
```

Call it repeatedly — each response is a different worker. A healthy multi-worker setup
shows a **different `pid` each time**, every one with `redis_subscriber_alive: true`,
and `ws_clients` summing to the number of live clients. If a worker reports
`redis_subscriber_alive: false` or is missing from the rotation, that is the cause.

```bash
for pid in $(pgrep -f "gunicorn: worker"); do
  echo "== $pid"; tr '\0' '\n' < /proc/$pid/environ | grep -E "REDIS_URL|DATABASE_URL|SECRET_KEY"
done

redis-cli -u "$REDIS_URL" pubsub channels   # expect orbitalcore:otp, one subscriber per worker
```

### Database

`DATABASE_URL` defaults to SQLite. WAL and a busy timeout are enabled automatically,
but SQLite still serialises writers, so concurrent workers can raise
`database is locked` under load. Use PostgreSQL for real multi-worker deployment:

```bash
pip install psycopg2-binary
export DATABASE_URL=postgresql://user:pass@127.0.0.1:5432/orbitalcore
```

`ensure_schema_columns()` uses plain `ALTER TABLE ... ADD COLUMN`, so it runs on both
SQLite and PostgreSQL.
