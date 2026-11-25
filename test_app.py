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
    _extract_otp_for_pub,
    _format_bdt,
    _parse_subscription,
    _serve_ws_connection,
    _unregister_ws,
    app,
)
from models import DevicePing, DeviceUser, OTPMessage, db  # noqa: E402


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
        self.assertEqual(
            sorted(payload), ["last_updated", "message", "message_pub", "revision"]
        )
        self.assertEqual(payload["message"], "111111")
        self.assertIn(payload["message_pub"], ("VFS", "IVAC"))
        self.assertTrue(payload["last_updated"].endswith("+06:00"))

    def test_ingesting_a_newer_otp_overwrites_the_number_s_row(self):
        client = app.test_client()

        for code in ("111111", "222222"):
            response = client.post(
                "/api/messages",
                json={"phone": "01712345679", "message": f"code {code}"},
            )
            self.assertEqual(response.status_code, 201)

        with app.app_context():
            rows = OTPMessage.query.filter_by(phone="01712345679").all()

        # No new object per OTP: the second arrival updated the row.
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].otp_message, "code 222222")

        payload = client.get("/api/messages/01712345679").get_json()
        self.assertEqual(payload["message"], "222222")

    def test_snake_case_message_pub_wins_over_camel_case(self):
        client = app.test_client()

        response = client.post(
            "/api/messages",
            json={
                "phone": "01712345681",
                "message": "Your code is 445566",
                "message_pub": "VFS",
                "messagePub": "IVAC",
            },
        )
        self.assertEqual(response.status_code, 201)

        payload = client.get("/api/messages/01712345681").get_json()
        self.assertEqual(payload["message_pub"], "VFS")
        self.assertEqual(payload["message"], "445566")

    def test_get_exposes_a_monotonic_revision(self):
        client = app.test_client()
        client.post(
            "/api/messages",
            json={"phone": "01712345690", "message": "code 111111"},
        )
        first = client.get("/api/messages/01712345690").get_json()
        self.assertIn("revision", first)
        rev1 = first["revision"]

        client.post(
            "/api/messages",
            json={"phone": "01712345690", "message": "code 222222"},
        )
        second = client.get("/api/messages/01712345690").get_json()
        self.assertGreater(second["revision"], rev1)
        self.assertEqual(second["message"], "222222")

    def test_revision_query_hides_an_already_seen_otp(self):
        client = app.test_client()
        client.post(
            "/api/messages",
            json={"phone": "01712345691", "message": "code 333333"},
        )
        seen = client.get("/api/messages/01712345691").get_json()
        rev = seen["revision"]

        # Polling with the revision the caller already consumed returns nothing.
        again = client.get(f"/api/messages/01712345691?revision={rev}").get_json()
        self.assertEqual(again, {})

        # A newer arrival is visible past that cursor.
        client.post(
            "/api/messages",
            json={"phone": "01712345691", "message": "code 444444"},
        )
        newer = client.get(f"/api/messages/01712345691?revision={rev}").get_json()
        self.assertEqual(newer["message"], "444444")

    def test_message_pub_query_filters_when_given(self):
        client = app.test_client()
        client.post(
            "/api/messages",
            json={
                "phone": "01712345692",
                "message": "Your IVAC OTP is 5-5-5-1-1-1",
                "message_pub": "IVAC",
            },
        )
        # Asking for the publisher that is stored returns it.
        match = client.get("/api/messages/01712345692?message_pub=IVAC").get_json()
        self.assertEqual(match["message_pub"], "IVAC")
        # Asking for the other publisher returns nothing rather than the wrong code.
        mismatch = client.get("/api/messages/01712345692?message_pub=VFS").get_json()
        self.assertEqual(mismatch, {})

    def test_list_messages_discloses_recent_otp_phones(self):
        """The fan-out list endpoint must expose which phones hold a fresh row,
        so a bot can discover the number the OTP actually landed on."""
        client = app.test_client()
        client.post(
            "/api/messages",
            json={"phone": "01712345693", "message": "code 555666", "message_pub": "VFS"},
        )
        client.post(
            "/api/messages",
            json={"phone": "01712345694", "message": "code 777888", "message_pub": "IVAC"},
        )

        all_rows = client.get("/api/messages").get_json()["messages"]
        phones = {r["phone"] for r in all_rows}
        self.assertIn("01712345693", phones)
        self.assertIn("01712345694", phones)
        # Each entry carries the fields the fan-out needs.
        for r in all_rows:
            if r["phone"] == "01712345693":
                self.assertEqual(r["message_pub"], "VFS")
                self.assertIn("revision", r)
                self.assertIn("last_updated", r)

        # Publisher filter narrows the list to just VFS rows.
        vfs = client.get("/api/messages?message_pub=VFS").get_json()["messages"]
        self.assertEqual({r["phone"] for r in vfs}, {"01712345693"})

    def test_camel_case_message_pub_still_accepted_for_backward_compat(self):
        client = app.test_client()

        response = client.post(
            "/api/messages",
            json={
                "phone": "01712345682",
                "message": "Your code is 778899",
                "messagePub": "IVAC",
            },
        )
        self.assertEqual(response.status_code, 201)

        payload = client.get("/api/messages/01712345682").get_json()
        self.assertEqual(payload["message_pub"], "IVAC")
        self.assertEqual(payload["message"], "778899")

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

    def test_device_ping_endpoint_creates_and_updates_record(self):
        with app.test_client() as client:
            resp = client.post(
                "/api/ping",
                json={
                    "phone": "01712345678",
                    "slot": 0,
                    "interval_minutes": 15,
                    "status": "online",
                },
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data.get("success"))
            self.assertEqual(data["ping"]["phone"], "01712345678")
            self.assertEqual(data["ping"]["slot"], 0)
            self.assertEqual(data["ping"]["interval_minutes"], 15)
            self.assertEqual(data["ping"]["status"], "online")

            # Update ping with new interval or status
            resp2 = client.post(
                "/api/ping",
                json={
                    "phone": "01712345678",
                    "slot": 0,
                    "interval_minutes": 30,
                    "status": "online",
                },
            )
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.get_json()
            self.assertEqual(data2["ping"]["interval_minutes"], 30)

            # Query list
            list_resp = client.get("/api/ping")
            self.assertEqual(list_resp.status_code, 200)
            pings = list_resp.get_json().get("pings")
            self.assertTrue(any(p["phone"] == "01712345678" for p in pings))

            # Query single
            get_resp = client.get("/api/ping/01712345678")
            self.assertEqual(get_resp.status_code, 200)
            self.assertEqual(get_resp.get_json()["ping"]["phone"], "01712345678")

    def test_ping_via_api_messages_does_not_corrupt_otp_message(self):
        with app.test_client() as client:
            # 1. Post a legitimate OTP
            otp_resp = client.post(
                "/api/messages",
                json={
                    "phone": "01811223344",
                    "message": "Your VFS code is 654321",
                    "message_pub": "VFS",
                },
            )
            self.assertEqual(otp_resp.status_code, 201)

            # Verify OTP is stored
            chk = client.get("/api/messages/01811223344?message_pub=VFS")
            self.assertEqual(chk.status_code, 200)
            self.assertEqual(chk.get_json().get("message"), "654321")

            # 2. Forwarder app sends a PING to /api/messages
            ping_resp = client.post(
                "/api/messages",
                json={
                    "phone": "01811223344",
                    "message": "PING | phone=01811223344 | SIM 1 | every=15m | status=online",
                    "message_pub": "PING",
                },
            )
            self.assertEqual(ping_resp.status_code, 200)

            # 3. Verify OTP was NOT overwritten!
            chk2 = client.get("/api/messages/01811223344?message_pub=VFS")
            self.assertEqual(chk2.status_code, 200)
            self.assertEqual(chk2.get_json().get("message"), "654321")

            # 4. Verify the ping was recorded in DevicePing
            ping_record = client.get("/api/ping/01811223344")
            self.assertEqual(ping_record.status_code, 200)
            self.assertEqual(ping_record.get_json()["ping"]["phone"], "01811223344")

    def test_root_console_renders_html_for_browsers(self):
        with app.test_client() as client:
            resp = client.get("/", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 Chrome/120.0"})
            self.assertEqual(resp.status_code, 200)
            self.assertIn("SMS SERVER // CLI CONSOLE", resp.get_data(as_text=True))
            self.assertIn("sms-cli&gt;", resp.get_data(as_text=True))

    def test_root_console_returns_plain_text_for_curl(self):
        with app.test_client() as client:
            resp = client.get("/", headers={"User-Agent": "curl/8.5.0"})
            self.assertEqual(resp.status_code, 200)
            text = resp.get_data(as_text=True)
            self.assertIn("=== SMS Server CLI Console ===", text)
            self.assertIn("Endpoints:", text)
            self.assertIn("python cli.py", text)

    def test_root_console_returns_json_endpoints(self):
        with app.test_client() as client:
            resp = client.get("/", headers={"Accept": "application/json"})
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data.get("service"), "sms_server")
            self.assertIn("endpoints", data)
            self.assertIn("pings", data["endpoints"])

    def test_direct_phone_url_retrieves_otp(self):
        with app.test_client() as client:
            client.post(
                "/api/messages",
                json={
                    "phone": "01799887766",
                    "message": "Your IVAC OTP is 987654",
                    "message_pub": "IVAC",
                },
            )
            # 1. Direct phone lookup via JSON
            json_resp = client.get("/01799887766", headers={"Accept": "application/json"})
            self.assertEqual(json_resp.status_code, 200)
            self.assertEqual(json_resp.get_json().get("message"), "987654")

            # 2. Direct phone lookup via curl (plain text digits)
            curl_resp = client.get("/01799887766", headers={"User-Agent": "curl/7.88.1"})
            self.assertEqual(curl_resp.status_code, 200)
            self.assertEqual(curl_resp.get_data(as_text=True).strip(), "987654")

            # 3. Invalid phone returns 404
            err_resp = client.get("/not-a-phone")
            self.assertEqual(err_resp.status_code, 404)

    def test_pub_query_parameter_filter_supports_both_pub_and_message_pub(self):
        with app.test_client() as client:
            client.post(
                "/api/messages",
                json={
                    "phone": "01755443322",
                    "message": "Your VFS registration code is 334455",
                    "message_pub": "VFS",
                },
            )
            # Test ?pub=vfs
            r1 = client.get("/api/messages/01755443322?pub=vfs")
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(r1.get_json()["message"], "334455")
            self.assertEqual(r1.get_json()["message_pub"], "VFS")

            # Test ?pub=VFS
            r2 = client.get("/api/messages/01755443322?pub=VFS")
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.get_json()["message"], "334455")

            # Test mismatch filter: ?pub=ivac should return empty dict
            r3 = client.get("/api/messages/01755443322?pub=ivac")
            self.assertEqual(r3.status_code, 200)
            self.assertEqual(r3.get_json(), {})

            # Test /api/messages list with ?pub=vfs
            r4 = client.get("/api/messages?pub=vfs")
            self.assertEqual(r4.status_code, 200)
            messages = r4.get_json()["messages"]
            self.assertTrue(any(m["phone"] == "01755443322" for m in messages))

    def test_extract_otp_for_pub_handles_vfs_and_ivac_distinctly(self):
        # VFS must only match standalone 6-digit OTPs, never phone numbers in test strings
        self.assertEqual(
            _extract_otp_for_pub("Test from VFS SMS Relay app (SIM 1 / slot 0, phone=01712345678)", "VFS"),
            "",
        )
        self.assertEqual(
            _extract_otp_for_pub("881521 is the OTP for Login to your VFS Global application.", "VFS"),
            "881521",
        )
        # IVAC supports word-based OTPs
        self.assertEqual(
            _extract_otp_for_pub("Your OTP is Five-Six-Nine-One-Two-Five", "IVAC"),
            "569125",
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



def tearDownModule():
    try:
        os.unlink(_db_file.name)
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    unittest.main()
