import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone


_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file.name}"

from app import (  # noqa: E402
    _broadcast_otp,
    _drop_user_ws,
    _format_bdt,
    _parse_subscription,
    _serve_ws_connection,
    app,
)
from models import OTPMessage, db  # noqa: E402


class FakeSocket:
    def __init__(self, incoming=None):
        self.incoming = list(incoming or [])
        self.sent = []
        self.closed = []

    def receive(self):
        if self.incoming:
            return self.incoming.pop(0)
        return None

    def send(self, message):
        self.sent.append(json.loads(message))

    def close(self, code, reason):
        self.closed.append((code, reason))


class OTPServerTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        with app.ws_clients_lock:
            app.ws_clients.clear()

    def tearDown(self):
        with app.ws_clients_lock:
            app.ws_clients.clear()
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_subscription_normalizes_and_deduplicates_phones(self):
        phones, error = _parse_subscription(
            json.dumps(
                {
                    "action": "subscribe",
                    "phones": ["+880 1712-345678", "01712345678"],
                }
            )
        )

        self.assertIsNone(error)
        self.assertEqual(phones, {"01712345678"})

    def test_invalid_subscription_returns_an_error(self):
        socket = FakeSocket(
            [
                json.dumps({"action": "subscribe", "phones": []}),
                None,
            ]
        )

        _serve_ws_connection(socket, user_id=10)

        self.assertEqual(socket.sent[0]["type"], "error")
        self.assertEqual(app.ws_clients, {})

    def test_broadcast_only_reaches_matching_subscriptions(self):
        first = FakeSocket()
        second = FakeSocket()
        same_user_second_connection = FakeSocket()
        with app.ws_clients_lock:
            app.ws_clients.update(
                {
                    id(first): {
                        "ws": first,
                        "user_id": 1,
                        "phones": {"01712345678"},
                    },
                    id(second): {
                        "ws": second,
                        "user_id": 2,
                        "phones": {"01612345678"},
                    },
                    id(same_user_second_connection): {
                        "ws": same_user_second_connection,
                        "user_id": 1,
                        "phones": {"01712345678"},
                    },
                }
            )

        _broadcast_otp({"phone": "+8801712345678", "otp": "123456"})

        self.assertEqual(len(first.sent), 1)
        self.assertEqual(len(same_user_second_connection.sent), 1)
        self.assertEqual(second.sent, [])

    def test_drop_user_closes_all_of_that_users_connections(self):
        first = FakeSocket()
        second = FakeSocket()
        other_user = FakeSocket()
        with app.ws_clients_lock:
            app.ws_clients.update(
                {
                    id(first): {"ws": first, "user_id": 1, "phones": set()},
                    id(second): {"ws": second, "user_id": 1, "phones": set()},
                    id(other_user): {
                        "ws": other_user,
                        "user_id": 2,
                        "phones": set(),
                    },
                }
            )

        _drop_user_ws(1)

        self.assertEqual(first.closed, [(4003, "Access revoked")])
        self.assertEqual(second.closed, [(4003, "Access revoked")])
        self.assertFalse(other_user.closed)
        self.assertEqual(list(app.ws_clients), [id(other_user)])

    def test_http_uses_each_message_creation_time_for_checked_at(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        first_time = now - timedelta(minutes=2)
        second_time = now - timedelta(minutes=1)
        with app.app_context():
            db.session.add_all(
                [
                    OTPMessage(
                        phone="01712345678",
                        otp_message="111111",
                        created_at=first_time,
                    ),
                    OTPMessage(
                        phone="01712345678",
                        otp_message="222222",
                        created_at=second_time,
                    ),
                ]
            )
            db.session.commit()

        response = app.test_client().get("/api/messages/01712345678")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["checkedAt"] for item in payload],
            [_format_bdt(first_time), _format_bdt(second_time)],
        )
        self.assertNotEqual(payload[0]["checkedAt"], payload[1]["checkedAt"])


def tearDownModule():
    try:
        os.unlink(_db_file.name)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    unittest.main()
