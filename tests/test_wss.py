"""Real product routes + real shared host/relay; only the external provider is a fixture."""
import asyncio
import base64
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from my_mediabank.app import create_app
from my_mediabank.config import Settings


async def fixture_runner(*, session, reader, on_event):
    while line := await reader.readline():
        command = json.loads(line)
        if command["type"] == "start":
            on_event({"type": "ready"})
        elif command["type"] == "text":
            photo = re.search(r"photo_id=(photo_[a-f0-9]+)", command["text"])
            request = re.search(r"request_id=([A-Za-z0-9._:-]+)", command["text"])
            if photo and request:
                on_event({"type": "tool_call", "calls": [{"id": "call_" + request[1], "name": "record_photo_evaluation",
                    "args": {"photo_id": photo[1], "request_id": request[1].rstrip("."), "score": 6,
                             "description": "Тестовый цветной квадрат.", "reason": "Это технический тест, не оценка реальной модели."}}]})
        elif command["type"] == "tool_response":
            on_event({"type": "audio", "data": base64.b64encode(b"\x00\x00" * 400).decode(), "mime_type": "audio/pcm;rate=24000"})
            on_event({"type": "turn_complete"})
        elif command["type"] == "stop":
            return
        await asyncio.sleep(0)


class WssTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        path = Path(self.temporary.name)
        self.app = create_app(Settings(origin="https://testserver", state_dir=path / "state", static_dir=path / "web"),
                              managed_runner=fixture_runner)
        self.client = TestClient(self.app, base_url="https://testserver")
        self.client.__enter__()
        self.headers = {"Origin": "https://testserver"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temporary.cleanup()

    def login(self):
        code = self.app.state.access.invite()
        response = self.client.post("/api/login", json={"code": code}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("Secure", response.headers["set-cookie"])
        return code

    def test_unauthorized_and_foreign_origin_do_not_start_live(self):
        self.assertEqual(self.client.post("/api/live", json={}, headers=self.headers).status_code, 401)
        code = self.app.state.access.invite()
        response = self.client.post("/api/login", json={"code": code}, headers={"Origin": "https://elsewhere.invalid"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/login", json={"code": code}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post("/api/login", json={"code": code}, headers=self.headers).status_code, 401)

    def test_photo_wss_shared_live_relay_score_and_stop(self):
        self.login()
        image = io.BytesIO()
        Image.new("RGB", (320, 240), "green").save(image, "JPEG")
        with self.client.websocket_connect("wss://testserver/api/photos", headers=self.headers) as upload:
            upload.send_bytes(image.getvalue())
            photo = upload.receive_json()
        self.assertLessEqual(photo["width"], 1024)
        result = self.client.post("/api/live", json={"attempt_id": "test_attempt"}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        live = result.json()
        protocols = ["wl-live-v1", "wl-ticket." + live["socket_ticket"]]
        with self.client.websocket_connect("wss://testserver" + live["socket_url"], subprotocols=protocols, headers=self.headers) as socket:
            socket.send_json({"type": "hello", "protocol": "wl-live-v1", "attempt_id": "test_attempt",
                              "cursor": 0, "connection_generation": 1})
            self.assertEqual(socket.receive_json()["type"], "hello_ack")
            socket.send_json({"type": "input", "message": {"photo_id": photo["photo_id"], "request_id": "test_request"}})
            evaluation = None
            for _ in range(30):
                frame = socket.receive()
                if frame.get("text"):
                    event = json.loads(frame["text"]).get("event", {})
                    if event.get("type") == "photo_evaluation":
                        evaluation = event
                        break
            self.assertIsNotNone(evaluation)
            self.assertEqual(evaluation["score"], 6)
            self.assertEqual(evaluation["photo_id"], photo["photo_id"])
            socket.send_json({"type": "stop"})
        self.assertEqual(self.app.state.live.size(), 0)


if __name__ == "__main__":
    unittest.main()
