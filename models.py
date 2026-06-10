from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

DEFAULT_MESSAGE_PUB = "IVAC"


class OTPMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False)
    otp_message = db.Column(db.String(200), nullable=False)
    is_used = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    message_pub = db.Column(
        db.String(16),
        nullable=False,
        default=DEFAULT_MESSAGE_PUB,
    )
