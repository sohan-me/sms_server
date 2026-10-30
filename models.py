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
    # One row per phone *per channel*: a new sign-in OTP overwrites the previous
    # sign-in OTP for that number, and a payment OTP gets its own row, so the two
    # can never overwrite each other. This is why the unique key moved from
    # `phone` to `(phone, kind)` — see ensure_schema_columns() in app.py for the
    # migration that removes the old phone-only constraint.
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
    # Label shown to clients ("IVAC", "bKash") and the raw address the SMS
    # forwarder reported, which may be numeric or a sender ID.
    sender = db.Column(db.String(64), nullable=False, default="", server_default="")
    sender_number = db.Column(
        db.String(32), nullable=False, default="", server_default=""
    )
    # Digits for an OTP, the transaction ID for a transaction.
    code = db.Column(db.String(32), nullable=False, default="", server_default="")
    # Only set for a transaction, so the latest frame can carry it without a
    # second lookup into the history table.
    amount = db.Column(db.String(20), nullable=False, default="", server_default="")
    # The number's payment counter at the moment this row arrived: the count
    # after the increment for a transaction, the completed-payments-so-far for a
    # payment OTP. Stored so a worker delivering late reports the count that
    # belonged to that payment instead of whatever the counter says now.
    payment_count = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )
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


class PaymentTransaction(db.Model):
    """Append-only history of completed payments.

    `otp_message` keeps only the latest transaction per number so the delivery
    poller has one row to follow; this table keeps every TRX ID, because a
    payment that is re-read or reconciled later must still be findable after a
    newer one arrives.
    """

    __tablename__ = "payment_transaction"
    __table_args__ = (
        db.UniqueConstraint(
            "phone", "trx_id", name="uq_payment_transaction_phone_trx"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False)
    trx_id = db.Column(db.String(32), nullable=False)
    amount = db.Column(db.String(20), nullable=False, default="", server_default="")
    sender = db.Column(db.String(64), nullable=False, default="", server_default="")
    sender_number = db.Column(
        db.String(32), nullable=False, default="", server_default=""
    )
    raw_message = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PaymentNumber(db.Model):
    """A payment number (or account) claimed by one device.

    Users declare the numbers they will pay from *before* paying, so the server
    can keep one user's payment OTPs and transactions off another user's sockets
    and HTTP reads. `payment_count` is how many payments this number has
    completed — 1 for the first file paid, 2 for the second with the same number.
    """

    __tablename__ = "payment_number"
    __table_args__ = (
        db.UniqueConstraint("phone", name="uq_payment_number_phone"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=False, index=True)
    phone = db.Column(db.String(20), nullable=False)
    payment_count = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )
    # Digest of the device signature fields that claimed this number. The owner
    # row can be reissued with a new token (or replaced outright); the machine
    # that claimed the number is what lets it take the number back, counter and
    # all, instead of being locked out by its own previous claim.
    owner_fingerprint = db.Column(
        db.String(64), nullable=False, default="", server_default=""
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class OTPRevision(db.Model):
    """Single-row monotonic counter over OTP arrivals."""

    __tablename__ = "otp_revision"

    id = db.Column(db.Integer, primary_key=True)
    value = db.Column(db.BigInteger, nullable=False, default=0)
