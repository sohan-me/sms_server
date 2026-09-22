import json
import multiprocessing
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request


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

        self.database = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.database.close()
        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL": f"sqlite:///{self.database.name}",
                "WS_AUTH_TOKEN": self.token,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

        self.server = subprocess.Popen(
            [
                os.path.join(os.path.dirname(__file__), "venv", "bin", "python"),
                "-c",
                (
                    "from app import app; "
                    f"app.run(host='127.0.0.1', port={self.port}, "
                    "debug=False, use_reloader=False, threaded=True)"
                ),
            ],
            cwd=os.path.dirname(__file__),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
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
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/messages",
            data=json.dumps({"phone": phone, "message": message}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status != 201:
                raise AssertionError(f"Unexpected POST status: {response.status}")


if __name__ == "__main__":
    unittest.main()
