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
    flash,
    jsonify,
    redirect,
    render_template,
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
    DeviceUser,
    OTPMessage,
    PaymentTransaction,
    db,
    generate_ws_token,
)
from sms_parser import (
    DELIVERY_KINDS,
    KIND_PAYMENT,
    KIND_SIGNIN,
    KIND_TRANSACTION,
    parse_sms,
)

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "sqlite:///" + os.path.join(BASE_DIR, "device_auth.db"),
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "orbitalcore-default-secret")
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


def _format_bdt(dt=None):
    bdt_tz = timezone(timedelta(hours=6))
    if dt is None:
        now = datetime.now(bdt_tz)
    elif dt.tzinfo is None:
        now = dt.replace(tzinfo=timezone.utc).astimezone(bdt_tz)
    else:
        now = dt.astimezone(bdt_tz)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+06:00"


def _channel_payload(kind, phone, code, sender, sender_number, last_updated, amount=""):
    """The body every channel shares.

    `phone` sits beside `receiver` because the original client reads `phone`;
    a transaction frame has no `otp` at all, so a payment amount can never be
    mistaken for a code by a consumer that only looks for `otp`.
    """
    payload = {
        "kind": kind,
        "phone": phone,
        "receiver": phone,
        "sender": sender,
        "sender_number": sender_number,
        "last_updated": last_updated,
    }
    if kind == KIND_TRANSACTION:
        payload["trx"] = code
        if amount:
            payload["amount"] = amount
    else:
        payload["otp"] = code
    return payload


def _otp_row_payload(row):
    """Build a channel payload from a stored row, or None if it has none.

    Rows written before the channel split carry no `code`, so their body is
    parsed instead and they keep flowing. `unknown` rows are kept for
    inspection and deliberately delivered nowhere.
    """
    if row is None:
        return None

    kind = row.kind or KIND_SIGNIN
    if kind not in DELIVERY_KINDS:
        return None

    code = (row.code or "").strip()
    if kind == KIND_TRANSACTION:
        if not code:
            return None
    else:
        code = code or _extract_normalized_otp(row.otp_message)
        if not code:
            return None

    # A row from before the split has no sender; those were IVAC sign-in OTPs.
    sender = row.sender or (DEFAULT_MESSAGE_PUB if kind == KIND_SIGNIN else "")
    return _channel_payload(
        kind=kind,
        phone=row.phone,
        code=code,
        sender=sender,
        sender_number=row.sender_number or "",
        last_updated=_format_bdt(row.updated_at),
        amount=row.amount or "",
    )


OTP_MIGRATION_COLUMNS = (
    "id",
    "phone",
    "kind",
    "otp_message",
    "message_pub",
    "sender",
    "sender_number",
    "code",
    "amount",
    "created_at",
    "updated_at",
    "revision",
)

# Columns that became NOT NULL during the migration; a row written by an
# older schema can still hold NULL, which would abort the table rebuild.
_OTP_REBUILD_COALESCE = {
    "kind": "'signin'",
    "message_pub": "'IVAC'",
    "sender": "''",
    "sender_number": "''",
    "code": "''",
    "amount": "''",
    "otp_message": "''",
    "revision": "id",
}

_OTP_REBUILD_TABLE = "otp_message_migrated"


def _phone_only_unique_keys(insp):
    """Unique keys that still enforce "one row per phone".

    Two shapes exist in the wild: the index an older ensure_schema_columns
    created, and the table constraint SQLAlchemy emitted for models.py when
    `phone` was unique=True. Both have to go before a number can hold a row per
    channel.
    """
    keys = set()
    for index in insp.get_indexes("otp_message"):
        if index.get("unique") and list(index.get("column_names") or []) == ["phone"]:
            name = index["name"]
            # SQLite's implicit indexes cannot be dropped directly.
            shape = "constraint" if name.startswith("sqlite_autoindex") else "index"
            keys.add((shape, name))
    for constraint in insp.get_unique_constraints("otp_message"):
        if list(constraint.get("column_names") or []) == ["phone"]:
            keys.add(("constraint", constraint["name"]))
    return keys


