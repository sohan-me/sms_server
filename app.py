import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template_string,
    request,
    session,
    url_for,
)
from flask_sock import Sock
from sqlalchemy import event, func, inspect, or_, text
from sqlalchemy.exc import IntegrityError, OperationalError

from otp_broker import OTPBroker
from otp_core import match_signatures
from models import (
    DEFAULT_MESSAGE_PUB,
    AdminUser,
    DevicePing,
    DeviceUser,
    OTPMessage,
    db,
    generate_ws_token,
)

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(BASE_DIR, "device_auth.db"),
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "raven-default-secret")
app.config["WS_AUTH_TOKEN"] = os.environ.get("WS_AUTH_TOKEN", "").strip() or None

db.init_app(app)
sock = Sock(app)
# Without a ping interval simple_websocket never probes an idle peer, so a client
# killed by nginx or a NAT timeout is never detected: it stays in app.ws_clients and
# its gthread slot stays blocked forever. Pinging makes it get closed and reaped.
app.config["SOCK_SERVER_OPTIONS"] = {
    "ping_interval": int(os.environ.get("WS_PING_INTERVAL", "25"))
}
app.ws_clients = {}  # connection_id -> {ws, user_id, phones, send_lock}
app.ws_clients_lock = threading.RLock()

MIN_SIGNATURE_FIELDS = 2


def _otp_retention_minutes():
    raw = os.environ.get("OTP_RETENTION_MINUTES", "30")
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def _norm_sig(value):
    if value is None:
        return ""
    # Fingerprints are retyped by hand in the admin panel and re-reported by
    # each PC, so the same identifier arrives with different casing depending
    # on who typed it. Matching was byte-exact, so a lowercase MAC or an
    # uppercase GUID failed to match the stored value and an already-registered
    # device was rejected with "Device not found". Compare case-insensitively.
    return str(value).strip().upper()


def _normalize_device_signatures(data):
    mac = _norm_sig(data.get("mac_address"))
    mb = _norm_sig(data.get("motherboard_serial"))
    guid = _norm_sig(data.get("machine_guid"))
    bios = _norm_sig(data.get("bios_serial"))
    count = sum(1 for v in (mac, mb, guid, bios) if v)
    return mac, mb, guid, bios, count


def _find_user_by_signatures(mac, mb, guid, bios):
    provided = {
        "mac_address": mac,
        "motherboard_serial": mb,
        "machine_guid": guid,
        "bios_serial": bios,
    }
    provided = {name: value for name, value in provided.items() if value}
    # norm_sig() upper-cases, so the prefilter must compare case-insensitively
    # too — a byte-exact SQL filter would never return a device stored with
    # different casing, and the device would look unregistered.
    clauses = [
        func.upper(getattr(DeviceUser, name)) == value
        for name, value in provided.items()
    ]
    if len(clauses) < MIN_SIGNATURE_FIELDS:
        return None, "insufficient"

    candidates = DeviceUser.query.filter(or_(*clauses)).all()
    return match_signatures(candidates, mac, mb, guid, bios)


def normalize_bd_phone(value):
    if value is None:
        return ""
    s = re.sub(r"[\s\-()]+", "", str(value).strip())
    if not s:
        return ""
    if s.startswith("+"):
        s = s[1:]
    digits = "".join(c for c in s if c.isdigit())
    if not digits:
        return ""
    d = digits
    if len(d) >= 12 and d.startswith("880") and len(d) > 3 and d[3] == "1":
        d = d[3:]
    if len(d) == 10 and d[0] == "1":
        d = "0" + d
    return d


NUMBER_WORD_TO_DIGIT = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}


def _extract_normalized_otp(raw):
    """Turn IVAC word OTPs / spaced digits into a digit string."""
    if raw is None:
        return ""
    text = str(raw).strip()
    if not text:
        return ""

    # 1) Number-word run (≥4 words): Five-Six-Nine-One-Two-Five → 569125
    tokens = re.findall(r"[a-z]+", text.lower())
    best = []
    current = []
    for tok in tokens:
        if tok in NUMBER_WORD_TO_DIGIT:
            current.append(NUMBER_WORD_TO_DIGIT[tok])
        else:
            if len(current) > len(best):
                best = current
            current = []
    if len(current) > len(best):
        best = current
    if len(best) >= 4:
        return "".join(best)

    # 2) Spaced/hyphenated digits: 2-5-7-0-0-7
    spaced = re.findall(r"(?:\d[\s\-]+){3,}\d", text)
    if spaced:
        return re.sub(r"\D", "", spaced[-1])

    # 3) Contiguous 4–8 digit block (last match), else longest digit run
    blocks = re.findall(r"\d{4,8}", text)
    if blocks:
        return blocks[-1]
    runs = re.findall(r"\d+", text)
    if runs:
        return max(runs, key=len)

    return ""


def _extract_otp_for_pub(raw, message_pub):
    """Extract OTP digits using publisher-specific rules (IVAC vs VFS)."""
    pub = str(message_pub or "").strip().upper()
    if pub == "IVAC":
        return _extract_normalized_otp(raw)
    m = re.search(r"\b(\d{6})\b", str(raw or ""))
    return m.group(1) if m else ""


def _format_bdt(dt=None):
    bdt_tz = timezone(timedelta(hours=6))
    if dt is None:
        now = datetime.now(bdt_tz)
    elif dt.tzinfo is None:
        now = dt.replace(tzinfo=timezone.utc).astimezone(bdt_tz)
    else:
        now = dt.astimezone(bdt_tz)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+06:00"


