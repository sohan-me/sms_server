"""One complete journey through the SMS server, A to Z.

Nothing here is stubbed: a real Flask server process, a real Redis, real HTTP
requests, a real admin-panel login, and real WebSocket connections over TCP.
The test walks the whole product in order:

  server up -> register -> pending socket refused -> admin approves ->
  authenticate -> claim payment numbers -> connect every channel -> forward
  real provider SMS -> receive sign-in / payment / transaction frames ->
  counters -> history -> isolation between two users -> token rotation ->
  release and hand-over -> restart with state intact.

The SMS bodies are the real Bangladesh wallet templates the parser must handle,
posted in the forwarder's real shape (`receiver` and `sender` numbers), so a
regression in routing, counting or scoping fails here first.
"""

import http.cookiejar
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

import websocket


ADMIN_USERNAME = "incomplete"
ADMIN_PASSWORD = "journey-admin-password"

# Real sender shortcodes, as the forwarder reports them.
ROCKET_SENDER = "16216"
BKASH_SENDER = "16247"
NAGAD_SENDER = "16167"

SIGNIN_OTP = "Your IVAC verification code is 601781. Do not share it with anyone."
ROCKET_OTP = "Your security code for Rocket transaction is 525926."
ROCKET_TRX = (
    "Tk4,500.00 paid through e-Commerce gateway. NetBal: Tk112,601.25 "
    "TxnId: 7028086216 Date:08-OCT-26 01:31:57 pm. Don't share your PIN with anybody"
)
BKASH_OTP = (
    "Do NOT share your OTP or PIN with anyone. Your bKash OTP for PAYMENT of "
    "Tk.3,000.00 to IVAC (DGePay) is 319805. Expires in 2 min."
)
BKASH_TRX = (
    "Payment of Tk 3,000.00 to DGEPAY SERVICES LIMITED-RM62457 is successful. "
    "Balance Tk 11,620.91. TrxID DJM4DNBFXE at 22/10/2026 14:02"
)
BKASH_TRX_2 = (
    "Payment of Tk 1,500.00 to DGEPAY SERVICES LIMITED-RM62457 is successful. "
    "Balance Tk 10,120.91. TrxID A1B2C3D4E5 at 22/10/2026 14:20"
)
NAGAD_OTP = (
    "NEVER share your OTP or PIN with anyone. Nagad will never ask for these.\n"
    "Your OTP for Nagad ECOM payment is 035568.\n"
    "Validity: 2 minutes.\nHelpline: 16167"
)
NAGAD_TRX = (
    "Payment to 'DGePAY Services Limite' is Successful.\n"
    "Amount: Tk 6000.0\nTxnID: 75PRV4ZK\nBalance: Tk 31840.01\n23/07/2026 16:57"
)
NAGAD_TRX_2 = (
    "Payment to 'DGePAY Services Limite' is Successful.\n"
    "Amount: Tk 900.0\nTxnID: 91QW2ERT\nBalance: Tk 30940.01\n23/07/2026 17:31"
)


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _close_socket(ws):
    """Close a WebSocket, including one the server already hung up on."""
    try:
        ws.close()
    except Exception:
        pass
    try:
        ws.shutdown()
    except Exception:
        pass