def _has_phone_kind_unique_key(insp):
    target = ["phone", "kind"]
    for index in insp.get_indexes("otp_message"):
        if index.get("unique") and list(index.get("column_names") or []) == target:
            return True
    for constraint in insp.get_unique_constraints("otp_message"):
        if list(constraint.get("column_names") or []) == target:
            return True
    return False


def _rebuild_otp_message_for_composite_key(conn, columns):
    """Remove a phone-only UNIQUE *constraint* on SQLite.

    SQLite has no ALTER TABLE ... DROP CONSTRAINT. A database created by the
    previous models.py (`phone` unique=True) carries exactly that constraint,
    and it would reject the second channel's row for a number while appearing
    in no index list -- so the table has to be rebuilt once.

    The rebuild is transactional, keeps ids and revisions (the delivery
    poller's cursor), and only copies columns the current schema knows about.
    """
    conn.execute(
        text(
            f"""
            CREATE TABLE {_OTP_REBUILD_TABLE} (
                id INTEGER NOT NULL,
                phone VARCHAR(20) NOT NULL,
                kind VARCHAR(16) NOT NULL DEFAULT 'signin',
                otp_message VARCHAR(200) NOT NULL,
                message_pub VARCHAR(16) NOT NULL DEFAULT 'IVAC',
                sender VARCHAR(64) NOT NULL DEFAULT '',
                sender_number VARCHAR(32) NOT NULL DEFAULT '',
                code VARCHAR(32) NOT NULL DEFAULT '',
                amount VARCHAR(20) NOT NULL DEFAULT '',
                created_at DATETIME,
                updated_at DATETIME,
                revision BIGINT NOT NULL DEFAULT 0,
                PRIMARY KEY (id)
            )
            """
        )
    )
    dropped = sorted(set(columns) - set(OTP_MIGRATION_COLUMNS))
    if dropped:
        # Say so rather than silently discarding a column nobody remembers:
        # the old `is_used` flag is the one this actually hits.
        logging.getLogger(__name__).warning(
            "Rebuilding otp_message for the (phone, kind) unique key; the "
            "rebuild drops columns that are no longer in the schema: %s",
            ", ".join(dropped),
        )

    copied = [name for name in OTP_MIGRATION_COLUMNS if name in columns]
    selection = [
        f"COALESCE({name}, {_OTP_REBUILD_COALESCE[name]})"
        if name in _OTP_REBUILD_COALESCE
        else name
        for name in copied
    ]
    conn.execute(
        text(
            f"INSERT INTO {_OTP_REBUILD_TABLE} ({', '.join(copied)}) "
            f"SELECT {', '.join(selection)} FROM otp_message"
        )
    )
    conn.execute(text("DROP TABLE otp_message"))
    conn.execute(text(f"ALTER TABLE {_OTP_REBUILD_TABLE} RENAME TO otp_message"))


