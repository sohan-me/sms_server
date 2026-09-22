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


class WebSocketEndToEndTests(unittest.TestCase):
    def test_sms_posts_are_routed_to_separate_websocket_processes(self):
        token = "e2e-test-token"
        first_phone = "01712345678"
        second_phone = "01612345678"

        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]

        database = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        database.close()
        env = os.environ.copy()
        env.update(
            {
                "DATABASE_URL": f"sqlite:///{database.name}",
                "WS_AUTH_TOKEN": token,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

        server = subprocess.Popen(
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
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        clients = []
        try:
            self._wait_for_server(port, server)

            context = multiprocessing.get_context("spawn")
            result_queue = context.Queue()
            ws_url = f"ws://127.0.0.1:{port}/ws/otp?token={token}"
            for phone in (first_phone, second_phone):
                client = context.Process(
                    target=_ws_receiver,
                    args=(ws_url, phone, result_queue),
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
                port,
                first_phone,
                "Your IVAC verification code is 111222. Do not share it.",
            )
            self._post_sms(
                port,
                second_phone,
                "Your IVAC verification code is 333444. Do not share it.",
            )
            self._post_sms(
                port,
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
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
            if server.stdout:
                server.stdout.close()
            os.unlink(database.name)

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
