import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone


_db_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_db_file.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file.name}"
os.environ["DISABLE_DB_OTP_POLLER"] = "1"

from app import (  # noqa: E402
    _broadcast_otp,
    _drop_user_ws,
    _format_bdt,
    _parse_subscription,
    _serve_ws_connection,
    _unregister_ws,
    app,
)
from models import DeviceUser, OTPMessage, db  # noqa: E402


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

    def close(self, code=1000, reason=""):
        self.closed.append((code, reason))


def register_client(ws, user_id, phones):
    """Mirror the entry shape _serve_ws_connection installs."""
    with app.ws_clients_lock:
        app.ws_clients[id(ws)] = {
            "ws": ws,
            "user_id": user_id,
            "phones": set(phones),
            "send_lock": threading.Lock(),
        }
    return id(ws)


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
        register_client(first, 1, ["01712345678"])
        register_client(second, 2, ["01612345678"])
        register_client(same_user_second_connection, 1, ["01712345678"])

        _broadcast_otp({"phone": "+8801712345678", "otp": "123456"})

        self.assertEqual(len(first.sent), 1)
        self.assertEqual(len(same_user_second_connection.sent), 1)
        self.assertEqual(second.sent, [])

    def test_drop_user_closes_all_of_that_users_connections(self):
        first = FakeSocket()
        second = FakeSocket()
        other_user = FakeSocket()
        register_client(first, 1, [])
        register_client(second, 1, [])
        register_client(other_user, 2, [])

        _drop_user_ws(1)

        self.assertEqual(first.closed, [(4003, "Access revoked")])
        self.assertEqual(second.closed, [(4003, "Access revoked")])
        self.assertFalse(other_user.closed)
        self.assertEqual(list(app.ws_clients), [id(other_user)])

    def test_unregister_closes_the_socket_so_no_zombie_client_remains(self):
        """Unregistering must close the socket.

        Popping the entry while leaving the socket open leaves the handler thread
        blocked in ws.receive() and the peer believing it is still connected, so the
        OTP simply never arrives again for that client.
        """
        ws = FakeSocket()
        connection_id = register_client(ws, 5, ["01712345678"])

        _unregister_ws(connection_id)

        self.assertNotIn(connection_id, app.ws_clients)
        self.assertEqual(len(ws.closed), 1)

    def test_unregister_ignores_a_stale_socket_and_leaves_the_live_entry(self):
        original = FakeSocket()
        connection_id = register_client(original, 5, ["01712345678"])
        replacement = FakeSocket()
        with app.ws_clients_lock:
            app.ws_clients[connection_id]["ws"] = replacement

        _unregister_ws(connection_id, expected_ws=original)

        self.assertIn(connection_id, app.ws_clients)
        self.assertIs(app.ws_clients[connection_id]["ws"], replacement)
        self.assertFalse(original.closed)

    def test_a_failed_send_unregisters_and_closes_the_dead_client(self):
        class DeadSocket(FakeSocket):
            def send(self, message):
                raise ConnectionResetError("peer went away")

        dead = DeadSocket()
        survivor = FakeSocket()
        register_client(dead, 1, ["01712345678"])
        register_client(survivor, 2, ["01712345678"])

        _broadcast_otp({"phone": "01712345678", "otp": "123456", "id": 991})

        self.assertNotIn(id(dead), app.ws_clients)
        self.assertEqual(len(dead.closed), 1)
        self.assertEqual(len(survivor.sent), 1)

    def test_concurrent_broadcasts_do_not_interleave_writes(self):
        """Two writers on one socket must be serialised.

        The Redis subscriber thread and this client's handler thread both write to
        the same socket, and simple_websocket's send() mutates wsproto state then
        writes raw bytes with no lock of its own. Unserialised, the peer's frame
        parser desyncs and silently discards every later message.
        """
        ws = FakeSocket()
        register_client(ws, 1, ["01712345678"])
        active = {"now": 0}
        peak = {"now": 0}
        guard = threading.Lock()
        real_send = ws.send

        def instrumented(message):
            with guard:
                active["now"] += 1
                peak["now"] = max(peak["now"], active["now"])
            time.sleep(0.005)
            real_send(message)
            with guard:
                active["now"] -= 1

        ws.send = instrumented

        def broadcast(index):
            _broadcast_otp(
                {"phone": "01712345678", "otp": f"5555{index:02d}", "id": index}
            )

        threads = [threading.Thread(target=broadcast, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(peak["now"], 1, "writes to one socket overlapped")
        self.assertEqual(len(ws.sent), 12)

    def test_http_returns_the_documented_envelope_with_top_level_used(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with app.app_context():
            db.session.add_all(
                [
                    OTPMessage(
                        phone="01712345678",
                        otp_message="111111",
                        created_at=now - timedelta(minutes=2),
                    ),
                    OTPMessage(
                        phone="01712345678",
                        otp_message="222222",
                        created_at=now - timedelta(minutes=1),
                    ),
                ]
            )
            db.session.commit()

        response = app.test_client().get("/api/messages/01712345678")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)

        # The envelope is exactly these three keys and no others.
        self.assertEqual(
            sorted(payload.keys()), ["checkedAt", "count", "messages"]
        )

        # `count` is how many OTPs this poll returned, so it tracks the list.
        self.assertEqual(payload["count"], 2)
        self.assertEqual(payload["count"], len(payload["messages"]))
        self.assertTrue(payload["checkedAt"])

        # Every code is its own object carrying {otp, used, id, created_at}, so
        # a bot can tell them apart and tell which is newest.
        self.assertEqual(len(payload["messages"]), 2)
        for item in payload["messages"]:
            self.assertIsInstance(item, dict)
            self.assertEqual(
                sorted(item), ["created_at", "id", "otp", "used"]
            )

        codes = [item["otp"] for item in payload["messages"]]
        self.assertEqual(codes, ["111111", "222222"])

        # Each item carries its own creation time, and they differ, so ordering
        # is unambiguous without any extra field.
        times = [item["created_at"] for item in payload["messages"]]
        self.assertEqual(len(set(times)), 2)
        self.assertEqual(times, sorted(times))
        ids = [item["id"] for item in payload["messages"]]
        self.assertEqual(ids, sorted(ids))

    def test_http_reports_each_otp_as_its_own_object(self):
        """One OTP -> count 1; three OTPs -> count 3, never a shared shape."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with app.app_context():
            for i in range(3):
                db.session.add(
                    OTPMessage(
                        phone="01712345679",
                        otp_message="10000%d" % i,
                        created_at=now - timedelta(minutes=3 - i),
                    )
                )
            db.session.commit()

        payload = app.test_client().get("/api/messages/01712345679").get_json()

        self.assertEqual(payload["count"], 3)
        self.assertEqual(
            [item["otp"] for item in payload["messages"]],
            ["100000", "100001", "100002"],
        )
        # No shared/duplicated envelope: each OTP is one distinct object.
        self.assertEqual(
            len({item["id"] for item in payload["messages"]}), 3
        )
        for item in payload["messages"]:
            self.assertEqual(
                sorted(item), ["created_at", "id", "otp", "used"]
            )

    def test_http_returns_an_empty_list_when_nothing_matches(self):
        response = app.test_client().get("/api/messages/01700000000")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["messages"], [])
        self.assertTrue(payload["checkedAt"])

    def test_authentication_accepts_any_two_matching_fingerprint_fields(self):
        with app.app_context():
            user = DeviceUser(
                name="Two Field User",
                mac_address="AA:BB:CC:DD:EE:01",
                motherboard_serial="MB-ONE",
                machine_guid="GUID-ONE",
                bios_serial="BIOS-ONE",
                ws_token="two-field-token",
                is_active=True,
            )
            db.session.add(user)
            db.session.commit()

        response = app.test_client().post(
            "/api/authenticate",
            json={
                "mac_address": "AA:BB:CC:DD:EE:01",
                "machine_guid": "GUID-ONE",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["ws_token"], "two-field-token")

    def test_authentication_rejects_fewer_than_two_matches(self):
        with app.app_context():
            db.session.add(
                DeviceUser(
                    name="Mismatch User",
                    mac_address="AA:BB:CC:DD:EE:02",
                    motherboard_serial="MB-TWO",
                    machine_guid="GUID-TWO",
                    bios_serial="BIOS-TWO",
                    ws_token="mismatch-token",
                    is_active=True,
                )
            )
            db.session.commit()

        response = app.test_client().post(
            "/api/authenticate",
            json={
                "mac_address": "AA:BB:CC:DD:EE:02",
                "machine_guid": "WRONG-GUID",
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "Device not found")

    def test_authentication_rejects_ambiguous_two_field_match(self):
        with app.app_context():
            db.session.add_all(
                [
                    DeviceUser(
                        name="First Shared Device",
                        mac_address="AA:BB:CC:DD:EE:03",
                        motherboard_serial="SHARED-MB",
                        machine_guid="SHARED-GUID",
                        bios_serial="BIOS-THREE",
                        ws_token="shared-token-one",
                        is_active=True,
                    ),
                    DeviceUser(
                        name="Second Shared Device",
                        mac_address="AA:BB:CC:DD:EE:04",
                        motherboard_serial="SHARED-MB",
                        machine_guid="SHARED-GUID",
                        bios_serial="BIOS-FOUR",
                        ws_token="shared-token-two",
                        is_active=True,
                    ),
                ]
            )
            db.session.commit()

        response = app.test_client().post(
            "/api/authenticate",
            json={
                "motherboard_serial": "SHARED-MB",
                "machine_guid": "SHARED-GUID",
            },
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()["error"],
            "Ambiguous device fingerprint",
        )

    def test_health_reports_the_path_actually_carrying_otps(self):
        """`db_poller_alive` is the field that decides cross-worker delivery.

        A worker whose poller is dead and has no Redis is `local-only`: clients on
        it only get OTPs ingested by that same worker, which is exactly the bug
        where the admin works and nobody else does.
        """
        response = app.test_client().get("/api/health")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        # `redis_configured` only says whether the variable was set, so it is not a
        # health signal on its own; these two are what actually describe delivery.
        self.assertIn("redis_configured", payload)
        self.assertIn("redis_subscriber_alive", payload)
        self.assertIn("db_poller_alive", payload)
        self.assertIn(payload["otp_fanout"], {"redis", "db-poller", "local-only"})

        if not payload["redis_subscriber_alive"]:
            # Without a live subscriber the poller is the only thing carrying
            # cross-worker OTPs, so it must be the reported path.
            self.assertEqual(
                payload["otp_fanout"],
                "db-poller" if payload["db_poller_alive"] else "local-only",
            )


class RedisIsOptionalTests(unittest.TestCase):
    """Redis is an accelerator, never a requirement.

    The broker is the fast path; the database poller in app.py is what actually
    carries OTPs between workers. If a hard `import redis` ever comes back, the
    app stops starting on any host without the package and delivery breaks.
    """

    def test_broker_is_inert_without_a_redis_url(self):
        from otp_broker import OTPBroker

        delivered = []
        broker = OTPBroker(None, on_message=delivered.append)

        self.assertFalse(broker.distributed)
        self.assertFalse(broker.subscriber_alive)
        self.assertTrue(broker.start())
        # publish() must still deliver locally even with no broker, so the OTP
        # reaches clients on this worker immediately.
        self.assertTrue(broker.publish({"phone": "01712345678", "otp": "111111"}))
        self.assertEqual(len(delivered), 1)

    def test_broker_reports_itself_inert_when_the_redis_package_is_absent(self):
        import otp_broker as module

        broker = module.OTPBroker("redis://127.0.0.1:6379/0", on_message=lambda p: None)

        if module.redis is None:
            # URL configured but the package cannot be imported: must degrade to
            # the poller rather than raise at call time.
            self.assertFalse(broker.distributed)
            self.assertTrue(broker.start())
            self.assertTrue(broker.publish({"phone": "01712345678", "otp": "1"}))
        else:
            self.assertTrue(broker.distributed)

    def test_published_otp_reaches_a_subscribed_client_without_redis(self):
        """End-to-end shape of the fix: ingest -> broadcast -> client frame."""
        client_ws = FakeSocket()
        register_client(client_ws, 1, ["01712345678"])

        from otp_broker import OTPBroker

        broker = OTPBroker("", on_message=_broadcast_otp)
        broker.publish(
            {
                "id": 4242,
                "otp": "135790",
                "phone": "01712345678",
                "used": False,
                "created_at": _format_bdt(),
            }
        )

        self.assertEqual(len(client_ws.sent), 1)
        self.assertEqual(client_ws.sent[0]["otp"], "135790")
        self.assertEqual(client_ws.sent[0]["phone"], "01712345678")


def tearDownModule():
    try:
        os.unlink(_db_file.name)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    unittest.main()