def ensure_schema_columns():
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())

    if "otp_message" in tables:
        columns = {c["name"] for c in insp.get_columns("otp_message")}
        phone_keys = _phone_only_unique_keys(insp)
        with db.engine.begin() as conn:
            if "message_pub" not in columns:
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
                columns.add("message_pub")

            # Each channel keeps its own row per number, so a payment OTP can
            # never overwrite the sign-in OTP for the same phone.
            for name, ddl in (
                ("kind", "VARCHAR(16) NOT NULL DEFAULT 'signin'"),
                ("sender", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("sender_number", "VARCHAR(32) NOT NULL DEFAULT ''"),
                ("code", "VARCHAR(32) NOT NULL DEFAULT ''"),
                ("amount", "VARCHAR(20) NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    conn.execute(
                        text(f"ALTER TABLE otp_message ADD COLUMN {name} {ddl}")
                    )
                    columns.add(name)

            if "updated_at" not in columns:
                conn.execute(
                    text("ALTER TABLE otp_message ADD COLUMN updated_at DATETIME")
                )
                conn.execute(
                    text(
                        "UPDATE otp_message SET updated_at = created_at "
                        "WHERE updated_at IS NULL"
                    )
                )
                columns.add("updated_at")
            if "revision" not in columns:
                conn.execute(
                    text("ALTER TABLE otp_message ADD COLUMN revision BIGINT")
                )
                conn.execute(
                    text("UPDATE otp_message SET revision = id WHERE revision IS NULL")
                )
                columns.add("revision")

            conn.execute(
                text("UPDATE otp_message SET kind = 'signin' WHERE kind IS NULL OR kind = ''")
            )
            # Collapse any history per (phone, kind) so the unique key can be
            # created. Rows without a kind were sign-in OTPs before this change.
            conn.execute(
                text(
                    "DELETE FROM otp_message WHERE id NOT IN "
                    "(SELECT MAX(id) FROM otp_message GROUP BY phone, kind)"
                )
            )

            rebuilt = False
            for shape, name in sorted(phone_keys):
                if shape == "index":
                    conn.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
                    continue
                if db.engine.dialect.name == "sqlite":
                    # Reflection can report the same constraint twice (as an
                    # autoindex and as a constraint); one rebuild is enough.
                    if not rebuilt:
                        _rebuild_otp_message_for_composite_key(conn, columns)
                        rebuilt = True
                else:
                    conn.execute(
                        text(f'ALTER TABLE otp_message DROP CONSTRAINT IF EXISTS "{name}"')
                    )

        # A fresh database already has the composite key from models.py; an
        # old one only gets it here. Checking after the commit is what makes
        # this correct for both without creating a duplicate index.
        if not _has_phone_kind_unique_key(inspect(db.engine)):
            with db.engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ix_otp_message_phone_kind "
                        "ON otp_message (phone, kind)"
                    )
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
    # worker already delivered. A row is reused per (phone, kind), so the
    # channel has to be part of the identity.
    kind = payload.get("kind") or KIND_SIGNIN
    phone = normalize_bd_phone(payload.get("phone"))
    arrival_key = f"{kind}|{phone}|{payload.get('last_updated')}"
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

    # /ws/otp predates the channels and its clients must keep receiving frames
    # with exactly the three original keys.
    legacy_msg = json.dumps(
        {
            "otp": payload.get("otp", ""),
            "phone": payload.get("phone"),
            "last_updated": payload.get("last_updated"),
        }
    )
    full_msg = json.dumps(payload)
    with app.ws_clients_lock:
        clients = list(app.ws_clients.items())

    delivered = 0
    dead = []
    for connection_id, client_data in clients:
        if phone not in client_data["phones"]:
            continue
        # One connection belongs to one channel: a client subscribed to the
        # sign-in socket must never see a payment OTP for the same number.
        if client_data.get("channel", KIND_SIGNIN) != kind:
            continue
        ws_msg = legacy_msg if client_data.get("legacy") else full_msg
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
            "%s %s for phone %s matched no WebSocket subscription on this "
            "worker; %d client(s) connected here",
            kind,
            payload.get("otp") or payload.get("trx"),
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
                    payload = _otp_row_payload(row)
                    if payload is None:
                        continue
                    _broadcast_otp(payload)
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


def _serve_ws_connection(ws, user_id, channel=KIND_SIGNIN, legacy=False):
    otp_broker.start()
    connection_id = id(ws)
    with app.ws_clients_lock:
        app.ws_clients[connection_id] = {
            "ws": ws,
            "user_id": user_id,
            "phones": set(),
            "channel": channel,
            # /ws/otp keeps its original three-key frame shape.
            "legacy": legacy,
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


def requires_admin_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "admin_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)

    return decorated


# ── Health / root ──────────────────────────────────────────────


@app.route("/")
def index():
    if "admin_id" in session:
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("admin_login"))


@app.route("/api/health")
def api_health():
    with app.ws_clients_lock:
        ws_clients = len(app.ws_clients)
        ws_clients_by_channel = {kind: 0 for kind in DELIVERY_KINDS}
        for client_data in app.ws_clients.values():
            channel = client_data.get("channel", KIND_SIGNIN)
            ws_clients_by_channel[channel] = ws_clients_by_channel.get(channel, 0) + 1

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
                "ws_clients_by_channel": ws_clients_by_channel,
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

    message = str(message)[:199]
    raw_sender = data.get("sender")
    sender = raw_sender.strip() if isinstance(raw_sender, str) else ""

    # The body decides the channel. The sender only labels it and resolves
    # messages whose body omits the brand, so IVAC changing its sender number
    # cannot reroute its OTPs.
    parsed = parse_sms(message, sender=sender)

    # One row per phone *per kind*: a new OTP overwrites the previous OTP of
    # the same kind and leaves the other channels' rows alone.
    now = datetime.utcnow()
    revision = _next_otp_revision()
    db.session.execute(
        text(
            """
            INSERT INTO otp_message
                (phone, kind, otp_message, message_pub, sender, sender_number,
                 code, amount, created_at, updated_at, revision)
            VALUES (:phone, :kind, :message, :pub, :sender, :sender_number,
                    :code, :amount, :now, :now, :revision)
            ON CONFLICT (phone, kind) DO UPDATE SET
                otp_message = excluded.otp_message,
                message_pub = excluded.message_pub,
                sender = excluded.sender,
                sender_number = excluded.sender_number,
                code = excluded.code,
                amount = excluded.amount,
                updated_at = excluded.updated_at,
                revision = excluded.revision
            """
        ),
        {
            "phone": phone,
            "kind": parsed.kind,
            "message": message,
            "pub": (parsed.sender or DEFAULT_MESSAGE_PUB)[:16],
            "sender": parsed.sender[:64],
            "sender_number": parsed.sender_number[:32],
            "code": parsed.code[:32],
            "amount": parsed.amount[:20],
            "now": now,
            "revision": revision,
        },
    )

    if parsed.kind == KIND_TRANSACTION:
        # The row above only keeps the latest transaction for delivery; this
        # table keeps every TRX ID, and a retried POST cannot duplicate one.
        db.session.execute(
            text(
                """
                INSERT INTO payment_transaction
                    (phone, trx_id, amount, sender, sender_number, raw_message,
                     created_at)
                VALUES (:phone, :trx, :amount, :sender, :sender_number, :raw,
                        :now)
                ON CONFLICT (phone, trx_id) DO NOTHING
                """
            ),
            {
                "phone": phone,
                "trx": parsed.trx[:32],
                "amount": parsed.amount[:20],
                "sender": parsed.sender[:64],
                "sender_number": parsed.sender_number[:32],
                "raw": message,
                "now": now,
            },
        )

    db.session.commit()

    if parsed.kind in DELIVERY_KINDS:
        otp_broker.publish(
            _channel_payload(
                kind=parsed.kind,
                phone=phone,
                code=parsed.code,
                sender=parsed.sender,
                sender_number=parsed.sender_number,
                last_updated=_format_bdt(now),
                amount=parsed.amount,
            )
        )

    return (
        jsonify(
            {
                "success": True,
                "message": "Message saved successfully",
                "routed": parsed.kind,
            }
        ),
        201,
    )


def _latest_row(phone, kind):
    retention = _otp_retention_minutes()
    expiry_threshold = datetime.utcnow() - timedelta(minutes=retention)
    OTPMessage.query.filter(OTPMessage.updated_at < expiry_threshold).delete()
    db.session.commit()

    norm = normalize_bd_phone(phone)
    raw_key = str(phone or "").strip()
    return (
        OTPMessage.query.filter(
            or_(OTPMessage.phone == norm, OTPMessage.phone == raw_key),
            OTPMessage.kind == kind,
        )
        .filter(OTPMessage.updated_at >= expiry_threshold)
        .order_by(OTPMessage.id.desc())
        .first()
    )


def _channel_response(phone, kind):
    payload = _otp_row_payload(_latest_row(phone, kind))
    if not payload:
        return jsonify({}), 200
    return jsonify(payload), 200


@app.route("/api/messages/<phone>", methods=["GET"], strict_slashes=False)
def api_get_messages(phone):
    """Legacy endpoint: the sign-in code in the original two-key shape."""
    payload = _otp_row_payload(_latest_row(phone, KIND_SIGNIN))
    if not payload:
        return jsonify({}), 200
    return (
        jsonify(
            {
                "message": payload["otp"],
                "last_updated": payload["last_updated"],
            }
        ),
        200,
    )


@app.route("/api/signin/<phone>", methods=["GET"], strict_slashes=False)
def api_signin(phone):
    return _channel_response(phone, KIND_SIGNIN)


@app.route("/api/payment/<phone>", methods=["GET"], strict_slashes=False)
def api_payment(phone):
    return _channel_response(phone, KIND_PAYMENT)


@app.route("/api/transaction/<phone>", methods=["GET"], strict_slashes=False)
def api_transaction(phone):
    return _channel_response(phone, KIND_TRANSACTION)


def _trx_history_retention_days():
    raw = os.environ.get("TRX_HISTORY_RETENTION_DAYS", "").strip()
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


@app.route("/api/transaction/<phone>/history", methods=["GET"], strict_slashes=False)
def api_transaction_history(phone):
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 200))
    except (TypeError, ValueError):
        return jsonify({"error": "limit must be an integer"}), 400

    retention_days = _trx_history_retention_days()
    if retention_days:
        cutoff = datetime.utcnow() - timedelta(days=retention_days)
        PaymentTransaction.query.filter(
            PaymentTransaction.created_at < cutoff
        ).delete()
        db.session.commit()

    norm = normalize_bd_phone(phone)
    rows = (
        PaymentTransaction.query.filter(PaymentTransaction.phone == norm)
        .order_by(PaymentTransaction.id.desc())
        .limit(limit)
        .all()
    )
    return (
        jsonify(
            {
                "transactions": [
                    {
                        "trx": row.trx_id,
                        "amount": row.amount,
                        "sender": row.sender,
                        "sender_number": row.sender_number,
                        "phone": row.phone,
                        "receiver": row.phone,
                        "last_updated": _format_bdt(row.created_at),
                    }
                    for row in rows
                ]
            }
        ),
        200,
    )


