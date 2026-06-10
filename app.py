import os
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request
from sqlalchemy import inspect, or_, text

from models import (
    DEFAULT_MESSAGE_PUB,
    OTPMessage,
    db,
)

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Same SQLite file as before to preserve OTP data on disk.
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "device_auth.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)


def _otp_retention_minutes():
    """How long OTP rows are kept before GET cleanup deletes them. Default: 30 minutes."""
    raw = os.environ.get("OTP_RETENTION_MINUTES", "30")
    try:
        n = int(raw)
        return max(1, n)
    except ValueError:
        return 30


def ensure_otp_message_message_pub_column():
    """SQLite: db.create_all() does not add new columns; migrate existing DBs."""
    insp = inspect(db.engine)
    if "otp_message" not in insp.get_table_names():
        return
    col_names = {c["name"] for c in insp.get_columns("otp_message")}
    if "message_pub" in col_names:
        return
    with db.engine.begin() as conn:
        conn.execute(
            text("ALTER TABLE otp_message ADD COLUMN message_pub VARCHAR(16) DEFAULT 'IVAC'")
        )
        conn.execute(
            text("UPDATE otp_message SET message_pub = 'IVAC' WHERE message_pub IS NULL")
        )


def normalize_bd_phone(value):
    """BD mobile inbox key: 01XXXXXXXXX. Handles +880…, 880…, 01…, dashes/spaces."""
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



with app.app_context():
    db.create_all()
    ensure_otp_message_message_pub_column()


@app.route("/")
def index():
    return jsonify({"service": "sms"}), 200


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
    if pub is None:
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

    return jsonify({"success": True, "message": "Message saved successfully"}), 201


@app.route("/api/messages/<phone>", methods=["GET"], strict_slashes=False)
def api_get_messages(phone):
    pub = request.args.get("message_pub")
    if pub is None:
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
    # `used` = true only after a prior GET already marked these rows (reload / "already seen").
    # First GET for fresh rows: all is_used False → used false; we then set is_used True.
    used = bool(messages) and all(m.is_used for m in messages)

    for m in messages:
        m.is_used = True
    if messages:
        db.session.commit()

    bdt_tz = timezone(timedelta(hours=6))
    checked_at = datetime.now(bdt_tz).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+06:00"
    return jsonify(
        {
            "used": used,
            "count": count,
            "messages": msg_list,
            "checkedAt": checked_at,
            "messagePub": pub,
        }
    ), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
