from datetime import datetime
import secrets

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

from sms_parser import KIND_SIGNIN

db = SQLAlchemy()

# Legacy publisher column, kept for DB compatibility. The channel now lives in
# `OTPMessage.kind`; this column is still written for older readers.
DEFAULT_MESSAGE_PUB = "IVAC"


def generate_ws_token():
    """Unique per-user WebSocket auth token."""
    return secrets.token_urlsafe(32)


class AdminUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class DeviceUser(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    mac_address = db.Column(db.String(50), nullable=False)
    motherboard_serial = db.Column(db.String(100), nullable=False)
    machine_guid = db.Column(db.String(100), nullable=False)
    bios_serial = db.Column(db.String(100), nullable=False)
    ws_token = db.Column(db.String(64), unique=True, nullable=True)
    is_active = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)

    @property
    def is_expired(self):
        if not self.expires_at:
            return False
        return self.expires_at <= datetime.utcnow()

    def ensure_ws_token(self):
        if not self.ws_token:
            self.ws_token = generate_ws_token()
        return self.ws_token

    def rotate_ws_token(self):
        self.ws_token = generate_ws_token()
        return self.ws_token

    def to_dict(self, include_ws_token=False):
        data = {
            "id": self.id,
            "name": self.name,
            "is_active": self.is_active,
            "mac_address": self.mac_address,
            "motherboard_serial": self.motherboard_serial,
            "machine_guid": self.machine_guid,
            "bios_serial": self.bios_serial,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
        if include_ws_token:
            data["ws_token"] = self.ws_token
        return data


class OTPMessage(db.Model):
    # One row per phone. `kind` and the (phone, kind) unique key are kept from
    # the multi-channel schema so existing databases keep working without a
    # table rebuild; every row this server writes is a sign-in OTP.
    __table_args__ = (
        db.UniqueConstraint("phone", "kind", name="uq_otp_message_phone_kind"),
    )

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False)
    kind = db.Column(
        db.String(16),
        nullable=False,
        default=KIND_SIGNIN,
        server_default=KIND_SIGNIN,
    )
    otp_message = db.Column(db.String(200), nullable=False)
    # Label shown to clients ("IVAC") and the raw address the SMS forwarder
    # reported, which may be numeric or a sender ID.
    sender = db.Column(db.String(64), nullable=False, default="", server_default="")
    sender_number = db.Column(
        db.String(32), nullable=False, default="", server_default=""
    )
    # Digits for an OTP.
    code = db.Column(db.String(32), nullable=False, default="", server_default="")
    message_pub = db.Column(
        db.String(16),
        nullable=False,
        default=DEFAULT_MESSAGE_PUB,
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)
    # A new OTP reuses the row, so the id never moves. The delivery
    # poller follows this counter to see every arrival.
    revision = db.Column(db.BigInteger, nullable=False, default=0)


class OTPRevision(db.Model):
    """Single-row monotonic counter over OTP arrivals."""

    __tablename__ = "otp_revision"

    id = db.Column(db.Integer, primary_key=True)
    value = db.Column(db.BigInteger, nullable=False, default=0)
