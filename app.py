import os
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, request
from sqlalchemy import inspect, text

from models import (
    DEFAULT_MESSAGE_PUB,
    MESSAGE_PUB_CHOICES,
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


def _parse_message_pub(raw):
    """Return normalized publisher or None if invalid."""
    if raw is None:
        return DEFAULT_MESSAGE_PUB
    s = str(raw).strip()
    if not s:
        return DEFAULT_MESSAGE_PUB
    if s not in MESSAGE_PUB_CHOICES:
        return None
    return s


with app.app_context():
    db.create_all()
    ensure_otp_message_message_pub_column()


@app.route("/")
def index():
    return jsonify({"service": "sms"}), 200


@app.route("/api/messages", methods=["POST"])
def api_add_message():
    data = request.json
    if not data:
        return jsonify({"error": "Invalid payload"}), 400

    phone = data.get("phone")
    message = data.get("message")
    if not phone or not message:
        return jsonify({"error": "Missing phone or message"}), 400

    pub = _parse_message_pub(data.get("message_pub"))
    if pub is None:
        return jsonify(
            {
                "error": "Invalid message_pub",
                "allowed": list(MESSAGE_PUB_CHOICES),
            }
        ), 400

    new_msg = OTPMessage(phone=phone, otp_message=message, message_pub=pub)
    db.session.add(new_msg)
    db.session.commit()

    return jsonify({"success": True, "message": "Message saved successfully"}), 201


@app.route("/api/messages/<phone>", methods=["GET"])
def api_get_messages(phone):
    pub = _parse_message_pub(request.args.get("message_pub"))
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

    messages = (
        OTPMessage.query.filter_by(phone=phone, message_pub=pub)
        .filter(OTPMessage.created_at >= expiry_threshold)
        .order_by(OTPMessage.id.asc())
        .all()
    )
    used = any(not m.is_used for m in messages)
    msg_list = [m.otp_message for m in messages]
    count = len(msg_list)

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
