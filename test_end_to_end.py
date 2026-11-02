"""One complete journey through the SMS server, A to Z.

Nothing here is stubbed: a real Flask server process, a real Redis, real HTTP
requests, a real admin-panel login, and real WebSocket connections over TCP.
The test walks the whole product in order:

  server up -> register -> pending socket refused -> admin approves ->
  authenticate -> connect the sign-in and legacy sockets -> forward a real
  IVAC SMS -> receive the frame on both -> overwrite it with a newer OTP ->
  drop an unrecognised message -> HTTP reads -> removed payment endpoints ->
  token rotation -> restart with state intact.

The SMS body is the real IVAC template the parser must handle, posted in the
forwarder's real shape (`receiver` and `sender` numbers), so a regression in
routing or delivery fails here first.
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

SIGNIN_OTP = "Your IVAC verification code is 601781. Do not share it with anyone."
SIGNIN_OTP_2 = "Your IVAC verification code is 883322. Do not share it with anyone."
UNRECOGNISED_SMS = "Get 50% cashback on your next purchase. Valid till 31-12-2026."


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

    def assert_status(self, path, expect, method="GET", payload=None):
        """Assert a status without parsing a body (a 404 page is not JSON)."""
        body = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        status, raw = self._open(request)
        if status != expect:
            raise AssertionError(
                f"{method} {path} expected {expect}, got {status}: {raw[:400]!r}"
            )

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
        phone = "01710000001"
        other_phone = "01710000002"

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
        self.assertNotIn("ws_clients_by_channel", health)

        # ── B. both devices register, pending approval ────────
        alice_id = server.post_json("/api/register", alice, expect=201)["user_id"]
        bob_id = server.post_json("/api/register", bob, expect=201)["user_id"]
        self.assertNotEqual(alice_id, bob_id)
        # The same MAC cannot register a second licence.
        server.post_json("/api/register", alice, expect=409)

        # ── C. no socket for a pending device, an unknown token
        #       or no token at all ──────────────────────────────
        server.assert_socket_refused("/ws/otp/signin", server.device_token(alice_id))
        server.assert_socket_refused("/ws/otp/signin")
        server.assert_socket_refused("/ws/otp/signin", "not-a-real-token")

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

        # ── F. connect the sign-in sockets ────────────────────
        signin_ws = server.connect("/ws/otp/signin", phone, alice_token)
        legacy_ws = server.connect("/ws/otp", phone, alice_token)
        bob_ws = server.connect("/ws/otp/signin", other_phone, bob_token)
        sockets = [signin_ws, legacy_ws, bob_ws]
        try:
            # ── G. a real IVAC sign-in SMS ────────────────────
            server.forward(phone, SIGNIN_OTP, sender="8801700000000")
            frame = server.expect_frame(signin_ws, "signin frame")
            self.assertEqual(
                sorted(frame),
                ["kind", "last_updated", "otp", "phone", "receiver", "sender",
                 "sender_number"],
            )
            self.assertEqual(frame["kind"], "signin")
            self.assertEqual(frame["otp"], "601781")
            self.assertEqual(frame["phone"], phone)
            self.assertEqual(frame["receiver"], phone)
            self.assertEqual(frame["sender"], "IVAC")
            self.assertEqual(frame["sender_number"], "8801700000000")
            self.assertTrue(frame["last_updated"].endswith("+06:00"))

            legacy = server.expect_frame(legacy_ws, "legacy frame")
            self.assertEqual(sorted(legacy), ["last_updated", "otp", "phone"])
            self.assertEqual(legacy["otp"], "601781")

            # A socket subscribed to a different number stays silent.
            server.expect_silence(bob_ws, "bob's other-number socket")

            # ── H. a newer OTP overwrites the number's row ────
            server.forward(phone, SIGNIN_OTP_2, sender="8801700000000")
            frame = server.expect_frame(signin_ws, "second signin frame")
            self.assertEqual(frame["otp"], "883322")
            legacy = server.expect_frame(legacy_ws, "second legacy frame")
            self.assertEqual(legacy["otp"], "883322")

            # ── I. an unrecognised message is routed nowhere ──
            routed = server.forward(phone, UNRECOGNISED_SMS, sender="8801700000000")
            self.assertEqual(routed["routed"], "unknown")
            server.expect_silence(signin_ws, "signin socket (unrecognised sms)")
            self.assertEqual(server.get_json(f"/api/signin/{phone}")["otp"], "883322")

            # ── J. HTTP reads ─────────────────────────────────
            signin_body = server.get_json(f"/api/signin/{phone}")
            self.assertEqual(signin_body["otp"], "883322")
            self.assertEqual(signin_body["sender"], "IVAC")
            self.assertEqual(signin_body["receiver"], phone)

            legacy_body = server.get_json(f"/api/messages/{phone}")
            self.assertEqual(sorted(legacy_body), ["last_updated", "message"])
            self.assertEqual(legacy_body["message"], "883322")

            # ── K. the payment / transaction contract is gone ─
            for path in (
                "/api/payment/numbers",
                f"/api/payment/{phone}",
                f"/api/transaction/{phone}",
                f"/api/transaction/{phone}/history",
            ):
                server.assert_status(path, 404)
            server.assert_status(
                "/api/payment/numbers", 404, "POST", {"numbers": [phone]}
            )
            server.assert_status(f"/api/payment/numbers/{phone}", 404, "DELETE")
            server.assert_socket_refused("/ws/otp/payment", alice_token)
            server.assert_socket_refused("/ws/otp/transaction", alice_token)

            # ── L. the admin rotates Alice's token ────────────
            old_token = alice_token
            server.admin_action(f"/admin/user/{alice_id}/regen-token")
            alice_token = server.device_token(alice_id)
            self.assertNotEqual(alice_token, old_token)

            # The old token is dead on the socket, and the rotation hung up the
            # sockets it had opened.
            server.assert_socket_refused("/ws/otp/signin", old_token)
            server.assert_server_closed(signin_ws, "signin socket")
            server.assert_server_closed(legacy_ws, "legacy socket")

            # Sign-in reads keep their original open contract and survive it.
            self.assertEqual(server.get_json(f"/api/signin/{phone}")["otp"], "883322")

            rotated_ws = server.connect("/ws/otp/signin", phone, alice_token)
            sockets.append(rotated_ws)
            server.forward(phone, SIGNIN_OTP, sender="8801700000000")
            frame = server.expect_frame(rotated_ws, "signin after rotation")
            self.assertEqual(frame["otp"], "601781")

            # ── M. the stored OTP survives a server restart ───
            server.restart()
            self.assertEqual(server.get_json(f"/api/signin/{phone}")["otp"], "601781")
            self.assertEqual(
                server.get_json(f"/api/messages/{phone}")["message"], "601781"
            )
        finally:
            for ws in sockets:
                _close_socket(ws)


if __name__ == "__main__":
    unittest.main()
