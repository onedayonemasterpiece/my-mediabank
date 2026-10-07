import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from live_interaction import LiveError

from my_mediabank.access import AccessStore
from my_mediabank.adapter import MediaBankAdapter
from my_mediabank.photos import MAX_PREVIEW, PhotoCache, PhotoError, normalize_image


def jpeg(size=(2400, 1200), orientation=None):
    image = Image.new("RGB", size, "#b77441")
    buffer = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[274] = orientation
        image.save(buffer, "JPEG", exif=exif)
    else:
        image.save(buffer, "JPEG")
    return buffer.getvalue()


class PhotoTests(unittest.TestCase):
    def test_oriented_bounded_preview_drops_exif(self):
        raw = jpeg(orientation=6)
        out, width, height = normalize_image(raw)
        self.assertEqual((width, height), (512, 1024))
        self.assertLessEqual(len(out), MAX_PREVIEW)
        with Image.open(io.BytesIO(out)) as image:
            self.assertEqual(dict(image.getexif()), {})
        self.assertEqual(raw, jpeg(orientation=6))

    def test_bad_data_and_large_input_rejected(self):
        for raw in (b"not a picture", b"a" * (2 * 1024 * 1024 + 1)):
            with self.assertRaises(PhotoError):
                normalize_image(raw)

    def test_cache_never_crosses_actor_or_grows_with_album(self):
        cache = PhotoCache()
        data, width, height = normalize_image(jpeg())
        first = cache.put("alice", data, width, height)
        with self.assertRaises(PhotoError):
            cache.get(first.id, "bob")
        for _ in range(1000):
            cache.put("alice", data, width, height)
        self.assertEqual(len(cache.items), 2)
        self.assertLessEqual(sum(len(x.data) for x in cache.items.values()), 2 * MAX_PREVIEW)


class AccessTests(unittest.TestCase):
    def test_invite_consumed_exactly_once_and_sessions_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AccessStore(Path(temporary) / "access.sqlite3")
            code = store.invite()
            with ThreadPoolExecutor(max_workers=2) as pool:
                tokens = list(pool.map(lambda _: store.login(code, ttl_seconds=60), (0, 1)))
            accepted = [token for token in tokens if token]
            self.assertEqual(len(accepted), 1)
            actor = store.authenticate(accepted[0])
            self.assertEqual(actor.subject, "owner")
            self.assertIsNone(store.authenticate("a" * 32))
            self.assertNotIn(accepted[0], Path(store.path).read_bytes().decode("latin1"))
            store.logout(accepted[0])
            self.assertIsNone(store.authenticate(accepted[0]))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.cache, self.writes, self.events = PhotoCache(), [], []
        self.adapter = MediaBankAdapter(photos=self.cache,
            write=lambda session, msg: self.writes.append(msg),
            emit=lambda session, msg: self.events.append(msg))
        initialized = self.adapter.initialize(resource_id="review:" + "a" * 24,
            actor={"subject": "owner", "tenant_id": "my-mediabank"}, model="gemini-3.8-live", owner_session="a" * 64)
        self.session = SimpleNamespace(id="live_test", state=initialized["state"], model="gemini-3.8-live")
        data, width, height = normalize_image(jpeg())
        self.photo = self.cache.put("a" * 64, data, width, height)

    def select(self, photo=None, request="request1"):
        self.adapter.input(self.session, {"photo_id": (photo or self.photo).id, "request_id": request})

    def call(self, **overrides):
        return {"name": "record_photo_evaluation", "args": {
            "photo_id": self.photo.id, "request_id": "request1", "score": 7,
            "description": "Одноцветный кадр.", "reason": "Тёплый цвет, но нет сюжета.", **overrides}}

    def test_retry_does_not_send_photo_or_trigger_turn_twice(self):
        self.select()
        self.select()
        self.assertEqual([x["type"] for x in self.writes], ["snapshot", "text"])
        self.assertFalse(self.writes[0]["optional"])
        self.assertTrue(self.events[-1]["duplicate"])

    def test_late_score_cannot_label_next_photo(self):
        self.select()
        next_photo = self.cache.put("a" * 64, self.photo.data, self.photo.width, self.photo.height)
        self.select(next_photo, "request2")
        with self.assertRaisesRegex(LiveError, "изменилась"):
            self.adapter.execute_tool(self.session, self.call())
        self.assertFalse(any(event["type"] == "photo_evaluation" for event in self.events))

    def test_score_bounds_and_stale_request(self):
        self.select()
        for value in (0, 11, True, 7.5, "7"):
            with self.assertRaises(LiveError):
                self.adapter.execute_tool(self.session, self.call(score=value))
        with self.assertRaises(LiveError):
            self.adapter.execute_tool(self.session, self.call(request_id="older"))
        result = self.adapter.execute_tool(self.session, self.call())
        self.assertEqual(result["score"], 7)
        self.assertEqual(self.events[-1]["photo_id"], self.photo.id)

    def test_resume_sends_current_image_without_replaying_analyze(self):
        self.select()
        self.writes.clear()
        self.adapter.on_resumed(self.session)
        self.assertEqual([x["type"] for x in self.writes], ["snapshot"])


if __name__ == "__main__":
    unittest.main()
