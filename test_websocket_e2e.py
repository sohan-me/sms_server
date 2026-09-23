import json
import multiprocessing
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid


def _ws_receiver(url, phone, result_queue):
    import websocket

    try:
        ws = websocket.create_connection(url, timeout=5)
        ws.send(json.dumps({"action": "subscribe", "phones": [phone]}))
        acknowledgement = json.loads(ws.recv())
        result_queue.put(("ready", phone, acknowledgement))

        ws.settimeout(3)
        messages = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                messages.append(json.loads(ws.recv()))
            except websocket.WebSocketTimeoutException:
                break

        result_queue.put(("result", phone, messages))
        ws.close()
    except Exception as exc:
        result_queue.put(("error", phone, repr(exc)))


def _ws_updatable_receiver(
    url,
    initial_phone,
    replacement_phones,
    command_queue,
    result_queue,
):
    import websocket

    ws = None
    try:
        ws = websocket.create_connection(url, timeout=5)
        ws.send(json.dumps({"action": "subscribe", "phones": [initial_phone]}))
        result_queue.put(("initial_ack", json.loads(ws.recv())))

        result_queue.put(("initial_otp", json.loads(ws.recv())))
        command_queue.get(timeout=10)

        ws.send(
            json.dumps(
                {
                    "action": "subscribe",
                    "phones": replacement_phones,
                }
            )
        )
        result_queue.put(("update_ack", json.loads(ws.recv())))

        ws.settimeout(3)
        messages = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                messages.append(json.loads(ws.recv()))
            except websocket.WebSocketTimeoutException:
                break
        result_queue.put(("updated_messages", messages))
    except Exception as exc:
        result_queue.put(("error", repr(exc)))
    finally:
        if ws is not None:
            ws.close()


class WebSocketEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.token = "e2e-test-token"
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            self.port = port_socket.getsockname()[1]
        with socket.socket() as redis_port_socket:
            redis_port_socket.bind(("127.0.0.1", 0))
            self.redis_port = redis_port_socket.getsockname()[1]

        self.database = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.database.close()
        self.redis_server = subprocess.Popen(
            [
                "redis-server",
                "--bind",
                "127.0.0.1",
                "--port",
                str(self.redis_port),
                "--save",
                "",
                "--appendonly",
                "no",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_for_port(self.redis_port, self.redis_server)

        self.server_env = os.environ.copy()
        self.server_env.update(
            {
                "DATABASE_URL": f"sqlite:///{self.database.name}",
                "WS_AUTH_TOKEN": self.token,
                "REDIS_URL": f"redis://127.0.0.1:{self.redis_port}/0",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

        self.server = self._start_server(self.port)
        self._wait_for_server(self.port, self.server)
        self.ws_url = (
            f"ws://127.0.0.1:{self.port}/ws/otp?token={self.token}"
        )

    def tearDown(self):
        self.server.terminate()
        try:
            self.server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=5)
        if self.server.stdout:
            self.server.stdout.close()
        self.redis_server.terminate()
        try:
            self.redis_server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.redis_server.kill()
            self.redis_server.wait(timeout=5)
        os.unlink(self.database.name)

    def test_sms_posts_are_routed_to_separate_websocket_processes(self):
        first_phone = "01712345678"
        second_phone = "01612345678"
        clients = []
        try:
            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            for phone in (first_phone, second_phone):
                client = context.Process(
                    target=_ws_receiver,
                    args=(self.ws_url, phone, result_queue),
                )
                client.start()
                clients.append(client)

            ready = [result_queue.get(timeout=10) for _ in range(2)]
            self.assertEqual({item[0] for item in ready}, {"ready"})
            self.assertEqual(
                {item[1] for item in ready},
                {first_phone, second_phone},
            )
            for _, phone, acknowledgement in ready:
                self.assertEqual(
                    acknowledgement,
                    {"type": "subscribed", "phones": [phone]},
                )

            self._post_sms(
                self.port,
                first_phone,
                "Your IVAC verification code is 111222. Do not share it.",
            )
            self._post_sms(
                self.port,
                second_phone,
                "Your IVAC verification code is 333444. Do not share it.",
            )
            self._post_sms(
                self.port,
                "01512345678",
                "Your IVAC verification code is 999000. Do not share it.",
            )

            received = [result_queue.get(timeout=10) for _ in range(2)]
            self.assertEqual({item[0] for item in received}, {"result"})
            by_phone = {phone: messages for _, phone, messages in received}

            self.assertEqual(
                [(item["phone"], item["otp"]) for item in by_phone[first_phone]],
                [(first_phone, "111222")],
            )
            self.assertEqual(
                [(item["phone"], item["otp"]) for item in by_phone[second_phone]],
                [(second_phone, "333444")],
            )
        finally:
            for client in clients:
                client.join(timeout=2)
                if client.is_alive():
                    client.terminate()
                    client.join(timeout=2)

    def test_subscription_can_be_replaced_without_reconnecting(self):
        initial_phone = "01712345678"
        added_phone = "01612345678"
        other_added_phone = "01512345678"
        replacement_phones = [added_phone, other_added_phone]

        context = multiprocessing.get_context("spawn")
        command_queue = context.Queue()
        result_queue = context.Queue()
        client = context.Process(
            target=_ws_updatable_receiver,
            args=(
                self.ws_url,
                initial_phone,
                replacement_phones,
                command_queue,
                result_queue,
            ),
        )
        client.start()

        try:
            event, acknowledgement = result_queue.get(timeout=10)
            self.assertEqual(event, "initial_ack")
            self.assertEqual(
                acknowledgement,
                {"type": "subscribed", "phones": [initial_phone]},
            )

            self._post_sms(
                self.port,
                initial_phone,
                "Your IVAC verification code is 111222. Do not share it.",
            )
            event, initial_otp = result_queue.get(timeout=10)
            self.assertEqual(event, "initial_otp")
            self.assertEqual(initial_otp["phone"], initial_phone)
            self.assertEqual(initial_otp["otp"], "111222")

            command_queue.put("update")
            event, acknowledgement = result_queue.get(timeout=10)
            self.assertEqual(event, "update_ack")
            self.assertEqual(
                acknowledgement,
                {
                    "type": "subscribed",
                    "phones": sorted(replacement_phones),
                },
            )

            self._post_sms(
                self.port,
                initial_phone,
                "Your IVAC verification code is 222333. Do not share it.",
            )
            self._post_sms(
                self.port,
                added_phone,
                "Your IVAC verification code is 333444. Do not share it.",
            )
            self._post_sms(
                self.port,
                other_added_phone,
                "Your IVAC verification code is 444555. Do not share it.",
            )

            event, messages = result_queue.get(timeout=10)
            self.assertEqual(event, "updated_messages")
            self.assertEqual(
                {(item["phone"], item["otp"]) for item in messages},
                {
                    (added_phone, "333444"),
                    (other_added_phone, "444555"),
                },
            )
            self.assertNotIn(
                initial_phone,
                {item["phone"] for item in messages},
            )
        finally:
            client.join(timeout=2)
            if client.is_alive():
                client.terminate()
                client.join(timeout=2)

    def test_three_registered_users_receive_their_own_complete_otp_response(self):
        phones = ["01710000001", "01710000002", "01710000003"]
        otps = ["101201", "202302", "303403"]
        users = []

        for index in range(3):
            fingerprint = uuid.uuid4().hex
            device = {
                "name": f"E2E User {index + 1}",
                "mac_address": f"02:{fingerprint[:10]}",
                "motherboard_serial": f"MB-{uuid.uuid4().hex}",
                "machine_guid": str(uuid.uuid4()),
                "bios_serial": f"BIOS-{uuid.uuid4().hex}",
            }
            registration = self._post_json(
                self.port,
                "/api/register",
                device,
                expected_status=201,
            )
            users.append(
                {
                    "id": registration["user_id"],
                    "device": device,
                }
            )

        with sqlite3.connect(self.database.name) as connection:
            connection.executemany(
                "UPDATE device_user SET is_active = 1 WHERE id = ?",
                [(user["id"],) for user in users],
            )
            connection.commit()

        for user in users:
            authentication = self._post_json(
                self.port,
                "/api/authenticate",
                user["device"],
                expected_status=200,
            )
            user["token"] = authentication["ws_token"]

        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        clients = []
        try:
            for user, phone in zip(users, phones):
                ws_url = (
                    f"ws://127.0.0.1:{self.port}/ws/otp"
                    f"?token={user['token']}"
                )
                client = context.Process(
                    target=_ws_receiver,
                    args=(ws_url, phone, result_queue),
                )
                client.start()
                clients.append(client)

            ready = [result_queue.get(timeout=10) for _ in range(3)]
            self.assertEqual({item[0] for item in ready}, {"ready"})
            self.assertEqual({item[1] for item in ready}, set(phones))

            for phone, otp in zip(phones, otps):
                self._post_sms(
                    self.port,
                    phone,
                    f"Your IVAC verification code is {otp}. Do not share it.",
                )

            received = [result_queue.get(timeout=10) for _ in range(3)]
            self.assertEqual({item[0] for item in received}, {"result"})
            by_phone = {phone: messages for _, phone, messages in received}

            for phone, otp in zip(phones, otps):
                self.assertEqual(len(by_phone[phone]), 1)
                response = by_phone[phone][0]
                self.assertEqual(response["phone"], phone)
                self.assertEqual(response["otp"], otp)
                self.assertIsInstance(response["id"], int)
                self.assertFalse(response["used"])
                self.assertTrue(response["created_at"].endswith("+06:00"))
        finally:
            for client in clients:
                client.join(timeout=2)
                if client.is_alive():
                    client.terminate()
                    client.join(timeout=2)

    def test_redis_routes_otps_between_server_processes(self):
        first_phone = "01720000001"
        second_phone = "01720000002"
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            second_port = port_socket.getsockname()[1]

        second_server = self._start_server(second_port)
        clients = []
        try:
            self._wait_for_server(second_port, second_server)
            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            urls_and_phones = [
                (self.ws_url, first_phone),
                (
                    f"ws://127.0.0.1:{second_port}/ws/otp"
                    f"?token={self.token}",
                    second_phone,
                ),
            ]
            for ws_url, phone in urls_and_phones:
                client = context.Process(
                    target=_ws_receiver,
                    args=(ws_url, phone, result_queue),
                )
                client.start()
                clients.append(client)

            ready = [result_queue.get(timeout=10) for _ in range(2)]
            self.assertEqual({item[0] for item in ready}, {"ready"})

            self._post_sms(
                self.port,
                first_phone,
                "Your IVAC verification code is 515253. Do not share it.",
            )
            self._post_sms(
                self.port,
                second_phone,
                "Your IVAC verification code is 616263. Do not share it.",
            )

            received = [result_queue.get(timeout=10) for _ in range(2)]
            self.assertEqual({item[0] for item in received}, {"result"})
            by_phone = {phone: messages for _, phone, messages in received}
            self.assertEqual(
                [(item["phone"], item["otp"]) for item in by_phone[first_phone]],
                [(first_phone, "515253")],
            )
            self.assertEqual(
                [(item["phone"], item["otp"]) for item in by_phone[second_phone]],
                [(second_phone, "616263")],
            )
        finally:
            for client in clients:
                client.join(timeout=2)
                if client.is_alive():
                    client.terminate()
                    client.join(timeout=2)
            second_server.terminate()
            try:
                second_server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                second_server.kill()
                second_server.wait(timeout=5)
            if second_server.stdout:
                second_server.stdout.close()

    def _start_server(self, port):
        return subprocess.Popen(
            [
                os.path.join(os.path.dirname(__file__), "venv", "bin", "python"),
                "-c",
                (
                    "from app import app; "
                    f"app.run(host='127.0.0.1', port={port}, "
                    "debug=False, use_reloader=False, threaded=True)"
                ),
            ],
            cwd=os.path.dirname(__file__),
            env=self.server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    @staticmethod
    def _wait_for_port(port, process):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("Redis exited before becoming ready")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("Redis did not become ready")

    @staticmethod
    def _wait_for_server(port, server):
        url = f"http://127.0.0.1:{port}/api/health"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if server.poll() is not None:
                output = server.stdout.read() if server.stdout else ""
                raise AssertionError(f"Server exited early:\n{output}")
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.1)
        raise AssertionError("Server did not become ready")

    @staticmethod
    def _post_sms(port, phone, message):
        WebSocketEndToEndTests._post_json(
            port,
            "/api/messages",
            {"phone": phone, "message": message},
            expected_status=201,
        )

    @staticmethod
    def _post_json(port, path, payload, expected_status):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != expected_status:
                raise AssertionError(f"Unexpected POST status: {response.status}")
            return json.loads(response.read())


if __name__ == "__main__":
    unittest.main()
