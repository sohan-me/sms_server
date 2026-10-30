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
    _otp_row_payload,
    _parse_subscription,
    _serve_ws_channel,
    _serve_ws_connection,
    _unregister_ws,
    app,
)
from models import AdminUser, DeviceUser, OTPMessage, db  # noqa: E402
from sms_parser import (  # noqa: E402
    KIND_PAYMENT,
    KIND_SIGNIN,
    KIND_TRANSACTION,
    KIND_UNKNOWN,
)


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


def register_client(ws, user_id, phones, channel=KIND_SIGNIN, legacy=False):
    """Mirror the entry shape _serve_ws_connection installs."""
    with app.ws_clients_lock:
        app.ws_clients[id(ws)] = {
            "ws": ws,
            "user_id": user_id,
            "phones": set(phones),
            "channel": channel,
            "legacy": legacy,
            "send_lock": threading.Lock(),
        }
    return id(ws)


def register_device(name, phones=None):
    """An approved device with a ws_token, plus any payment numbers it claims."""
    with app.app_context():
        user = DeviceUser(
            name=name,
            mac_address=f"MAC-{name}",
            motherboard_serial=f"MB-{name}",
            machine_guid=f"GUID-{name}",
            bios_serial=f"BIOS-{name}",
            ws_token=f"token-{name}",
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    token = f"token-{name}"
    if phones:
        requested = [phones] if isinstance(phones, str) else list(phones)
        response = app.test_client().post(
            "/api/payment/numbers",
            json={"numbers": requested},
            query_string={"token": token},
        )
        assert response.status_code == 200, response.get_json()
    return user_id, token


class OTPServerTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}

    def tearDown(self):
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}
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

        _broadcast_otp(
            {
                "phone": "01712345678",
                "otp": "123456",
                "last_updated": "2026-01-01T00:00:00.000+06:00",
            }
        )

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
                {
                    "phone": "01712345678",
                    "otp": f"5555{index:02d}",
                    "last_updated": f"2026-01-01T00:00:{index:02d}.000+06:00",
                }
            )

        threads = [threading.Thread(target=broadcast, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(peak["now"], 1, "writes to one socket overlapped")
        self.assertEqual(len(ws.sent), 12)

    def test_http_returns_a_single_object_with_the_documented_keys(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with app.app_context():
            db.session.add(
                OTPMessage(
                    phone="01712345678",
                    otp_message="111111",
                    created_at=now - timedelta(minutes=2),
                    updated_at=now - timedelta(minutes=1),
                )
            )
            db.session.commit()

        response = app.test_client().get("/api/messages/01712345678")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)

        # One row per number: the body is a single object, not a list.
        self.assertIsInstance(payload, dict)
        self.assertEqual(sorted(payload), ["last_updated", "message"])
        self.assertEqual(payload["message"], "111111")
        self.assertTrue(payload["last_updated"].endswith("+06:00"))

    def test_ingesting_a_newer_otp_overwrites_the_number_s_row(self):
        client = app.test_client()

        for code in ("111111", "222222"):
            response = client.post(
                "/api/messages",
                json={
                    "phone": "01712345679",
                    "message": f"Your IVAC verification code is {code}.",
                },
            )
            self.assertEqual(response.status_code, 201)

        with app.app_context():
            rows = OTPMessage.query.filter_by(phone="01712345679").all()

        # No new object per OTP: the second arrival updated the row.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].otp_message, "Your IVAC verification code is 222222.")

        payload = client.get("/api/messages/01712345679").get_json()
        self.assertEqual(payload["message"], "222222")

    def test_http_returns_only_the_latest_otp_for_a_number(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        with app.app_context():
            row = OTPMessage(
                phone="01712345680",
                otp_message="100000",
                created_at=now - timedelta(minutes=3),
                updated_at=now - timedelta(minutes=3),
            )
            db.session.add(row)
            db.session.commit()
            row.otp_message = "200000"
            row.updated_at = now - timedelta(minutes=1)
            db.session.commit()

        payload = app.test_client().get("/api/messages/01712345680").get_json()

        self.assertEqual(payload["message"], "200000")

    def test_http_returns_an_empty_object_when_nothing_matches(self):
        response = app.test_client().get("/api/messages/01700000000")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload, {})

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

    def setUp(self):
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}

    def tearDown(self):
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}

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
                "otp": "135790",
                "phone": "01712345678",
                "last_updated": _format_bdt(),
            }
        )

        self.assertEqual(len(client_ws.sent), 1)
        self.assertEqual(client_ws.sent[0]["otp"], "135790")
        self.assertEqual(client_ws.sent[0]["phone"], "01712345678")

    def test_the_same_arrival_is_not_delivered_twice(self):
        """Local publish plus its Redis/poller echo must reach the client once."""
        client_ws = FakeSocket()
        register_client(client_ws, 1, ["01712345678"])

        frame = {
            "otp": "135790",
            "phone": "01712345678",
            "last_updated": "2026-01-01T00:00:00.000+06:00",
        }
        _broadcast_otp(dict(frame))
        _broadcast_otp(dict(frame))

        self.assertEqual(len(client_ws.sent), 1)

    def test_a_newer_otp_for_the_same_number_still_delivers(self):
        """The row is reused per number, so the arrival time — not the
        row id — is what makes two OTPs for one phone distinct."""
        client_ws = FakeSocket()
        register_client(client_ws, 1, ["01712345678"])

        _broadcast_otp(
            {
                "otp": "111111",
                "phone": "01712345678",
                "last_updated": "2026-01-01T00:00:00.000+06:00",
            }
        )
        _broadcast_otp(
            {
                "otp": "222222",
                "phone": "01712345678",
                "last_updated": "2026-01-01T00:00:01.000+06:00",
            }
        )

        self.assertEqual(len(client_ws.sent), 2)
        self.assertEqual(client_ws.sent[1]["otp"], "222222")