class LiveServer:
    """A real server process, plus the HTTP / WebSocket client that drives it."""

    def __init__(self):
        self.ws_auth_token = "journey-server-wide-token"
        self.port = _free_port()
        self.redis_port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.ws_base = f"ws://127.0.0.1:{self.port}"
        database = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        database.close()
        self.database_path = database.name
        self.admin_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.redis = None
        self.server = None

    # ── lifecycle ──────────────────────────────────────────────

    def _server_env(self):
        env = os.environ.copy()
        # Other suites import app.py with these set in-process; the server this
        # test spawns must be a normal production-shaped worker, poller and all.
        env.pop("DISABLE_DB_OTP_POLLER", None)
        env.update(
            {
                "DATABASE_URL": f"sqlite:///{self.database_path}",
                "WS_AUTH_TOKEN": self.ws_auth_token,
                "REDIS_URL": f"redis://127.0.0.1:{self.redis_port}/0",
                "ADMIN_PASSWORD": ADMIN_PASSWORD,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        return env

    def _launch_server(self):
        self.server = subprocess.Popen(
            [
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python"
                ),
                "-c",
                (
                    "from app import app; "
                    f"app.run(host='127.0.0.1', port={self.port}, "
                    "debug=False, use_reloader=False, threaded=True)"
                ),
            ],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            env=self._server_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._wait_for_http()

    def start(self):
        self.redis = subprocess.Popen(
            [
                "redis-server",
                "--bind", "127.0.0.1",
                "--port", str(self.redis_port),
                "--save", "",
                "--appendonly", "no",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_for_port(self.redis_port, self.redis, "Redis")
        self._launch_server()
        return self

    def restart(self):
        """Restart the server on the same database: what must survive a crash."""
        self._stop_server()
        self._launch_server()

    def _stop_server(self):
        if self.server is None:
            return
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        if self.server.stdout:
            self.server.stdout.close()

    def stop(self):
        self._stop_server()
        if self.redis is not None:
            self.redis.terminate()
            try:
                self.redis.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.redis.kill()
                self.redis.wait(timeout=5)
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.database_path + suffix)
            except FileNotFoundError:
                pass

    # ── HTTP ───────────────────────────────────────────────────

    @staticmethod
    def _open(request, opener=None, timeout=10):
        try:
            if opener is None:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.status, response.read()
            with opener.open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def request_json(self, path, payload=None, method="GET", expect=200, token=None,
                     opener=None):
        body = None if payload is None else json.dumps(payload).encode()
        url = self.base + path
        if token is not None:
            url += ("&" if "?" in path else "?") + urllib.parse.urlencode({"token": token})
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method=method
        )
        status, raw = self._open(request, opener=opener)
        if status != expect:
            raise AssertionError(
                f"{method} {path} expected {expect}, got {status}: {raw[:400]!r}"
            )
        return json.loads(raw) if raw else {}

    def get_json(self, path, expect=200, token=None, opener=None):
        return self.request_json(path, None, "GET", expect, token, opener)

    def post_json(self, path, payload, expect=200, token=None):
        return self.request_json(path, payload, "POST", expect, token)

    def delete_json(self, path, expect=200, token=None):
        return self.request_json(path, None, "DELETE", expect, token)

    def forward(self, phone, message, sender, expect=201):
        """Post an SMS exactly as the forwarder does: receiver + sender numbers."""
        return self.request_json(
            "/api/messages",
            {"receiver": phone, "sender": sender, "message": message},
            "POST",
            expect,
        )

    # ── admin panel ────────────────────────────────────────────

    def admin_login(self):
        form = urllib.parse.urlencode(
            {"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        ).encode()
        request = urllib.request.Request(
            self.base + "/login",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        status, raw = self._open(request, opener=self.admin_opener)
        if status != 200:
            raise AssertionError(f"admin login failed: {status} {raw[:300]!r}")

    def admin_action(self, path):
        """POST an admin-panel action, following its redirect to the dashboard."""
        request = urllib.request.Request(self.base + path, data=b"", method="POST")
        status, raw = self._open(request, opener=self.admin_opener)
        if status != 200:
            raise AssertionError(f"admin action {path} failed: {status} {raw[:300]!r}")

    # ── database (only to read the token the API issues) ──────

    def device_token(self, user_id):
        with sqlite3.connect(self.database_path) as connection:
            row = connection.execute(
                "SELECT ws_token FROM device_user WHERE id = ?", (user_id,)
            ).fetchone()
        if row is None:
            raise AssertionError(f"device {user_id} missing")
        return row[0]

    # ── WebSocket ──────────────────────────────────────────────

    def connect(self, channel_path, phones, token):
        """Open a channel socket and subscribe it, returning after the ack."""
        if isinstance(phones, str):
            phones = [phones]
        ws = websocket.create_connection(
            f"{self.ws_base}{channel_path}?token={token}", timeout=5
        )
        ws.send(json.dumps({"action": "subscribe", "phones": phones}))
        ws.settimeout(5)
        acknowledgement = json.loads(ws.recv())
        expected = {"type": "subscribed", "phones": sorted(phones)}
        if acknowledgement != expected:
            raise AssertionError(f"subscribe ack {acknowledgement!r} != {expected!r}")
        return ws

    def assert_socket_refused(self, channel_path, token=None):
        """A socket that must not be served is closed before it carries a frame."""
        url = f"{self.ws_base}{channel_path}"
        if token is not None:
            url += "?token=" + urllib.parse.quote(token)
        try:
            ws = websocket.create_connection(url, timeout=5)
        except (websocket.WebSocketBadStatusException, ConnectionResetError, OSError):
            return
        try:
            ws.settimeout(3)
            raw = ws.recv()
        except (
            websocket.WebSocketConnectionClosedException,
            ConnectionResetError,
            OSError,
        ):
            return
        finally:
            _close_socket(ws)

        if not raw:
            return
        raise AssertionError(f"{channel_path}: served a socket that should be refused ({raw!r})")

    @staticmethod
    def expect_frame(ws, label="frame", timeout=5):
        ws.settimeout(timeout)
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            raise AssertionError(f"{label}: no frame arrived")
        if not raw:
            raise AssertionError(f"{label}: socket closed instead of a frame")
        return json.loads(raw)

    @staticmethod
    def assert_server_closed(ws, label="socket", timeout=3):
        """The server hung up (used for a revoked device's live sockets)."""
        ws.settimeout(timeout)
        try:
            raw = ws.recv()
        except (websocket.WebSocketConnectionClosedException, ConnectionResetError, OSError):
            return
        if not raw:
            return
        raise AssertionError(f"{label}: still open, received {raw!r}")

    @staticmethod
    def expect_silence(ws, label="socket", wait=0.9):
        ws.settimeout(wait)
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            return
        raise AssertionError(f"{label}: unexpected frame {raw!r}")

    # ── readiness ──────────────────────────────────────────────

    def _wait_for_http(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                output = self.server.stdout.read() if self.server.stdout else ""
                raise AssertionError(f"server exited before becoming ready:\n{output}")
            try:
                with urllib.request.urlopen(self.base + "/api/health", timeout=1) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError, ConnectionError):
                time.sleep(0.1)
        raise AssertionError("server did not become ready")

    @staticmethod
    def _wait_for_port(port, process, name):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"{name} exited before becoming ready")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return
            except OSError:
                time.sleep(0.1)
        raise AssertionError(f"{name} did not become ready")


class CompleteJourneyTests(unittest.TestCase):
    """Registration to rotated token, over real sockets against a real server."""

    @classmethod
    def setUpClass(cls):
        cls.server = LiveServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_a_to_z(self):
        server = self.server
        signin_phone = "01710000001"
        bkash_phone = "01710000002"
        nagad_phone = "01710000003"
        unclaimed_phone = "01710000099"

        alice = {
            "name": "Alice",
            "mac_address": "AA:BB:CC:00:00:01",
            "motherboard_serial": "MB-ALICE-1",
            "machine_guid": "11111111-aaaa-bbbb-cccc-000000000001",
            "bios_serial": "BIOS-ALICE-1",
        }
        bob = {
            "name": "Bob",
            "mac_address": "AA:BB:CC:00:00:02",
            "motherboard_serial": "MB-BOB-1",
            "machine_guid": "22222222-aaaa-bbbb-cccc-000000000002",
            "bios_serial": "BIOS-BOB-1",
        }

        # ── A. the server is up and fanning out through Redis ─
        health = server.get_json("/api/health")
        self.assertEqual(health["service"], "sms")
        self.assertTrue(health["redis_subscriber_alive"])
        self.assertTrue(health["db_poller_alive"])
        self.assertEqual(health["otp_fanout"], "redis")

        # ── B. both devices register, pending approval ────────
        alice_id = server.post_json("/api/register", alice, expect=201)["user_id"]
        bob_id = server.post_json("/api/register", bob, expect=201)["user_id"]
        self.assertNotEqual(alice_id, bob_id)
        # The same MAC cannot register a second licence.
        server.post_json("/api/register", alice, expect=409)

        # ── C. no socket for a pending device, an unknown token
        #       or no token at all ──────────────────────────────
        server.assert_socket_refused("/ws/otp/payment", server.device_token(alice_id))
        server.assert_socket_refused("/ws/otp/payment")
        server.assert_socket_refused("/ws/otp/payment", "not-a-real-token")

        # ── D. the admin approves both through the real panel ─
        server.admin_login()
        server.admin_action(f"/admin/toggle/{alice_id}")
        server.admin_action(f"/admin/toggle/{bob_id}")

        # ── E. authenticate: fingerprint in, device token out ─
        alice_token = server.post_json("/api/authenticate", alice)["ws_token"]
        bob_token = server.post_json("/api/authenticate", bob)["ws_token"]
        self.assertTrue(alice_token and bob_token)
        self.assertNotEqual(alice_token, bob_token)
        # A device that was never registered is not found.
        server.post_json(
            "/api/authenticate",
            {
                "name": "Nobody",
                "mac_address": "AA:BB:CC:00:00:99",
                "motherboard_serial": "MB-NOBODY",
                "machine_guid": "99999999-aaaa-bbbb-cccc-000000000099",
                "bios_serial": "BIOS-NOBODY",
            },
            expect=404,
        )

        # ── F. Alice claims the numbers she will pay from ─────
        claimed = server.post_json(
            "/api/payment/numbers",
            {
                "numbers": [bkash_phone, nagad_phone],
                "mac_address": alice["mac_address"],
                "motherboard_serial": alice["motherboard_serial"],
            },
            token=alice_token,
        )
        self.assertEqual(
            claimed["numbers"],
            [
                {"phone": bkash_phone, "payment_count": 0},
                {"phone": nagad_phone, "payment_count": 0},
            ],
        )
        # Claiming again is a no-op; Bob cannot take Alice's number.
        again = server.post_json(
            "/api/payment/numbers", {"numbers": [bkash_phone]}, token=alice_token
        )
        self.assertEqual(len(again["numbers"]), 2)
        self.assertNotIn("reclaimed", again)
        conflict = server.post_json(
            "/api/payment/numbers", {"numbers": [bkash_phone]}, expect=409, token=bob_token
        )
        self.assertEqual(conflict["numbers"], [bkash_phone])

        # ── G. connect every channel ──────────────────────────
        signin_ws = server.connect("/ws/otp/signin", [signin_phone, bkash_phone], alice_token)
        legacy_ws = server.connect("/ws/otp", [signin_phone, bkash_phone], alice_token)
        bkash_payment_ws = server.connect("/ws/otp/payment", bkash_phone, alice_token)
        bkash_trx_ws = server.connect("/ws/otp/transaction", bkash_phone, alice_token)
        nagad_payment_ws = server.connect("/ws/otp/payment", nagad_phone, alice_token)
        nagad_trx_ws = server.connect("/ws/otp/transaction", nagad_phone, alice_token)
        # Bob subscribes to Alice's numbers on purpose: scoping must ignore him.
        bob_payment_ws = server.connect(
            "/ws/otp/payment", [bkash_phone, nagad_phone], bob_token
        )
        bob_trx_ws = server.connect(
            "/ws/otp/transaction", [bkash_phone, nagad_phone], bob_token
        )
        sockets = [
            signin_ws, legacy_ws, bkash_payment_ws, bkash_trx_ws,
            nagad_payment_ws, nagad_trx_ws, bob_payment_ws, bob_trx_ws,
        ]
        try:
            # ── H. sign-in SMS: signin and legacy frames only ─
            server.forward(signin_phone, SIGNIN_OTP, sender="8801700000000")
            frame = server.expect_frame(signin_ws, "signin frame")
            self.assertEqual(
                sorted(frame),
                ["kind", "last_updated", "otp", "phone", "receiver", "sender",
                 "sender_number"],
            )
            self.assertEqual(frame["kind"], "signin")
            self.assertEqual(frame["otp"], "601781")
            self.assertEqual(frame["phone"], signin_phone)
            self.assertEqual(frame["receiver"], signin_phone)
            self.assertEqual(frame["sender"], "IVAC")
            self.assertEqual(frame["sender_number"], "8801700000000")
            self.assertTrue(frame["last_updated"].endswith("+06:00"))
            self.assertNotIn("trx", frame)
            self.assertNotIn("payment_count", frame)

            legacy = server.expect_frame(legacy_ws, "legacy frame")
            self.assertEqual(sorted(legacy), ["last_updated", "otp", "phone"])
            self.assertEqual(legacy["otp"], "601781")

            # A sign-in OTP never travels on the payment or transaction
            # socket, even with that same phone subscribed to them.
            server.expect_silence(bkash_payment_ws, "payment socket (signin sms)")
            server.expect_silence(bkash_trx_ws, "transaction socket (signin sms)")

            # ── I. bKash payment OTP for a claimed number ─────
            server.forward(bkash_phone, BKASH_OTP, sender=BKASH_SENDER)
            frame = server.expect_frame(bkash_payment_ws, "bKash payment OTP")
            self.assertEqual(
                sorted(frame),
                ["kind", "last_updated", "otp", "payment_count", "phone",
                 "receiver", "sender", "sender_number"],
            )
            self.assertEqual(frame["kind"], "payment")
            self.assertEqual(frame["otp"], "319805")
            self.assertEqual(frame["sender"], "bKash")
            self.assertEqual(frame["sender_number"], BKASH_SENDER)
            # The body names only the payee; the merchant must not reroute it,
            # and nothing has completed yet on this number.
            self.assertEqual(frame["payment_count"], 0)
            server.expect_silence(signin_ws, "signin socket (bKash payment OTP)")
            server.expect_silence(bkash_trx_ws, "transaction socket (payment OTP)")
            server.expect_silence(bob_payment_ws, "bob socket (alice payment OTP)")
            server.expect_silence(nagad_payment_ws, "nagad socket (bKash payment OTP)")

            # ── J. bKash transaction: payment 1 of this number ─
            server.forward(bkash_phone, BKASH_TRX, sender=BKASH_SENDER)
            frame = server.expect_frame(bkash_trx_ws, "bKash transaction")
            self.assertEqual(
                sorted(frame),
                ["amount", "kind", "last_updated", "payment_count", "phone",
                 "receiver", "sender", "sender_number", "trx"],
            )
            self.assertEqual(frame["kind"], "transaction")
            self.assertEqual(frame["trx"], "DJM4DNBFXE")
            self.assertEqual(frame["amount"], "3000.00")
            self.assertEqual(frame["sender"], "bKash")
            self.assertEqual(frame["payment_count"], 1)
            self.assertNotIn("otp", frame)
            server.expect_silence(bob_trx_ws, "bob socket (alice transaction)")

            # ── K. a second file paid with the same number ────
            server.forward(bkash_phone, BKASH_TRX_2, sender=BKASH_SENDER)
            frame = server.expect_frame(bkash_trx_ws, "bKash transaction 2")
            self.assertEqual(frame["trx"], "A1B2C3D4E5")
            self.assertEqual(frame["amount"], "1500.00")
            self.assertEqual(frame["payment_count"], 2)

            # ── L. a forwarder retry is not a new payment ─────
            retry = server.forward(bkash_phone, BKASH_TRX, sender=BKASH_SENDER)
            self.assertTrue(retry.get("duplicate"))
            server.expect_silence(bkash_trx_ws, "transaction socket (retried trx)")
            self.assertEqual(
                server.get_json("/api/payment/numbers", token=alice_token)["numbers"][0][
                    "payment_count"
                ],
                2,
            )

            # ── M. Nagad runs its own counter ─────────────────
            server.forward(nagad_phone, NAGAD_OTP, sender=NAGAD_SENDER)
            frame = server.expect_frame(nagad_payment_ws, "Nagad payment OTP")
            self.assertEqual(frame["otp"], "035568")
            self.assertEqual(frame["sender"], "Nagad")
            self.assertEqual(frame["payment_count"], 0)
            server.expect_silence(bkash_payment_ws, "bKash socket (Nagad OTP)")

            server.forward(nagad_phone, NAGAD_TRX, sender=NAGAD_SENDER)
            frame = server.expect_frame(nagad_trx_ws, "Nagad transaction")
            self.assertEqual(frame["trx"], "75PRV4ZK")
            self.assertEqual(frame["amount"], "6000.00")
            self.assertEqual(frame["sender"], "Nagad")
            self.assertEqual(frame["payment_count"], 1)

            # ── N. the brandless Rocket body still gets its label ─
            server.forward(unclaimed_phone, ROCKET_TRX, sender=ROCKET_SENDER)
            # Nobody claimed this number, so no socket receives it...
            server.expect_silence(bkash_trx_ws, "claimed socket (unclaimed number)")
            server.expect_silence(bob_trx_ws, "bob socket (unclaimed number)")
            # ...but it is stored and labelled from the sender shortcode.
            stored = server.get_json(
                f"/api/transaction/{unclaimed_phone}", token=server.ws_auth_token
            )
            self.assertEqual(stored["trx"], "7028086216")
            self.assertEqual(stored["sender"], "Rocket")
            self.assertEqual(stored["amount"], "4500.00")
            server.get_json(
                f"/api/transaction/{unclaimed_phone}", expect=403, token=alice_token
            )

            # ── O. authenticated HTTP reads ───────────────────
            latest_payment = server.get_json(
                f"/api/payment/{bkash_phone}", token=alice_token
            )
            self.assertEqual(latest_payment["otp"], "319805")
            self.assertEqual(latest_payment["sender"], "bKash")
            self.assertEqual(latest_payment["payment_count"], 0)

            history = server.get_json(
                f"/api/transaction/{bkash_phone}/history", token=alice_token
            )
            # Newest first, but two payments in the same second must not make
            # the assertion depend on timestamp resolution.
            self.assertEqual(
                {item["trx"]: item["amount"] for item in history["transactions"]},
                {"DJM4DNBFXE": "3000.00", "A1B2C3D4E5": "1500.00"},
            )
            self.assertEqual(
                server.get_json("/api/payment/numbers", token=alice_token)["numbers"],
                [
                    {"phone": bkash_phone, "payment_count": 2},
                    {"phone": nagad_phone, "payment_count": 1},
                ],
            )

            # ── P. other users and anonymous callers are refused ─
            server.get_json(f"/api/payment/{bkash_phone}", expect=401)
            server.get_json(f"/api/payment/{bkash_phone}", expect=403, token=bob_token)
            server.get_json(f"/api/transaction/{bkash_phone}", expect=403, token=bob_token)
            server.get_json(
                f"/api/transaction/{bkash_phone}/history", expect=403, token=bob_token
            )
            server.get_json("/api/payment/numbers", expect=401)
            self.assertEqual(
                server.get_json("/api/payment/numbers", token=bob_token)["numbers"], []
            )
            # Sign-in keeps its original open contract.
            self.assertEqual(
                server.get_json(f"/api/signin/{signin_phone}")["otp"], "601781"
            )
            self.assertEqual(
                server.get_json(f"/api/messages/{signin_phone}")["message"], "601781"
            )

            # ── Q. the admin rotates Alice's token ────────────
            old_token = alice_token
            server.admin_action(f"/admin/user/{alice_id}/regen-token")
            alice_token = server.device_token(alice_id)
            self.assertNotEqual(alice_token, old_token)

            # The old token is dead on both transports, and the rotation hung up
            # the sockets it had opened.
            server.assert_socket_refused("/ws/otp/payment", old_token)
            server.get_json(f"/api/payment/{bkash_phone}", expect=401, token=old_token)
            server.get_json("/api/payment/numbers", expect=401, token=old_token)
            for ws, label in (
                (signin_ws, "signin socket"),
                (legacy_ws, "legacy socket"),
                (bkash_payment_ws, "payment socket"),
                (bkash_trx_ws, "transaction socket"),
                (nagad_payment_ws, "nagad payment socket"),
                (nagad_trx_ws, "nagad transaction socket"),
            ):
                server.assert_server_closed(ws, label)

            # The numbers, and their counters, survive the rotation.
            self.assertEqual(
                server.get_json("/api/payment/numbers", token=alice_token)["numbers"],
                [
                    {"phone": bkash_phone, "payment_count": 2},
                    {"phone": nagad_phone, "payment_count": 1},
                ],
            )
            # A socket opened with the new token keeps counting where it was.
            rotated_payment_ws = server.connect(
                "/ws/otp/payment", bkash_phone, alice_token
            )
            sockets.append(rotated_payment_ws)
            server.forward(bkash_phone, BKASH_OTP, sender=BKASH_SENDER)
            frame = server.expect_frame(rotated_payment_ws, "payment after rotation")
            self.assertEqual(frame["otp"], "319805")
            self.assertEqual(frame["payment_count"], 2)

            # ── R. release a number and hand it to another device ─
            released = server.delete_json(
                f"/api/payment/numbers/{nagad_phone}", token=alice_token
            )
            self.assertEqual(
                released["numbers"], [{"phone": bkash_phone, "payment_count": 2}]
            )
            taken = server.post_json(
                "/api/payment/numbers",
                {
                    "numbers": [nagad_phone],
                    "mac_address": bob["mac_address"],
                    "motherboard_serial": bob["motherboard_serial"],
                },
                token=bob_token,
            )
            self.assertEqual(taken["numbers"], [{"phone": nagad_phone, "payment_count": 0}])

            # Ownership moved: the next Nagad payment reaches Bob, counted from
            # his own zero, and Alice's reads are refused.
            server.forward(nagad_phone, NAGAD_TRX_2, sender=NAGAD_SENDER)
            frame = server.expect_frame(bob_trx_ws, "bob Nagad transaction")
            self.assertEqual(frame["trx"], "91QW2ERT")
            self.assertEqual(frame["payment_count"], 1)
            server.get_json(f"/api/transaction/{nagad_phone}", expect=403, token=alice_token)
            self.assertEqual(
                server.get_json("/api/payment/numbers", token=bob_token)["numbers"],
                [{"phone": nagad_phone, "payment_count": 1}],
            )

            # ── S. everything survives a server restart ───────
            server.restart()
            self.assertEqual(
                server.get_json("/api/payment/numbers", token=alice_token)["numbers"],
                [{"phone": bkash_phone, "payment_count": 2}],
            )
            self.assertEqual(
                server.get_json("/api/payment/numbers", token=bob_token)["numbers"],
                [{"phone": nagad_phone, "payment_count": 1}],
            )
            self.assertEqual(
                server.get_json(f"/api/payment/{bkash_phone}", token=alice_token)["otp"],
                "319805",
            )
            self.assertEqual(
                {item["trx"] for item in server.get_json(
                    f"/api/transaction/{bkash_phone}/history", token=alice_token
                )["transactions"]},
                {"DJM4DNBFXE", "A1B2C3D4E5"},
            )
            self.assertEqual(
                len(server.get_json(
                    f"/api/transaction/{nagad_phone}/history", token=bob_token
                )["transactions"]),
                2,
            )
        finally:
            for ws in sockets:
                _close_socket(ws)


if __name__ == "__main__":
    unittest.main()