def ensure_schema_columns():
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())

    if "otp_message" in tables:
        cols = {c["name"] for c in insp.get_columns("otp_message")}
        with db.engine.begin() as conn:
            if "message_pub" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE otp_message ADD COLUMN message_pub VARCHAR(16) DEFAULT 'IVAC'"
                    )
                )
                conn.execute(
                    text(
                        "UPDATE otp_message SET message_pub = 'IVAC' WHERE message_pub IS NULL"
                    )
                )

            # The store keeps one row per phone now: a new OTP overwrites
            # the existing row. Collapse any history first so the unique
            # index can be created.
            conn.execute(
                text(
                    "DELETE FROM otp_message WHERE id NOT IN "
                    "(SELECT MAX(id) FROM otp_message GROUP BY phone)"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_otp_message_phone "
                    "ON otp_message (phone)"
                )
            )
            if "updated_at" not in cols:
                conn.execute(
                    text("ALTER TABLE otp_message ADD COLUMN updated_at DATETIME")
                )
                conn.execute(
                    text(
                        "UPDATE otp_message SET updated_at = created_at "
                        "WHERE updated_at IS NULL"
                    )
                )
            if "revision" not in cols:
                conn.execute(
                    text("ALTER TABLE otp_message ADD COLUMN revision BIGINT")
                )
                conn.execute(
                    text("UPDATE otp_message SET revision = id WHERE revision IS NULL")
                )

    # The counter must never lag behind the rows it counts, and every
    # worker boots concurrently, so both statements are idempotent.
    with db.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO otp_revision (id, value) "
                "SELECT 1, COALESCE((SELECT MAX(revision) FROM otp_message), 0) "
                "WHERE NOT EXISTS (SELECT 1 FROM otp_revision)"
            )
        )
        conn.execute(
            text(
                "UPDATE otp_revision SET value = "
                "(SELECT COALESCE(MAX(revision), 0) FROM otp_message) "
                "WHERE value < (SELECT COALESCE(MAX(revision), 0) FROM otp_message)"
            )
        )

    if "device_user" in tables:
        cols = {c["name"] for c in insp.get_columns("device_user")}
        with db.engine.begin() as conn:
            if "expires_at" not in cols:
                conn.execute(
                    text("ALTER TABLE device_user ADD COLUMN expires_at DATETIME")
                )
            if "ws_token" not in cols:
                conn.execute(
                    text("ALTER TABLE device_user ADD COLUMN ws_token VARCHAR(64)")
                )


def backfill_missing_ws_tokens():
    users = DeviceUser.query.filter(
        (DeviceUser.ws_token.is_(None)) | (DeviceUser.ws_token == "")
    ).all()
    if not users:
        return
    existing = {
        u.ws_token
        for u in DeviceUser.query.filter(DeviceUser.ws_token.isnot(None)).all()
        if u.ws_token
    }
    for user in users:
        token = generate_ws_token()
        while token in existing:
            token = generate_ws_token()
        user.ws_token = token
        existing.add(token)
    db.session.commit()


def _unregister_ws(connection_id, expected_ws=None, close_reason=1000, close_message="Connection closed"):
    """Remove a client from the registry and close its socket.

    Every removal must go through here. Popping the entry while leaving the socket
    open leaves the handler thread blocked in ws.receive() and the peer believing it
    is still connected, so the OTP simply never arrives again for that client.
    """
    with app.ws_clients_lock:
        client_data = app.ws_clients.get(connection_id)
        if client_data is None:
            return None
        if expected_ws is not None and client_data["ws"] is not expected_ws:
            return None
        app.ws_clients.pop(connection_id, None)

    ws = client_data["ws"]
    try:
        ws.close(close_reason, close_message)
    except Exception:
        pass
    return client_data


def _broadcast_otp(payload: dict):
    # Deduplicate the same arrival reaching this worker twice: the local
    # publish plus its Redis echo, or the poller re-reading a row this
    # worker already delivered. A number's row is reused, so the arrival
    # timestamp is the per-delivery identity.
    phone = normalize_bd_phone(payload.get("phone"))
    arrival_key = f"{phone}|{payload.get('last_updated')}"
    now = time.time()
    with app.ws_clients_lock:
        seen = getattr(app, "_otp_broadcast_seen", None)
        if seen is None:
            seen = {}
            app._otp_broadcast_seen = seen
        expired = [k for k, ts in seen.items() if now - ts > 60]
        for k in expired:
            seen.pop(k, None)
        if arrival_key in seen:
            return
        seen[arrival_key] = now

    ws_msg = json.dumps(payload)
    with app.ws_clients_lock:
        clients = list(app.ws_clients.items())

    delivered = 0
    dead = []
    for connection_id, client_data in clients:
        if phone not in client_data["phones"]:
            continue
        try:
            # The Redis subscriber thread and this client's handler thread both
            # write to the same socket, and simple_websocket's send() mutates
            # wsproto state then writes raw bytes with no lock of its own.
            # Without this, two writers interleave and the peer's frame parser
            # desyncs, silently discarding every later message.
            with client_data["send_lock"]:
                client_data["ws"].send(ws_msg)
            delivered += 1
        except Exception:
            dead.append((connection_id, client_data["ws"]))
            logging.getLogger(__name__).warning(
                "WS send failed for phone %s on connection %s (user %s)",
                phone,
                connection_id,
                client_data["user_id"],
                exc_info=True,
            )

    if delivered == 0:
        logging.getLogger(__name__).info(
            "OTP %s for phone %s matched no WebSocket subscription on this "
            "worker; %d client(s) connected here",
            payload.get("otp"),
            phone,
            len(clients),
        )

    for connection_id, client in dead:
        _unregister_ws(connection_id, expected_ws=client, close_reason=1011)