# ── WebSocket ──────────────────────────────────────────────────


def _ws_identity(ws):
    """Authenticate a WebSocket connection; returns (user_id, ok).

    A failed check closes the socket here, so callers only act on `ok`.
    """
    token = (request.args.get("token") or request.headers.get("X-WS-Token") or "").strip()
    if not token:
        ws.close(4001, "Unauthorized")
        return None, False

    user = DeviceUser.query.filter_by(ws_token=token).first()
    if user:
        if user.is_expired:
            user.is_active = False
            db.session.commit()
            ws.close(4003, "Expired")
            return None, False
        if not user.is_active:
            ws.close(4003, "Inactive")
            return None, False
        return user.id, True

    global_token = app.config.get("WS_AUTH_TOKEN")
    if global_token and token == global_token:
        return None, True

    ws.close(4001, "Unauthorized")
    return None, False


def _serve_ws_channel(ws, channel, legacy=False):
    user_id, ok = _ws_identity(ws)
    if not ok:
        return
    _serve_ws_connection(ws, user_id, channel=channel, legacy=legacy)


@sock.route("/ws/otp")
def ws_otp(ws):
    # Legacy path: sign-in only, original frame shape. Existing OrbitalCore
    # clients keep working without a change.
    _serve_ws_channel(ws, KIND_SIGNIN, legacy=True)


