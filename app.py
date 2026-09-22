import json
import os
import re
import threading
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
from sqlalchemy import inspect, or_, text

from models import (
    DEFAULT_MESSAGE_PUB,
    AdminUser,
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
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "orbitalcore-default-secret")
app.config["WS_AUTH_TOKEN"] = os.environ.get("WS_AUTH_TOKEN", "").strip() or None

db.init_app(app)
sock = Sock(app)
app.ws_clients = {}  # connection_id -> {ws, user_id, phones}
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
    return str(value).strip()


def _normalize_device_signatures(data):
    mac = _norm_sig(data.get("mac_address"))
    mb = _norm_sig(data.get("motherboard_serial"))
    guid = _norm_sig(data.get("machine_guid"))
    bios = _norm_sig(data.get("bios_serial"))
    count = sum(1 for v in (mac, mb, guid, bios) if v)
    return mac, mb, guid, bios, count


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


def ensure_schema_columns():
    insp = inspect(db.engine)
    tables = set(insp.get_table_names())

    if "otp_message" in tables:
        cols = {c["name"] for c in insp.get_columns("otp_message")}
        if "message_pub" not in cols:
            with db.engine.begin() as conn:
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


def _broadcast_otp(payload: dict):
    ws_msg = json.dumps(payload)
    phone = normalize_bd_phone(payload.get("phone"))
    with app.ws_clients_lock:
        clients = list(app.ws_clients.items())

    dead = []
    for connection_id, client_data in clients:
        if phone not in client_data["phones"]:
            continue
        try:
            client_data["ws"].send(ws_msg)
        except Exception:
            dead.append((connection_id, client_data["ws"]))

    with app.ws_clients_lock:
        for connection_id, client in dead:
            current = app.ws_clients.get(connection_id)
            if current and current["ws"] is client:
                app.ws_clients.pop(connection_id, None)


def _drop_user_ws(user_id):
    with app.ws_clients_lock:
        clients = [
            (connection_id, client_data["ws"])
            for connection_id, client_data in app.ws_clients.items()
            if client_data["user_id"] == user_id
        ]
        for connection_id, _ in clients:
            app.ws_clients.pop(connection_id, None)

    for _, client in clients:
        try:
            client.close(4003, "Access revoked")
        except Exception:
            pass


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
    connection_id = id(ws)
    with app.ws_clients_lock:
        app.ws_clients[connection_id] = {
            "ws": ws,
            "user_id": user_id,
            "phones": set(),
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

            ws.send(
                json.dumps(
                    {
                        "type": "subscribed",
                        "phones": sorted(phones),
                    }
                )
            )
    except Exception:
        pass
    finally:
        with app.ws_clients_lock:
            client_data = app.ws_clients.get(connection_id)
            if client_data and client_data["ws"] is ws:
                app.ws_clients.pop(connection_id, None)


with app.app_context():
    db.create_all()
    ensure_schema_columns()
    backfill_missing_ws_tokens()
    if not AdminUser.query.filter_by(username="incomplete").first():
        admin = AdminUser(username="incomplete")
        admin.set_password(os.environ.get("ADMIN_PASSWORD", "Barhatta&2026"))
        db.session.add(admin)
        db.session.commit()


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
    return jsonify({"service": "sms"}), 200


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

    user = DeviceUser.query.filter_by(
        mac_address=mac,
        motherboard_serial=mb,
        machine_guid=guid,
        bios_serial=bios,
    ).first()

    if not user:
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
    new_msg = OTPMessage(
        phone=phone,
        otp_message=message,
        message_pub=DEFAULT_MESSAGE_PUB,
    )
    db.session.add(new_msg)
    db.session.commit()

    otp_digits = _extract_normalized_otp(message)
    if otp_digits:
        _broadcast_otp(
            {
                "id": new_msg.id,
                "otp": otp_digits,
                "phone": new_msg.phone,
                "used": bool(new_msg.is_used),
                "created_at": _format_bdt(new_msg.created_at),
            }
        )

    return jsonify({"success": True, "message": "Message saved successfully"}), 201


@app.route("/api/messages/<phone>", methods=["GET"], strict_slashes=False)
def api_get_messages(phone):
    retention = _otp_retention_minutes()
    expiry_threshold = datetime.utcnow() - timedelta(minutes=retention)
    OTPMessage.query.filter(OTPMessage.created_at < expiry_threshold).delete()
    db.session.commit()

    norm = normalize_bd_phone(phone)
    raw_key = str(phone or "").strip()
    messages = (
        OTPMessage.query.filter(
            or_(OTPMessage.phone == norm, OTPMessage.phone == raw_key),
        )
        .filter(OTPMessage.created_at >= expiry_threshold)
        .order_by(OTPMessage.id.asc())
        .all()
    )

    # Only return normalized digit OTPs (drops junk like "test-no-pub")
    msg_list = []
    for m in messages:
        digits = _extract_normalized_otp(m.otp_message)
        if not digits:
            continue
        msg_list.append(
            {
                "checkedAt": _format_bdt(m.created_at),
                "count": len(msg_list) + 1,
                "message": digits,
                "used": bool(m.is_used),
            }
        )

    for m in messages:
        m.is_used = True
    if messages:
        db.session.commit()

    return jsonify(msg_list), 200


# ── WebSocket ──────────────────────────────────────────────────


@sock.route("/ws/otp")
def ws_otp(ws):
    token = (request.args.get("token") or request.headers.get("X-WS-Token") or "").strip()
    if not token:
        ws.close(4001, "Unauthorized")
        return

    user = DeviceUser.query.filter_by(ws_token=token).first()
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

    global_token = app.config.get("WS_AUTH_TOKEN")
    if global_token and token == global_token:
        _serve_ws_connection(ws, None)
        return

    ws.close(4001, "Unauthorized")


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