def _next_otp_revision():
    """Bump and return the monotonic OTP arrival counter.

    The delivery poller follows this counter instead of `otp_message.id`
    because a new OTP for a known number updates the existing row — the
    row id stays the same, so it cannot mark an arrival as seen.
    """
    # Seed the single counter row if it is missing (fresh database).
    db.session.execute(
        text(
            "INSERT INTO otp_revision (id, value) VALUES (1, 0) "
            "ON CONFLICT (id) DO NOTHING"
        )
    )
    db.session.execute(text("UPDATE otp_revision SET value = value + 1 WHERE id = 1"))
    return db.session.execute(
        text("SELECT value FROM otp_revision WHERE id = 1")
    ).scalar()


def _poll_otps_from_db(stop_event, interval=0.5):
    """Deliver OTPs that this worker never saw directly, by polling the shared table.

    This is the cross-worker delivery path. Every number has one row in
    `otp_message`, shared by every worker, so a worker can always deliver
    OTPs another worker ingested — no broker required.

    The cursor is the `revision` counter, not the row id: a new OTP for
    a known number updates the existing row, so the id stays the same and
    cannot mark an arrival as seen. Deduplicated against the same
    `_otp_broadcast_seen` map the broker path uses, so a client
    subscribed on several workers still receives each OTP exactly once.

    A worker with no WebSocket clients has nobody to deliver to, so it skips the
    query entirely. With Redis removed this poller is the only delivery path, so
    keeping idle workers quiet keeps database load proportional to how many bots
    are actually connected.
    """
    last_seen_revision = 0
    with app.app_context():
        try:
            last_seen_revision = db.session.query(
                func.max(OTPMessage.revision)
            ).scalar() or 0
        except Exception:
            # The schema is created further down at import time, so on a cold boot
            # the table may not exist yet. Start from 0 and re-check on each pass.
            db.session.rollback()
            last_seen_revision = 0

        while not stop_event.is_set():
            with app.ws_clients_lock:
                has_clients = bool(app.ws_clients)

            if not has_clients:
                stop_event.wait(interval)
                continue

            try:
                rows = (
                    db.session.query(OTPMessage)
                    .filter(OTPMessage.revision > last_seen_revision)
                    .order_by(OTPMessage.revision.asc())
                    .limit(500)
                    .all()
                )
                for row in rows:
                    last_seen_revision = row.revision
                    digits = _extract_otp_for_pub(row.otp_message, row.message_pub)
                    if not digits:
                        continue
                    _broadcast_otp(
                        {
                            "otp": digits,
                            "phone": row.phone,
                            "last_updated": _format_bdt(row.updated_at),
                        }
                    )
                if rows:
                    db.session.remove()
            except Exception:
                db.session.remove()
                logging.getLogger(__name__).exception(
                    "OTP database poller hit an error; retrying"
                )

            stop_event.wait(interval)


def _start_db_poller():
    if os.environ.get("DISABLE_DB_OTP_POLLER"):
        return None
    interval = max(0.05, float(os.environ.get("OTP_POLL_INTERVAL", "0.5")))
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_poll_otps_from_db,
        args=(stop_event, interval),
        name="otp-db-poller",
        daemon=True,
    )
    thread.start()
    return thread


otp_broker = OTPBroker(
    redis_url=os.environ.get("REDIS_URL"),
    on_message=_broadcast_otp,
)
# Redis is an optional accelerator. Without it the database poller below carries
# delivery on its own, so nothing here is a failure state.
if not otp_broker.distributed:
    logging.getLogger(__name__).info(
        "OTP delivery on this worker runs through the database poller "
        "(no usable REDIS_URL). Cross-worker delivery does not require Redis; "
        "setting REDIS_URL only makes it faster."
    )
elif not otp_broker.start():
    logging.getLogger(__name__).error(
        "OTP Redis subscriber failed to start on this worker — falling back to "
        "the database poller. Check REDIS_URL and that redis-server is running "
        "and accepting connections."
    )


def _drop_user_ws(user_id):
    with app.ws_clients_lock:
        connection_ids = [
            connection_id
            for connection_id, client_data in app.ws_clients.items()
            if client_data["user_id"] == user_id
        ]

    for connection_id in connection_ids:
        _unregister_ws(connection_id, close_reason=4003, close_message="Access revoked")


def _parse_subscription(raw_message):
    try:
        data = json.loads(raw_message)
    except (TypeError, ValueError):
        return None, "Invalid JSON"

    if not isinstance(data, dict) or data.get("action") != "subscribe":
        return None, "Expected a subscribe action"

    raw_phones = data.get("phones")
    if not isinstance(raw_phones, list) or not raw_phones:
        return None, "phones must be a non-empty array"

    phones = set()
    for raw_phone in raw_phones:
        if not isinstance(raw_phone, str):
            return None, "Each phone must be a string"
        phone = normalize_bd_phone(raw_phone)
        if not phone:
            return None, "Each phone must contain digits"
        phones.add(phone)

    return phones, None