@sock.route("/ws/otp/signin")
def ws_otp_signin(ws):
    _serve_ws_channel(ws, KIND_SIGNIN)


@sock.route("/ws/otp/payment")
def ws_otp_payment(ws):
    _serve_ws_channel(ws, KIND_PAYMENT)


@sock.route("/ws/otp/transaction")
def ws_otp_transaction(ws):
    _serve_ws_channel(ws, KIND_TRANSACTION)


# ── Admin auth ─────────────────────────────────────────────────


@app.route("/login", methods=["GET", "POST"])
def admin_login():
    if "admin_id" in session:
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        admin = AdminUser.query.filter_by(username=username).first()
        if admin and admin.check_password(password):
            session["admin_id"] = admin.id
            session["username"] = admin.username
            return redirect(url_for("admin_dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def admin_logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("admin_login"))


@app.route("/admin")
@requires_admin_auth
def admin_dashboard():
    users = DeviceUser.query.order_by(DeviceUser.created_at.desc()).all()
    return render_template("admin.html", users=users)


@app.route("/admin/users/add", methods=["POST"])
@requires_admin_auth
def admin_add_user():
    name = (request.form.get("name") or "").strip()
    data = {
        "mac_address": request.form.get("mac_address"),
        "motherboard_serial": request.form.get("motherboard_serial"),
        "machine_guid": request.form.get("machine_guid"),
        "bios_serial": request.form.get("bios_serial"),
    }
    mac, mb, guid, bios, sig_count = _normalize_device_signatures(data)
    if not name:
        flash("Name is required.", "danger")
        return redirect(url_for("admin_dashboard"))
    if sig_count < MIN_SIGNATURE_FIELDS:
        flash("At least 2 device signature fields are required.", "danger")
        return redirect(url_for("admin_dashboard"))
    if mac and DeviceUser.query.filter_by(mac_address=mac).first():
        flash("A user with that MAC already exists.", "danger")
        return redirect(url_for("admin_dashboard"))

    active = request.form.get("is_active") == "1"
    user = DeviceUser(
        name=name,
        mac_address=mac,
        motherboard_serial=mb,
        machine_guid=guid,
        bios_serial=bios,
        ws_token=generate_ws_token(),
        is_active=active,
    )
    db.session.add(user)
    db.session.commit()
    flash(f"User {name} created.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/toggle/<int:user_id>", methods=["POST"])
@requires_admin_auth
def toggle_user(user_id):
    user = DeviceUser.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()
    if not user.is_active:
        _drop_user_ws(user.id)
    action = "activated" if user.is_active else "deactivated"
    flash(f"User {user.name or 'Unnamed'} has been {action}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/expiry", methods=["POST"])
@requires_admin_auth
def update_user_expiry(user_id):
    user = DeviceUser.query.get_or_404(user_id)
    if request.form.get("clear_expiry") == "1":
        user.expires_at = None
        db.session.commit()
        flash("Expiry cleared.", "success")
        return redirect(url_for("admin_dashboard"))

    raw = (request.form.get("expires_at") or "").strip()
    if not raw:
        user.expires_at = None
        db.session.commit()
        flash("Expiry cleared.", "success")
        return redirect(url_for("admin_dashboard"))

    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        user.expires_at = dt
    except ValueError:
        flash("Invalid expiry date.", "danger")
        return redirect(url_for("admin_dashboard"))

    if user.expires_at <= datetime.utcnow():
        user.is_active = False
        _drop_user_ws(user.id)
    db.session.commit()
    flash("Expiry updated (UTC).", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/user/<int:user_id>/regen-token", methods=["POST"])
@requires_admin_auth
def regen_user_ws_token(user_id):
    user = DeviceUser.query.get_or_404(user_id)
    user.rotate_ws_token()
    db.session.commit()
    _drop_user_ws(user.id)
    flash(f"Auth token regenerated for {user.name or 'Unnamed'}.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete/<int:user_id>", methods=["POST"])
@requires_admin_auth
def delete_user(user_id):
    user = DeviceUser.query.get_or_404(user_id)
    display = user.name or "Unnamed"
    _drop_user_ws(user.id)
    db.session.delete(user)
    db.session.commit()
    flash(f"User {display} deleted.", "danger")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