class ChannelRoutingTests(unittest.TestCase):
    """One ingest, three channels: storage, HTTP and WebSocket filtering."""

    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}

    def tearDown(self):
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}
        with app.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _post_message(client, phone, message, sender=None):
        payload = {"phone": phone, "message": message}
        if sender is not None:
            payload["sender"] = sender
        return client.post("/api/messages", json=payload)

    def test_each_channel_is_stored_and_served_on_its_own_endpoint(self):
        phone = "01712340001"
        client = app.test_client()
        # Payment numbers are claimed before paying; the claim is also what
        # makes the payment/transaction reads readable.
        _, token = register_device("channel-buyer", phone)

        signin = self._post_message(
            client, phone, "Your IVAC verification code is ONE_TWO_THREE_FOUR_FIVE_SIX"
        )
        payment = self._post_message(
            client, phone, "Your bKash OTP is 456789. Do not share.", sender="bKash"
        )
        transaction = self._post_message(
            client,
            phone,
            "You have received Tk 500.00 from 01711111111. TrxID 9HABCDEFG1.",
            sender="bKash",
        )

        self.assertEqual(signin.get_json()["routed"], KIND_SIGNIN)
        self.assertEqual(payment.get_json()["routed"], KIND_PAYMENT)
        self.assertEqual(transaction.get_json()["routed"], KIND_TRANSACTION)

        # One row per channel for the same number, not one row overall.
        with app.app_context():
            self.assertEqual(OTPMessage.query.filter_by(phone=phone).count(), 3)

        signin_body = client.get(f"/api/signin/{phone}").get_json()
        self.assertEqual(signin_body["kind"], KIND_SIGNIN)
        self.assertEqual(signin_body["otp"], "123456")
        self.assertEqual(signin_body["sender"], "IVAC")
        self.assertEqual(signin_body["receiver"], phone)
        self.assertNotIn("trx", signin_body)

        payment_body = client.get(f"/api/payment/{phone}?token={token}").get_json()
        self.assertEqual(payment_body["kind"], KIND_PAYMENT)
        self.assertEqual(payment_body["otp"], "456789")
        self.assertEqual(payment_body["sender"], "bKash")
        self.assertNotIn("trx", payment_body)

        transaction_body = client.get(
            f"/api/transaction/{phone}?token={token}"
        ).get_json()
        self.assertEqual(transaction_body["kind"], KIND_TRANSACTION)
        self.assertEqual(transaction_body["trx"], "9HABCDEFG1")
        self.assertEqual(transaction_body["amount"], "500.00")
        self.assertNotIn("otp", transaction_body)

    def test_legacy_http_endpoint_keeps_its_shape_and_only_serves_signin(self):
        phone = "01712340002"
        client = app.test_client()
        self._post_message(client, phone, "Your IVAC verification code is 111222")
        self._post_message(client, phone, "Your bKash OTP is 999888.", sender="bKash")

        legacy = client.get(f"/api/messages/{phone}").get_json()

        self.assertEqual(sorted(legacy), ["last_updated", "message"])
        self.assertEqual(legacy["message"], "111222")

    def test_a_sender_number_change_does_not_reroute_the_signin_otp(self):
        phone = "01712340003"
        client = app.test_client()

        response = self._post_message(
            client,
            phone,
            "Your IVAC verification code is 1_2_3_4_5_6",
            sender="8801999999999",
        )

        self.assertEqual(response.get_json()["routed"], KIND_SIGNIN)
        body = client.get(f"/api/signin/{phone}").get_json()
        self.assertEqual(body["otp"], "123456")
        self.assertEqual(body["sender"], "IVAC")
        self.assertEqual(body["sender_number"], "8801999999999")

    def test_a_forwarder_payload_with_receiver_and_sender_numbers(self):
        client = app.test_client()
        _, token = register_device("forwarder-buyer", "01712340006")

        response = client.post(
            "/api/messages",
            json={
                "receiver": "8801712340006",
                "sender": "16216",
                "message": "Tk4,500.00 paid through e-Commerce gateway. "
                "TxnId: 7028086216. Don't share your PIN with anybody",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["routed"], KIND_TRANSACTION)

        body = client.get(
            f"/api/transaction/01712340006?token={token}"
        ).get_json()
        self.assertEqual(body["phone"], "01712340006")
        self.assertEqual(body["receiver"], "01712340006")
        self.assertEqual(body["sender"], "Rocket")
        self.assertEqual(body["sender_number"], "16216")
        self.assertEqual(body["trx"], "7028086216")
        self.assertEqual(body["amount"], "4500.00")

    def test_the_sender_address_may_arrive_as_sender_number(self):
        phone = "01712340007"
        client = app.test_client()
        _, token = register_device("sender-number-buyer", phone)

        response = client.post(
            "/api/messages",
            json={
                "receiver": phone,
                "sender_number": "16247",
                "message": "Your security code is 525926. Do not share it.",
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["routed"], KIND_PAYMENT)
        body = client.get(f"/api/payment/{phone}?token={token}").get_json()
        self.assertEqual(body["otp"], "525926")
        self.assertEqual(body["sender"], "bKash")

    def test_an_unrecognised_message_is_stored_but_routed_nowhere(self):
        phone = "01712340004"
        client = app.test_client()

        response = self._post_message(
            client,
            phone,
            "Get 50% cashback on your next purchase. Valid till 31-12-2026.",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["routed"], KIND_UNKNOWN)
        _, token = register_device("unknown-buyer", phone)
        self.assertEqual(client.get(f"/api/signin/{phone}").get_json(), {})
        for path in ("/api/payment", "/api/transaction"):
            self.assertEqual(
                client.get(f"{path}/{phone}?token={token}").get_json(), {}
            )

        with app.app_context():
            self.assertIsNotNone(
                OTPMessage.query.filter_by(phone=phone, kind=KIND_UNKNOWN).first()
            )

    def test_each_ws_endpoint_only_receives_its_own_channel(self):
        phone = "01712340005"
        owner_id, _ = register_device("channel-ws-owner", phone)
        signin_ws = FakeSocket()
        payment_ws = FakeSocket()
        legacy_ws = FakeSocket()
        other_phone_ws = FakeSocket()
        register_client(signin_ws, owner_id, [phone], channel=KIND_SIGNIN)
        register_client(payment_ws, owner_id, [phone], channel=KIND_PAYMENT)
        register_client(legacy_ws, 1, [phone], channel=KIND_SIGNIN, legacy=True)
        register_client(other_phone_ws, 2, ["01600000000"], channel=KIND_SIGNIN)

        _broadcast_otp(
            {
                "kind": KIND_PAYMENT,
                "otp": "111111",
                "phone": phone,
                "receiver": phone,
                "sender": "bKash",
                "sender_number": "bKash",
                "last_updated": "2026-10-29T10:00:00.000+06:00",
            }
        )

        self.assertEqual(signin_ws.sent, [])
        self.assertEqual(legacy_ws.sent, [])
        self.assertEqual(len(payment_ws.sent), 1)
        self.assertEqual(payment_ws.sent[0]["kind"], KIND_PAYMENT)

        _broadcast_otp(
            {
                "kind": KIND_SIGNIN,
                "otp": "222222",
                "phone": phone,
                "receiver": phone,
                "sender": "IVAC",
                "sender_number": "8801999999999",
                "last_updated": "2026-10-29T10:00:01.000+06:00",
            }
        )

        self.assertEqual(len(signin_ws.sent), 1)
        self.assertEqual(signin_ws.sent[0]["kind"], KIND_SIGNIN)
        self.assertEqual(signin_ws.sent[0]["receiver"], phone)
        # /ws/otp keeps the original three-key frame, so old clients are intact.
        self.assertEqual(
            legacy_ws.sent,
            [
                {
                    "otp": "222222",
                    "phone": phone,
                    "last_updated": "2026-10-29T10:00:01.000+06:00",
                }
            ],
        )
        self.assertEqual(other_phone_ws.sent, [])

    def test_poller_payloads_are_built_per_channel_from_stored_rows(self):
        phone = "01712340007"
        with app.app_context():
            db.session.add_all(
                [
                    # A row from before the split: no stored code, no sender.
                    OTPMessage(
                        phone=phone,
                        kind=KIND_SIGNIN,
                        otp_message="legacy IVAC code 445566",
                    ),
                    OTPMessage(
                        phone=phone,
                        kind=KIND_PAYMENT,
                        otp_message="bKash OTP 778899",
                        code="778899",
                        sender="bKash",
                    ),
                    OTPMessage(
                        phone=phone,
                        kind=KIND_TRANSACTION,
                        otp_message="TrxID 9HABCDEFG1",
                        code="9HABCDEFG1",
                        sender="bKash",
                        amount="500.00",
                    ),
                    OTPMessage(
                        phone=phone,
                        kind=KIND_UNKNOWN,
                        otp_message="promo 123456",
                    ),
                ]
            )
            db.session.commit()
            rows = {
                row.kind: row for row in OTPMessage.query.filter_by(phone=phone).all()
            }

            signin = _otp_row_payload(rows[KIND_SIGNIN])
            self.assertEqual(signin["otp"], "445566")
            self.assertEqual(signin["sender"], "IVAC")

            payment = _otp_row_payload(rows[KIND_PAYMENT])
            self.assertEqual(payment["otp"], "778899")
            self.assertEqual(payment["sender"], "bKash")

            transaction = _otp_row_payload(rows[KIND_TRANSACTION])
            self.assertEqual(transaction["trx"], "9HABCDEFG1")
            self.assertEqual(transaction["amount"], "500.00")
            self.assertNotIn("otp", transaction)

            # Unknown rows are stored for inspection, never delivered.
            self.assertIsNone(_otp_row_payload(rows[KIND_UNKNOWN]))

    def test_transaction_history_keeps_every_trx_and_ignores_a_repeat(self):
        phone = "01712340006"
        client = app.test_client()
        _, token = register_device("history-buyer", phone)
        first = "You have received Tk 500.00 from 01711111111. TrxID 9HABCDEFG1."
        second = "Payment successful. Amount: BDT 250. TxnID: 7X9ABCDEF2"

        self._post_message(client, phone, first, sender="bKash")
        # A forwarder retry of the same SMS must not double-record the payment.
        self._post_message(client, phone, first, sender="bKash")
        self._post_message(client, phone, second, sender="Nagad")

        history = client.get(
            f"/api/transaction/{phone}/history?token={token}"
        ).get_json()
        self.assertEqual(
            [item["trx"] for item in history["transactions"]],
            ["7X9ABCDEF2", "9HABCDEFG1"],
        )
        self.assertEqual(history["transactions"][1]["amount"], "500.00")
        self.assertEqual(history["transactions"][0]["sender"], "Nagad")

        limited = client.get(
            f"/api/transaction/{phone}/history?limit=1&token={token}"
        ).get_json()
        self.assertEqual(
            [item["trx"] for item in limited["transactions"]], ["7X9ABCDEF2"]
        )
        # A number nobody claimed is not readable at all.
        self.assertEqual(
            client.get(
                f"/api/transaction/01700000000/history?token={token}"
            ).status_code,
            403,
        )
        self.assertEqual(
            client.get(
                f"/api/transaction/{phone}/history?limit=abc&token={token}"
            ).status_code,
            400,
        )


class BlockingSocket:
    """A WebSocket that subscribes once and then sits idle until released."""

    def __init__(self, phones=("01712345678",)):
        self.incoming = [json.dumps({"action": "subscribe", "phones": list(phones)})]
        self.sent = []
        self.acked = threading.Event()
        self.released = threading.Event()

    def receive(self):
        if self.incoming:
            return self.incoming.pop(0)
        self.released.wait(10)
        return None

    def send(self, message):
        self.sent.append(json.loads(message))
        self.acked.set()

    def close(self, code=1000, reason=""):
        pass


class WebSocketPoolTests(unittest.TestCase):
    """An idle socket must not pin a database connection.

    A WebSocket handler's app context lives as long as the connection, so its
    session would keep one pooled connection checked out for the whole socket.
    With the default pool (5 + 10 overflow) that caps a worker at ~15 sockets
    however many threads it has, and the next socket waits `pool_timeout` and
    then fails to upgrade. The same pool also serves `POST /api/messages`, so an
    exhausted pool stalls ingest too.
    """

    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}

    def tearDown(self):
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_an_idle_socket_does_not_hold_a_database_connection(self):
        _, token = register_device("pool-user")
        ws = BlockingSocket()
        errors = []

        def serve():
            try:
                with app.test_request_context(f"/ws/otp/signin?token={token}"):
                    _serve_ws_channel(ws, KIND_SIGNIN)
            except Exception as exc:  # surfaced below, not swallowed
                errors.append(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            self.assertTrue(ws.acked.wait(10), "the socket was never acknowledged")
            with app.app_context():
                db.session.remove()
                self.assertEqual(
                    db.engine.pool.checkedout(),
                    0,
                    "an idle WebSocket is holding a pooled database connection",
                )
        finally:
            ws.released.set()
            thread.join(timeout=10)
        self.assertEqual(errors, [])


class PaymentNumberClaimTests(unittest.TestCase):
    """A user claims numbers before paying; their payments stay private."""

    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}

    def tearDown(self):
        with app.ws_clients_lock:
            app.ws_clients.clear()
            app._otp_broadcast_seen = {}
        with app.app_context():
            db.session.remove()
            db.drop_all()

    @staticmethod
    def _post_message(client, phone, message, sender=None):
        payload = {"phone": phone, "message": message}
        if sender is not None:
            payload["sender"] = sender
        return client.post("/api/messages", json=payload)

    @staticmethod
    def _numbers(client, token):
        return client.get(f"/api/payment/numbers?token={token}").get_json()["numbers"]

    def test_numbers_are_listed_with_their_counters(self):
        client = app.test_client()
        _, token = register_device("claim-buyer", ["01712340101", "01812340102"])

        self.assertEqual(
            self._numbers(client, token),
            [
                {"phone": "01712340101", "payment_count": 0},
                {"phone": "01812340102", "payment_count": 0},
            ],
        )

    def test_claiming_again_is_idempotent(self):
        client = app.test_client()
        _, token = register_device("idempotent-buyer", "01712340103")

        response = client.post(
            "/api/payment/numbers",
            json={"numbers": ["+880 1712-340103"]},
            query_string={"token": token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._numbers(client, token)), 1)

    def test_another_device_cannot_claim_a_number(self):
        client = app.test_client()
        register_device("first-owner", "01712340104")
        _, other_token = register_device("second-buyer")

        response = client.post(
            "/api/payment/numbers",
            json={"numbers": ["01712340104"]},
            query_string={"token": other_token},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["numbers"], ["01712340104"])
        self.assertEqual(self._numbers(client, other_token), [])

    def test_releasing_frees_the_number_and_resets_its_counter(self):
        client = app.test_client()
        _, first_token = register_device("releaser", "01712340105")
        _, second_token = register_device("next-owner")
        self._post_message(
            client,
            "01712340105",
            "Payment successful. Amount: BDT 250. TxnID: 7X9ABCDEF2",
            sender="bKash",
        )
        self.assertEqual(self._numbers(client, first_token)[0]["payment_count"], 1)

        released = client.delete(
            f"/api/payment/numbers/01712340105?token={first_token}"
        )
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.get_json(), {"numbers": []})

        claimed = client.post(
            "/api/payment/numbers",
            json={"numbers": ["01712340105"]},
            query_string={"token": second_token},
        )
        self.assertEqual(claimed.status_code, 200)
        self.assertEqual(self._numbers(client, second_token)[0]["payment_count"], 0)

    def test_only_the_owner_can_release(self):
        client = app.test_client()
        register_device("keep-owner", "01712340106")
        _, other_token = register_device("not-owner")

        response = client.delete(
            f"/api/payment/numbers/01712340106?token={other_token}"
        )

        self.assertEqual(response.status_code, 404)

    def test_claiming_requires_an_approved_device(self):
        client = app.test_client()

        self.assertEqual(client.get("/api/payment/numbers").status_code, 401)
        self.assertEqual(client.get("/api/payment/numbers?token=nope").status_code, 401)

        with app.app_context():
            db.session.add(
                DeviceUser(
                    name="pending",
                    mac_address="MAC-pending",
                    motherboard_serial="MB-pending",
                    machine_guid="GUID-pending",
                    bios_serial="BIOS-pending",
                    ws_token="token-pending",
                    is_active=False,
                )
            )
            db.session.commit()

        self.assertEqual(
            client.get("/api/payment/numbers?token=token-pending").status_code, 403
        )

    def test_payment_reads_require_the_owner(self):
        client = app.test_client()
        _, token = register_device("read-owner", "01712340107")
        _, other_token = register_device("read-other")
        self._post_message(
            client,
            "01712340107",
            "Your bKash OTP is 456789. Do not share.",
            sender="bKash",
        )

        for path in (
            "/api/payment/01712340107",
            "/api/transaction/01712340107",
            "/api/transaction/01712340107/history",
        ):
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 401)
                self.assertEqual(
                    client.get(f"{path}?token={other_token}").status_code, 403
                )

        body = client.get(f"/api/payment/01712340107?token={token}").get_json()
        self.assertEqual(body["otp"], "456789")

    def test_a_payment_is_never_pushed_to_another_device(self):
        phone = "01712340108"
        owner_id, _ = register_device("push-owner", phone)
        other_id, _ = register_device("push-other")
        owner_payment = FakeSocket()
        owner_transaction = FakeSocket()
        other_payment = FakeSocket()
        other_transaction = FakeSocket()
        register_client(owner_payment, owner_id, [phone], channel=KIND_PAYMENT)
        register_client(
            owner_transaction, owner_id, [phone], channel=KIND_TRANSACTION
        )
        register_client(other_payment, other_id, [phone], channel=KIND_PAYMENT)
        register_client(
            other_transaction, other_id, [phone], channel=KIND_TRANSACTION
        )

        client = app.test_client()
        self._post_message(
            client, phone, "Your bKash OTP is 456789. Do not share.", sender="bKash"
        )

        self.assertEqual(len(owner_payment.sent), 1)
        self.assertEqual(owner_payment.sent[0]["otp"], "456789")
        self.assertEqual(other_payment.sent, [])

        self._post_message(
            client,
            phone,
            "Payment successful. Amount: BDT 250. TxnID: 7X9ABCDEF2",
            sender="bKash",
        )

        self.assertEqual(len(owner_transaction.sent), 1)
        self.assertEqual(owner_transaction.sent[0]["trx"], "7X9ABCDEF2")
        self.assertEqual(other_transaction.sent, [])

    def test_an_unclaimed_number_is_pushed_to_no_device(self):
        phone = "01712340109"
        user_id, _ = register_device("unclaimed-user")
        ws = FakeSocket()
        register_client(ws, user_id, [phone], channel=KIND_PAYMENT)

        self._post_message(
            app.test_client(),
            phone,
            "Your bKash OTP is 111222. Do not share.",
            sender="bKash",
        )

        self.assertEqual(ws.sent, [])

    def test_payment_count_increments_once_per_new_transaction(self):
        phone = "01712340110"
        user_id, token = register_device("counter-owner", phone)
        ws = FakeSocket()
        register_client(ws, user_id, [phone], channel=KIND_TRANSACTION)
        client = app.test_client()

        first = "You have received Tk 500.00 from 01711111111. TrxID 9HABCDEFG1."
        self._post_message(client, phone, first, sender="bKash")
        self.assertEqual(ws.sent[0]["payment_count"], 1)

        # A forwarder retry of the same SMS is the same payment.
        self._post_message(client, phone, first, sender="bKash")
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(self._numbers(client, token)[0]["payment_count"], 1)

        self._post_message(
            client,
            phone,
            "Payment successful. Amount: BDT 250. TxnID: 7X9ABCDEF2",
            sender="Nagad",
        )

        self.assertEqual(ws.sent[1]["payment_count"], 2)
        self.assertEqual(self._numbers(client, token)[0]["payment_count"], 2)

        latest = client.get(f"/api/transaction/{phone}?token={token}").get_json()
        self.assertEqual(latest["trx"], "7X9ABCDEF2")
        self.assertEqual(latest["payment_count"], 2)

    def test_the_owner_lookup_works_without_an_app_context(self):
        # The Redis subscriber thread calls _broadcast_otp with no app context,
        # so the scope check must be able to open one itself.
        owner_id, _ = register_device("owner-lookup", "01712340113")
        from app import _payment_number_owner

        self.assertEqual(_payment_number_owner("01712340113"), owner_id)
        self.assertIsNone(_payment_number_owner("01700000000"))

    def test_a_payment_otp_carries_the_count_before_that_payment(self):
        phone = "01712340111"
        user_id, _ = register_device("otp-count", phone)
        ws = FakeSocket()
        register_client(ws, user_id, [phone], channel=KIND_PAYMENT)
        client = app.test_client()

        self._post_message(
            client, phone, "Your bKash OTP is 333444. Do not share.", sender="bKash"
        )
        self.assertEqual(ws.sent[0]["payment_count"], 0)

        self._post_message(
            client,
            phone,
            "Payment successful. Amount: BDT 100. TxnID: AAAABBBB1",
            sender="bKash",
        )
        self._post_message(
            client, phone, "Your bKash OTP is 555666. Do not share.", sender="bKash"
        )
        self.assertEqual(ws.sent[1]["payment_count"], 1)

    def test_deleting_a_device_releases_its_numbers(self):
        client = app.test_client()
        owner_id, _ = register_device("deleted-owner", "01712340114")
        _, other_token = register_device("after-delete")
        blocked = client.post(
            "/api/payment/numbers",
            json={"numbers": ["01712340114"]},
            query_string={"token": other_token},
        )
        self.assertEqual(blocked.status_code, 409)

        with app.app_context():
            admin = AdminUser(username="admin")
            admin.set_password("secret")
            db.session.add(admin)
            db.session.commit()
        with client.session_transaction() as session:
            session["admin_id"] = 1
        self.assertEqual(client.post(f"/admin/delete/{owner_id}").status_code, 302)

        response = client.post(
            "/api/payment/numbers",
            json={"numbers": ["01712340114"]},
            query_string={"token": other_token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self._numbers(client, other_token)[0]["phone"], "01712340114"
        )
        self.assertEqual(
            client.get(
                "/api/payment/01712340114", query_string={"token": other_token}
            ).status_code,
            200,
        )

    def test_a_rotated_token_keeps_the_numbers_and_their_counter(self):
        client = app.test_client()
        owner_id, _ = register_device("rotate-owner", "01712340116")
        self._post_message(
            client,
            "01712340116",
            "Payment successful. Amount: BDT 250. TxnID: 8A1BCDEFG3",
            sender="Nagad",
        )

        with app.app_context():
            admin = AdminUser(username="admin")
            admin.set_password("secret")
            db.session.add(admin)
            db.session.commit()
        with client.session_transaction() as session:
            session["admin_id"] = 1
        self.assertEqual(
            client.post(f"/admin/user/{owner_id}/regen-token").status_code, 302
        )

        with app.app_context():
            rotated_token = db.session.get(DeviceUser, owner_id).ws_token
        self.assertNotEqual(rotated_token, f"token-rotate-owner")
        self.assertEqual(
            self._numbers(client, rotated_token),
            [{"phone": "01712340116", "payment_count": 1}],
        )

    def test_a_reissued_device_reclaims_its_numbers_by_fingerprint(self):
        client = app.test_client()
        phone = "01712340117"
        fingerprint = {
            "mac_address": "MAC-reclaim",
            "motherboard_serial": "MB-reclaim",
        }
        _, first_token = register_device("reclaim-before")
        self.assertEqual(
            client.post(
                "/api/payment/numbers",
                json={"numbers": [phone], **fingerprint},
                query_string={"token": first_token},
            ).status_code,
            200,
        )
        self._post_message(
            client,
            phone,
            "Payment successful. Amount: BDT 900. TxnID: 4Z2CDEFGH5",
            sender="bKash",
        )

        # The admin re-issued the same machine as a new row with a fresh token:
        # its old claim must not lock it out of its own number.
        with app.app_context():
            reissued = DeviceUser(
                name="reclaim-after",
                mac_address="MAC-reclaim",
                motherboard_serial="MB-reclaim",
                machine_guid="",
                bios_serial="",
                ws_token="token-reissued",
                is_active=True,
            )
            db.session.add(reissued)
            db.session.commit()

        reclaimed = client.post(
            "/api/payment/numbers",
            json={"numbers": [phone], **fingerprint},
            query_string={"token": "token-reissued"},
        )

        self.assertEqual(reclaimed.status_code, 200)
        self.assertEqual(reclaimed.get_json()["reclaimed"], [phone])
        self.assertEqual(
            reclaimed.get_json()["numbers"],
            [{"phone": phone, "payment_count": 1}],
        )
        self.assertEqual(self._numbers(client, first_token), [])
        self.assertEqual(
            client.get(
                f"/api/payment/{phone}", query_string={"token": first_token}
            ).status_code,
            403,
        )

    def test_a_number_claimed_without_a_fingerprint_cannot_be_taken_elsewhere(self):
        client = app.test_client()
        phone = "01712340118"
        _, first_token = register_device("no-fingerprint-owner", phone)

        _, other_token = register_device("figment")
        attempt = client.post(
            "/api/payment/numbers",
            json={
                "numbers": [phone],
                "mac_address": "MAC-figment",
                "motherboard_serial": "MB-figment",
            },
            query_string={"token": other_token},
        )

        # Nothing was bound to the claim, so no fingerprint can be proven and
        # the number stays with its owner until it is released.
        self.assertEqual(attempt.status_code, 409)
        self.assertEqual(attempt.get_json()["numbers"], [phone])
        self.assertEqual(self._numbers(client, first_token)[0]["phone"], phone)

        # The owner itself can bind a fingerprint afterwards.
        bound = client.post(
            "/api/payment/numbers",
            json={
                "numbers": [phone],
                "mac_address": "MAC-no-fingerprint-owner",
                "motherboard_serial": "MB-no-fingerprint-owner",
            },
            query_string={"token": first_token},
        )
        self.assertEqual(bound.status_code, 200)
        self.assertNotIn("reclaimed", bound.get_json())

    def test_wrong_fingerprint_cannot_reclaim(self):
        client = app.test_client()
        phone = "01712340119"
        _, owner_token = register_device("fp-owner")
        client.post(
            "/api/payment/numbers",
            json={
                "numbers": [phone],
                "mac_address": "MAC-fp-owner",
                "motherboard_serial": "MB-fp-owner",
            },
            query_string={"token": owner_token},
        )
        _, stranger_token = register_device("fp-stranger")

        attempt = client.post(
            "/api/payment/numbers",
            json={
                "numbers": [phone],
                "mac_address": "MAC-fp-stranger",
                "motherboard_serial": "MB-fp-stranger",
            },
            query_string={"token": stranger_token},
        )

        self.assertEqual(attempt.status_code, 409)
        self.assertEqual(self._numbers(client, owner_token)[0]["phone"], phone)

    def test_the_signin_channel_is_still_unscoped(self):
        phone = "01712340112"
        user_id, _ = register_device("signin-user")
        ws = FakeSocket()
        register_client(ws, user_id, [phone], channel=KIND_SIGNIN)
        client = app.test_client()

        self._post_message(client, phone, "Your IVAC verification code is 778899")

        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0]["otp"], "778899")
        self.assertEqual(
            client.get(f"/api/signin/{phone}").get_json()["otp"], "778899"
        )


def tearDownModule():
    try:
        os.unlink(_db_file.name)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    unittest.main()
