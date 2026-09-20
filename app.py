import json
import os
import re
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
    MESSAGE_PUB_CHOICES,
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
app.ws_clients = {}  # user_id -> websocket
app.ws_client = None  # legacy global token slot

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
    dead = []
    for user_id, client in list(app.ws_clients.items()):
        try:
            client.send(ws_msg)
        except Exception:
            dead.append(user_id)
    for user_id in dead:
        app.ws_clients.pop(user_id, None)
    if app.ws_client:
        try:
            app.ws_client.send(ws_msg)
        except Exception:
            app.ws_client = None


def _drop_user_ws(user_id):
    client = app.ws_clients.pop(user_id, None)
    if client is None:
        return
    try:
        client.close(4003, "Access revoked")
    except Exception:
        pass


def _format_bdt(dt=None):
    bdt_tz = timezone(timedelta(hours=6))
    if dt is None:
        now = datetime.now(bdt_tz)
    elif dt.tzinfo is None:
        now = dt.replace(tzinfo=timezone.utc).astimezone(bdt_tz)
    else:
        now = dt.astimezone(bdt_tz)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+06:00"


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

    pub = data.get("message_pub")
    if pub is None or pub not in MESSAGE_PUB_CHOICES:
        return jsonify(
            {
                "error": "Invalid message_pub",
                "allowed": list(MESSAGE_PUB_CHOICES),
            }
        ), 400

    message = str(message)[:199]
    new_msg = OTPMessage(phone=phone, otp_message=message, message_pub=pub)
    db.session.add(new_msg)
    db.session.commit()

    _broadcast_otp(
        {
            "id": new_msg.id,
            "otp": new_msg.otp_message,
            "phone": new_msg.phone,
            "used": bool(new_msg.is_used),
            "message_pub": new_msg.message_pub,
            "created_at": _format_bdt(new_msg.created_at),
        }
    )

    return jsonify({"success": True, "message": "Message saved successfully"}), 201


@app.route("/api/messages/<phone>", methods=["GET"], strict_slashes=False)
def api_get_messages(phone):
    pub = request.args.get("message_pub")
    if pub is None or pub not in MESSAGE_PUB_CHOICES:
        return jsonify(
            {
                "error": "Invalid message_pub",
                "allowed": list(MESSAGE_PUB_CHOICES),
            }
        ), 400

    retention = _otp_retention_minutes()
    expiry_threshold = datetime.utcnow() - timedelta(minutes=retention)
    OTPMessage.query.filter(OTPMessage.created_at < expiry_threshold).delete()
    db.session.commit()

    norm = normalize_bd_phone(phone)
    raw_key = str(phone or "").strip()
    messages = (
        OTPMessage.query.filter(
            or_(OTPMessage.phone == norm, OTPMessage.phone == raw_key),
            OTPMessage.message_pub == pub,
        )
        .filter(OTPMessage.created_at >= expiry_threshold)
        .order_by(OTPMessage.id.asc())
        .all()
    )
    msg_list = [m.otp_message for m in messages]
    count = len(msg_list)
    used = bool(messages) and all(m.is_used for m in messages)

    for m in messages:
        m.is_used = True
    if messages:
        db.session.commit()

    return jsonify(
        {
            "used": used,
            "count": count,
            "messages": msg_list,
            "checkedAt": _format_bdt(),
            "messagePub": pub,
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
    if user:
        if user.is_expired:
            user.is_active = False
            db.session.commit()
            ws.close(4003, "Expired")
            return
        if not user.is_active:
            ws.close(4003, "Inactive")
            return

        user_id = user.id
        old = app.ws_clients.get(user_id)
        if old is not None and old is not ws:
            try:
                old.close(4002, "Replaced")
            except Exception:
                pass
        app.ws_clients[user_id] = ws
        try:
            while True:
                ws.receive(timeout=30)
        except Exception:
            pass
        finally:
            if app.ws_clients.get(user_id) is ws:
                app.ws_clients.pop(user_id, None)
        return

    global_token = app.config.get("WS_AUTH_TOKEN")
    if global_token and token == global_token:
        app.ws_client = ws
        try:
            while True:
                ws.receive(timeout=30)
        except Exception:
            pass
        finally:
            if app.ws_client is ws:
                app.ws_client = None
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