def _serve_ws_connection(ws, user_id):
    otp_broker.start()
    connection_id = id(ws)
    with app.ws_clients_lock:
        app.ws_clients[connection_id] = {
            "ws": ws,
            "user_id": user_id,
            "phones": set(),
            "send_lock": threading.Lock(),
        }

    try:
        while True:
            raw_message = ws.receive()
            if raw_message is None:
                break

            phones, error = _parse_subscription(raw_message)
            if error:
                ws.send(json.dumps({"type": "error", "error": error}))
                continue

            with app.ws_clients_lock:
                client_data = app.ws_clients.get(connection_id)
                if not client_data or client_data["ws"] is not ws:
                    break
                client_data["phones"] = phones

            with client_data["send_lock"]:
                ws.send(
                    json.dumps(
                        {
                            "type": "subscribed",
                            "phones": sorted(phones),
                        }
                    )
                )
    except Exception:
        logging.getLogger(__name__).debug(
            "WS receive loop ended for connection %s (user %s)",
            connection_id,
            user_id,
            exc_info=True,
        )
    finally:
        _unregister_ws(connection_id, expected_ws=ws)


def _configure_sqlite_concurrency(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA busy_timeout=5000")


def _enable_sqlite_wal():
    """WAL cannot be set inside a transaction, so it needs its own connection."""
    raw = db.engine.raw_connection()
    try:
        raw.connection.execute("PRAGMA journal_mode=WAL")
    finally:
        raw.close()


def _run_startup_schema_setup():
    """Every worker runs this concurrently, so a check-then-insert would race and
    the UNIQUE violation would kill the worker. Retrying makes the loser recover."""
    for attempt in range(5):
        try:
            with db.session.begin_nested():
                db.create_all()
                ensure_schema_columns()
                backfill_missing_ws_tokens()
                if not AdminUser.query.filter_by(username="incomplete").first():
                    admin = AdminUser(username="incomplete")
                    admin.set_password(os.environ.get("ADMIN_PASSWORD", "Barhatta&2026"))
                    db.session.add(admin)
            db.session.commit()
            return
        except (IntegrityError, OperationalError) as exc:
            db.session.rollback()
            if attempt == 4:
                logging.getLogger(__name__).exception(
                    "Startup schema setup failed after 5 attempts; this worker "
                    "cannot serve requests reliably"
                )
                raise
            logging.getLogger(__name__).info(
                "Startup schema setup lost a race, retrying: %s",
                exc,
            )
            time.sleep(0.5)


with app.app_context():
    if db.engine.dialect.name == "sqlite":
        event.listen(db.engine, "connect", _configure_sqlite_concurrency)
        _enable_sqlite_wal()
    _run_startup_schema_setup()

# Started only after the schema exists, so the poller never races table creation.
# The poller is the correctness backstop: it makes delivery independent of Redis,
# which is what caused OTPs to silently stop reaching clients on other workers.
app.otp_db_poller = _start_db_poller()


# ── CLI Console HTML ───────────────────────────────────────────

CLI_CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SMS Server // CLI Console</title>
  <style>
    * { box-sizing: border-box; }
    body {
      background-color: #0c0d0e;
      color: #39ff14;
      font-family: "Courier New", Courier, monospace;
      margin: 0;
      padding: 16px;
    }
    .term-box {
      max-width: 960px;
      margin: 0 auto;
      border: 1px solid #1f3b1f;
      border-radius: 6px;
      background: #000;
      box-shadow: 0 4px 20px rgba(0, 255, 0, 0.08);
      overflow: hidden;
    }
    .term-header {
      background: #111811;
      border-bottom: 1px solid #1f3b1f;
      padding: 10px 16px;
      color: #a3e635;
      font-weight: bold;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .term-actions {
      padding: 10px 16px;
      background: #0a0e0a;
      border-bottom: 1px solid #182a18;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    button {
      background: #142814;
      color: #79f244;
      border: 1px solid #2d5a2d;
      padding: 6px 12px;
      font-family: inherit;
      font-size: 13px;
      cursor: pointer;
      border-radius: 3px;
    }
    button:hover { background: #1c3c1c; border-color: #39ff14; color: #fff; }
    .term-body {
      padding: 16px;
      min-height: 400px;
      max-height: 600px;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
      color: #e2e8f0;
      font-size: 13px;
      line-height: 1.45;
    }
    .term-input-row {
      display: flex;
      border-top: 1px solid #1f3b1f;
      background: #0a0e0a;
      padding: 10px 16px;
      align-items: center;
      gap: 8px;
    }
    .term-prompt {
      color: #39ff14;
      font-weight: bold;
      user-select: none;
    }
    .term-input {
      flex: 1;
      background: transparent;
      border: none;
      color: #39ff14;
      font-family: inherit;
      font-size: 14px;
      outline: none;
    }
    .b-dim { color: #64748b; }
  </style>
</head>
<body>
  <div class="term-box">
    <div class="term-header">
      <span>SMS SERVER // CLI CONSOLE</span>
      <span class="b-dim" id="clock"></span>
    </div>
    <div class="term-actions">
      <button onclick="runCommand('pings')">📡 View Ping Messages</button>
      <button onclick="promptOtp()">🔑 Retrieve OTP</button>
      <button onclick="runCommand('messages')">📨 Recent OTP Messages</button>
      <button onclick="runCommand('health')">🩺 Server Health</button>
      <button onclick="clearScreen()">🧹 Clear</button>
    </div>
    <div class="term-body" id="screen">Ready. Type a command below or click an action button above.
Available commands:
  • pings               : View SIM relay heartbeat ping messages
  • otp &lt;phone&gt;         : Retrieve active OTP for phone number
  • messages            : List recent OTP messages
  • health              : Show server diagnostics & fanout path
  • clear               : Clear output screen
  • help                : Display this help message
</div>
    <div class="term-input-row">
      <span class="term-prompt">sms-cli&gt;</span>
      <input type="text" id="cmdInput" class="term-input" placeholder="Type command (e.g. 'pings' or 'otp 017XXXXXXXX') and press Enter" autofocus autocomplete="off" />
      <button onclick="submitInput()">Enter</button>
    </div>
  </div>

  <script>
    function updateClock() {
      document.getElementById('clock').textContent = new Date().toISOString().replace('T', ' ').substring(0, 19) + ' UTC';
    }
    setInterval(updateClock, 1000); updateClock();

    const screen = document.getElementById('screen');
    const input = document.getElementById('cmdInput');

    function log(msg, color) {
      const line = document.createElement('div');
      if (color) line.style.color = color;
      line.textContent = msg;
      screen.appendChild(line);
      screen.scrollTop = screen.scrollHeight;
    }

    function clearScreen() {
      screen.innerHTML = '';
      log("Console cleared.", "#64748b");
    }

    function promptOtp() {
      const phone = prompt("Enter BD Phone Number (e.g. 017XXXXXXXX):");
      if (phone) runCommand("otp " + phone.trim());
    }

    async function runCommand(raw) {
      const cmd = raw.trim();
      if (!cmd) return;
      log("\\nsms-cli> " + cmd, "#39ff14");

      const parts = cmd.split(/\\s+/);
      const action = parts[0].toLowerCase();
      const arg = parts.slice(1).join(" ");

      if (action === "clear") {
        clearScreen();
        return;
      }
      if (action === "help") {
        log("Commands:\\n  pings          - View device ping messages\\n  otp <phone>    - Retrieve latest OTP for phone\\n  messages       - View recent messages\\n  health         - Check server health\\n  clear          - Clear console", "#a3e635");
        return;
      }
      if (action === "pings" || action === "ping") {
        try {
          const res = await fetch("/api/ping");
          const data = await res.json();
          const list = data.pings || [];
          if (list.length === 0) {
            log("No device pings recorded yet.", "#f59e0b");
            return;
          }
          let out = "═".repeat(88) + "\\n";
          out += "PHONE        | SIM SLOT | STATUS | INTERVAL | LAST PING (BDT)     | MESSAGE\\n";
          out += "─".repeat(88) + "\\n";
          list.forEach(p => {
            const slotStr = p.slot !== null ? "SIM " + (p.slot + 1) : "SIM ?";
            out += (p.phone || "").padEnd(12) + " | " +
                   slotStr.padEnd(8) + " | " +
                   (p.status || "online").padEnd(6) + " | " +
                   ((p.interval_minutes || 15) + "m").padEnd(8) + " | " +
                   (p.updated_at || "").padEnd(19) + " | " +
                   (p.message || "") + "\\n";
          });
          out += "═".repeat(88) + "\\nTotal: " + list.length + " relay(s)";
          log(out, "#e2e8f0");
        } catch (err) {
          log("Error fetching pings: " + err.message, "#ff6b7a");
        }
        return;
      }
      if (action === "otp") {
        const phone = parts[1] || "";
        const pub = parts[2] || "";
        if (!phone) {
          log("Usage: otp <phone_number> [vfs|ivac]", "#f59e0b");
          return;
        }
        try {
          const qs = pub ? "?pub=" + encodeURIComponent(pub) : "";
          const res = await fetch("/api/messages/" + encodeURIComponent(phone) + qs);
          const data = await res.json();
          if (!data || !data.message) {
            log("No active OTP found for phone: " + phone, "#f59e0b");
          } else {
            let out = "─".repeat(50) + "\\n";
            out += "Phone:        " + phone + "\\n";
            out += "OTP Code:     " + data.message + "\\n";
            out += "Publisher:    " + (data.message_pub || "IVAC") + "\\n";
            out += "Last Updated: " + (data.last_updated || "N/A") + " BDT\\n";
            out += "Revision:     " + (data.revision || 0) + "\\n";
            out += "─".repeat(50);
            log(out, "#a3e635");
          }
        } catch (err) {
          log("Error retrieving OTP: " + err.message, "#ff6b7a");
        }
        return;
      }
      if (action === "messages") {
        try {
          const res = await fetch("/api/messages");
          const data = await res.json();
          const msgs = data.messages || [];
          if (msgs.length === 0) {
            log("No recent messages in retention window.", "#f59e0b");
            return;
          }
          let out = "═".repeat(70) + "\\n";
          out += "PHONE        | PUB  | REVISION | LAST UPDATED (BDT)\\n";
          out += "─".repeat(70) + "\\n";
          msgs.forEach(m => {
            out += (m.phone || "").padEnd(12) + " | " +
                   (m.message_pub || "").padEnd(4) + " | " +
                   String(m.revision || 0).padEnd(8) + " | " +
                   (m.last_updated || "") + "\\n";
          });
          out += "═".repeat(70);
          log(out, "#e2e8f0");
        } catch (err) {
          log("Error fetching messages: " + err.message, "#ff6b7a");
        }
        return;
      }
      if (action === "health") {
        try {
          const res = await fetch("/api/health");
          const data = await res.json();
          log(JSON.stringify(data, null, 2), "#a3e635");
        } catch (err) {
          log("Error fetching health: " + err.message, "#ff6b7a");
        }
        return;
      }
      if (/^(\\+?880|0)?1[3-9]\\d{8}$/.test(cmd)) {
        runCommand("otp " + cmd);
        return;
      }
      log("Unknown command: '" + cmd + "'. Type 'help' for available commands.", "#ff6b7a");
    }

    function submitInput() {
      const val = input.value;
      input.value = "";
      runCommand(val);
    }
    input.addEventListener("keydown", function(e) {
      if (e.key === "Enter") submitInput();
    });
  </script>
</body>
</html>
"""


# ── Health / Root CLI Console ──────────────────────────────────


@app.route("/")
def index():
    accept = request.headers.get("Accept", "")
    user_agent = request.headers.get("User-Agent", "").lower()

    if "curl" in user_agent or "httpie" in user_agent or "text/plain" in accept:
        base = request.host_url.rstrip("/")
        text = f"""=== SMS Server CLI Console ===
Base URL: {base}

Endpoints:
  GET  /api/ping             - List all device pings (JSON)
  GET  /api/ping/<phone>     - Get ping for phone (JSON)
  GET  /api/messages         - List recent OTP messages (JSON)
  GET  /api/messages/<phone> - Retrieve latest OTP for phone (JSON)
  GET  /<phone>              - Direct OTP lookup for phone (Text / JSON)
  GET  /api/health           - Server health check (JSON)

Terminal CLI:
  Run `python cli.py` in terminal for interactive CLI controller.
"""
        return Response(text, mimetype="text/plain")

    if "application/json" in accept and "text/html" not in accept:
        return jsonify(
            {
                "service": "sms_server",
                "status": "online",
                "endpoints": {
                    "messages": "/api/messages",
                    "messages_by_phone": "/api/messages/<phone>",
                    "pings": "/api/ping",
                    "ping_by_phone": "/api/ping/<phone>",
                    "direct_otp": "/<phone>",
                    "health": "/api/health",
                },
                "cli": "Run `python cli.py` in terminal for interactive controller",
            }
        )

    return render_template_string(CLI_CONSOLE_HTML)


@app.route("/<phone>", methods=["GET"])
def direct_get_otp(phone):
    norm = normalize_bd_phone(phone)
    if not norm or not re.fullmatch(r"01[3-9]\d{8}", norm):
        return jsonify({"error": "Invalid phone number format"}), 404

    user_agent = request.headers.get("User-Agent", "").lower()
    accept = request.headers.get("Accept", "")

    resp, status_code = api_get_messages(norm)
    data = resp.get_json() if hasattr(resp, "get_json") else {}

    if ("curl" in user_agent or "text/plain" in accept) and "application/json" not in accept:
        digits = data.get("message")
        if digits:
            return Response(f"{digits}\n", mimetype="text/plain", status=status_code)
        return Response("NO_OTP_FOUND\n", mimetype="text/plain", status=status_code)

    return resp, status_code


@app.route("/api/health")
def api_health():
    with app.ws_clients_lock:
        ws_clients = len(app.ws_clients)

    # "redis_configured" alone used to look healthy on a worker whose Redis
    # subscriber had died. Report the two separately, plus which delivery path is
    # actually carrying OTPs, because that is what determines whether a client on
    # this worker gets its OTP at all.
    poller_alive = bool(
        getattr(app, "otp_db_poller", None) and app.otp_db_poller.is_alive()
    )
    if otp_broker.subscriber_alive:
        fanout = "redis"
    elif poller_alive:
        fanout = "db-poller"
    else:
        fanout = "local-only"

    return (
        jsonify(
            {
                "service": "sms",
                "pid": os.getpid(),
                # Redis is an optional accelerator; delivery does not need it.
                "redis_configured": otp_broker.distributed,
                "redis_subscriber_alive": otp_broker.subscriber_alive,
                "otp_fanout": fanout,
                "db_poller_alive": poller_alive,
                "ws_clients": ws_clients,
                "database": app.config["SQLALCHEMY_DATABASE_URI"].split("://", 1)[0],
            }
        ),
        200,
    )


# ── Device register / authenticate ─────────────────────────────


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.json
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    name = (data.get("name") or "").strip()
    mac, mb, guid, bios, sig_count = _normalize_device_signatures(data)

    if not name:
        return jsonify({"error": "Missing device name"}), 400
    if sig_count < MIN_SIGNATURE_FIELDS:
        return jsonify(
            {"error": "At least 2 device signature fields are required"}
        ), 400

    existing = DeviceUser.query.filter_by(mac_address=mac).first()
    if existing and mac:
        return jsonify(
            {
                "error": "License already registered",
                "is_active": existing.is_active,
            }
        ), 409

    user = DeviceUser(
        name=name,
        mac_address=mac,
        motherboard_serial=mb,
        machine_guid=guid,
        bios_serial=bios,
        ws_token=generate_ws_token(),
        is_active=False,
    )
    db.session.add(user)
    db.session.commit()
    return jsonify(
        {
            "message": "Registration successful. Waiting for admin approval.",
            "user_id": user.id,
        }
    ), 201


@app.route("/api/authenticate", methods=["POST"])
def api_authenticate():
    data = request.json
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    mac, mb, guid, bios, sig_count = _normalize_device_signatures(data)
    if sig_count < MIN_SIGNATURE_FIELDS:
        return jsonify(
            {"error": "At least 2 device signature fields are required"}
        ), 400

    user, match_error = _find_user_by_signatures(mac, mb, guid, bios)

    if not user:
        if match_error == "ambiguous":
            return jsonify({"error": "Ambiguous device fingerprint"}), 409
        return jsonify({"error": "Device not found"}), 404

    if user.expires_at and user.expires_at <= datetime.utcnow():
        user.is_active = False
        db.session.commit()
        _drop_user_ws(user.id)
        return jsonify({"error": "Access expired", "status": "expired"}), 403

    if not user.is_active:
        return jsonify(
            {"error": "Account pending admin approval", "status": "inactive"}
        ), 403

    user.ensure_ws_token()
    db.session.commit()
    return jsonify(
        {
            "message": "Authentication successful",
            "status": "active",
            "user": {"id": user.id, "name": user.name},
            "ws_token": user.ws_token,
        }
    ), 200


# ── Device / SIM Ping HTTP ───────────────────────────────────────


def _record_device_ping(phone, slot=None, interval_minutes=15, status="online", message=None):
    if not phone:
        return None
    phone = normalize_bd_phone(phone) or str(phone).strip()
    now = datetime.utcnow()
    row = DevicePing.query.filter_by(phone=phone).first()
    if not row:
        row = DevicePing(phone=phone)
        db.session.add(row)
    if slot is not None:
        try:
            row.slot = int(slot)
        except (ValueError, TypeError):
            pass
    if interval_minutes is not None:
        try:
            row.interval_minutes = int(interval_minutes)
        except (ValueError, TypeError):
            pass
    row.status = str(status or "online")[:50]
    if message is not None:
        row.message = str(message)[:199]
    row.updated_at = now
    db.session.commit()
    return row


@app.route("/api/ping", methods=["POST"])
@app.route("/api/ping/", methods=["POST"])
def api_device_ping():
    data = request.json or {}
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    phone = normalize_bd_phone(data.get("phone"))
    if not phone:
        return jsonify({"error": "Missing or invalid phone"}), 400

    slot = data.get("slot")
    interval = data.get("interval_minutes") or data.get("interval")
    status = data.get("status") or "online"
    message = data.get("message")

    if not message:
        slot_str = f"SIM {int(slot) + 1}" if slot is not None else "SIM ?"
        message = f"PING | phone={phone} | {slot_str} | status={status}"

    row = _record_device_ping(
        phone=phone,
        slot=slot,
        interval_minutes=interval or 15,
        status=status,
        message=message,
    )

    return (
        jsonify(
            {
                "success": True,
                "message": "Ping recorded successfully",
                "ping": row.to_dict(),
            }
        ),
        200,
    )


@app.route("/api/ping", methods=["GET"])
@app.route("/api/ping/", methods=["GET"])
def api_list_pings():
    rows = DevicePing.query.order_by(DevicePing.updated_at.desc()).all()
    return jsonify({"pings": [r.to_dict() for r in rows]}), 200


@app.route("/api/ping/<phone>", methods=["GET"], strict_slashes=False)
def api_get_ping(phone):
    norm = normalize_bd_phone(phone)
    raw_key = str(phone or "").strip()
    row = DevicePing.query.filter(
        or_(DevicePing.phone == norm, DevicePing.phone == raw_key)
    ).first()
    if not row:
        return jsonify({"error": "Ping record not found"}), 404
    return jsonify({"ping": row.to_dict()}), 200


# ── SMS OTP HTTP ───────────────────────────────────────────────


@app.route("/api/messages", methods=["POST"])
@app.route("/api/messages/", methods=["POST"])
def api_add_message():
    data = request.json
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    phone = normalize_bd_phone(data.get("phone"))
    message = data.get("message")
    if not phone or not message:
        return jsonify({"error": "Missing phone or message"}), 400

    # Publisher: canonical snake_case `message_pub` wins; `messagePub` is accepted
    # only for backward compatibility. Clients should send exactly one.
    raw_pub = data.get("message_pub")
    if raw_pub is None or not str(raw_pub).strip():
        raw_pub = data.get("messagePub")
    raw_pub_upper = str(raw_pub or "").strip().upper()

    # Safeguard: if forwarder sent a heartbeat ping to /api/messages, record it
    # without corrupting the OTP table or publishing false OTPs.
    if raw_pub_upper == "PING" or str(message).startswith("PING |"):
        row = _record_device_ping(phone, message=str(message)[:199])
        return (
            jsonify(
                {
                    "success": True,
                    "message": "Ping recorded successfully",
                    "ping": row.to_dict() if row else None,
                }
            ),
            200,
        )

    message_pub = raw_pub_upper
    if message_pub not in ("VFS", "IVAC"):
        message_pub = DEFAULT_MESSAGE_PUB

    message = str(message)[:199]
    # One row per phone: an OTP for a known number overwrites the
    # previous one instead of appending a second row.
    now = datetime.utcnow()
    revision = _next_otp_revision()
    db.session.execute(
        text(
            """
            INSERT INTO otp_message
                (phone, otp_message, message_pub, created_at, updated_at, revision)
            VALUES (:phone, :message, :pub, :now, :now, :revision)
            ON CONFLICT (phone) DO UPDATE SET
                otp_message = excluded.otp_message,
                message_pub = excluded.message_pub,
                updated_at = excluded.updated_at,
                revision = excluded.revision
            """
        ),
        {
            "phone": phone,
            "message": message,
            "pub": message_pub,
            "now": now,
            "revision": revision,
        },
    )
    db.session.commit()

    # Only normalize OTP for IVAC (word-based OTPs like "ONE_TWO_THREE" or spaced digits "2-5-7-0-0-7").
    # VFS already sends normalized digit OTPs, so skip normalization for VFS to avoid false matches.
    otp_digits = _extract_otp_for_pub(message, message_pub)

    if otp_digits:
        otp_broker.publish(
            {
                "otp": otp_digits,
                "phone": phone,
                "last_updated": _format_bdt(now),
            }
        )

    return jsonify({"success": True, "message": "Message saved successfully"}), 201


@app.route("/api/messages", methods=["GET"])
@app.route("/api/messages/", methods=["GET"])
def api_list_messages():
    """List recent OTP rows so a bot can discover which phone actually received
    the SMS (the forwarder keys a row by the SIM that got it, which can differ
    from the phone the bot was told to poll). Used to fan out the wait.

    Returns {messages: [{phone, revision, last_updated, message_pub}]}. Optional
    ?message_pub=VFS|IVAC filters by publisher. Rows past the retention window
    are pruned first (same as the per-phone GET).
    """
    retention = _otp_retention_minutes()
    expiry_threshold = datetime.utcnow() - timedelta(minutes=retention)
    OTPMessage.query.filter(OTPMessage.updated_at < expiry_threshold).delete()
    db.session.commit()

    q = OTPMessage.query.filter(OTPMessage.updated_at >= expiry_threshold)
    wanted_pub = (
        request.args.get("pub")
        or request.args.get("message_pub")
        or request.args.get("messagePub")
        or ""
    ).strip().upper()
    if wanted_pub in ("VFS", "IVAC"):
        q = q.filter(OTPMessage.message_pub == wanted_pub)
    rows = q.order_by(OTPMessage.updated_at.desc()).limit(200).all()

    return jsonify({
        "messages": [
            {
                "phone": str(row.phone or ""),
                "message_pub": str(row.message_pub or "").upper(),
                "revision": int(row.revision or 0),
                "last_updated": _format_bdt(row.updated_at),
            }
            for row in rows
        ],
    }), 200


@app.route("/api/messages/<phone>", methods=["GET"], strict_slashes=False)
def api_get_messages(phone):
    retention = _otp_retention_minutes()
    expiry_threshold = datetime.utcnow() - timedelta(minutes=retention)
    OTPMessage.query.filter(OTPMessage.updated_at < expiry_threshold).delete()
    db.session.commit()

    norm = normalize_bd_phone(phone)
    raw_key = str(phone or "").strip()
    row = (
        OTPMessage.query.filter(
            or_(OTPMessage.phone == norm, OTPMessage.phone == raw_key),
        )
        .filter(OTPMessage.updated_at >= expiry_threshold)
        .order_by(OTPMessage.id.desc())
        .first()
    )

    if row is None:
        return jsonify({}), 200

    # Freshness cursor. A caller that passes the `revision` it saw on a previous
    # poll is telling us "only give me an OTP that arrived after this one". The
    # row is reused per phone, so its id never moves and last_updated alone
    # cannot distinguish a genuinely new SMS from a stale row still inside the
    # retention window. Without this a bot resuming after a pause immediately
    # consumes an OTP that was already used minutes ago.
    try:
        since_revision = int(request.args.get("revision", ""))
    except (TypeError, ValueError):
        since_revision = None
    if since_revision is not None and int(row.revision or 0) <= since_revision:
        return jsonify({}), 200

    # Optional publisher filter. Supports ?pub=, ?message_pub=, and ?messagePub=.
    # Omitted keeps the historical "latest row" behaviour so existing clients
    # are unaffected.
    wanted_pub = (
        request.args.get("pub")
        or request.args.get("message_pub")
        or request.args.get("messagePub")
        or ""
    ).strip().upper()
    stored_pub = (row.message_pub or "").strip().upper()
    if wanted_pub in ("VFS", "IVAC") and stored_pub != wanted_pub:
        return jsonify({}), 200

    digits = _extract_otp_for_pub(row.otp_message, stored_pub)

    if not digits:
        return jsonify({}), 200

    return jsonify(
        {
            "message": digits,
            "last_updated": _format_bdt(row.updated_at),
            "message_pub": stored_pub,
            # Monotonic arrival counter. Callers should pass this back as
            # ?revision= on their next poll to avoid re-reading a used OTP.
            "revision": int(row.revision or 0),
        }
    ), 200


# ── WebSocket ──────────────────────────────────────────────────


@sock.route("/ws/otp")
def ws_otp(ws):
    token = (request.args.get("token") or request.headers.get("X-WS-Token") or "").strip()
    if not token:
        ws.close(4001, "Unauthorized")
        return

    user = DeviceUser.query.filter_by(ws_token=token).first()
    global_token = app.config.get("WS_AUTH_TOKEN")
    if user:
        if user.is_expired:
            user.is_active = False
            db.session.commit()
            ws.close(4003, "Expired")
            return
        if not user.is_active:
            ws.close(4003, "Inactive")
            return

        _serve_ws_connection(ws, user.id)
        return

    if global_token and token == global_token:
        _serve_ws_connection(ws, None)
        return

    ws.close(4001, "Unauthorized")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
