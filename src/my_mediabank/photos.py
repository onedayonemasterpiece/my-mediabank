"""Bounded in-memory previews. The original never enters a persistent database."""
from __future__ import annotations

import hashlib
import io
import secrets
import time
import warnings
from collections import OrderedDict
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD = 2 * 1024 * 1024
MAX_PIXELS = 25_000_000
MAX_PREVIEW = 180 * 1024
MAX_EDGE = 1024


class PhotoError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def normalize_image(raw: bytes):
    if not raw or len(raw) > MAX_UPLOAD:
        raise PhotoError("PHOTO_SIZE", "Фото для просмотра должно быть не больше 2 МБ")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format not in {"JPEG", "PNG", "WEBP"}:
                    raise PhotoError("PHOTO_FORMAT", "Нужна фотография JPEG, PNG или WebP")
                if source.width * source.height > MAX_PIXELS or getattr(source, "n_frames", 1) != 1:
                    raise PhotoError("PHOTO_DIMENSIONS", "Изображение слишком большое или содержит анимацию")
                oriented = ImageOps.exif_transpose(source)
                oriented.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
                if "A" in oriented.getbands():
                    rgba = oriented.convert("RGBA")
                    clean = Image.new("RGB", rgba.size, "white")
                    clean.paste(rgba, mask=rgba.getchannel("A"))
                    rgba.close()
                else:
                    clean = oriented.convert("RGB")
                oriented.close()
                try:
                    for edge in (MAX_EDGE, 768, 512):
                        clean.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                        for quality in (82, 72, 62):
                            output = io.BytesIO()
                            # No EXIF/location/comment payload is copied to the model preview.
                            clean.save(output, "JPEG", quality=quality, optimize=True)
                            data = output.getvalue()
                            if len(data) <= MAX_PREVIEW:
                                return data, clean.width, clean.height
                finally:
                    clean.close()
    except PhotoError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise PhotoError("PHOTO_DECODE", "Не удалось прочитать фотографию") from None
    raise PhotoError("PHOTO_SIZE", "Не удалось подготовить небольшой просмотр фотографии")


@dataclass(frozen=True)
class Photo:
    id: str
    owner_session: str
    data: bytes
    width: int
    height: int
    created: float

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()

    def public(self):
        return {"photo_id": self.id, "width": self.width, "height": self.height,
                "sha256": self.sha256, "bytes": len(self.data)}


class PhotoCache:
    def __init__(self, *, capacity=4, ttl=900):
        self.capacity, self.ttl = capacity, ttl
        self.items: OrderedDict[str, Photo] = OrderedDict()

    def prune(self):
        now = time.monotonic()
        for key, photo in list(self.items.items()):
            if now - photo.created > self.ttl:
                self.items.pop(key, None)

    def put(self, owner_session, data, width, height):
        self.prune()
        # Keep at most two previews per browser login: current and replacement.
        previous = [key for key, photo in self.items.items() if photo.owner_session == owner_session]
        for key in previous[:-1]:
            self.items.pop(key, None)
        item = Photo("photo_" + secrets.token_hex(12), owner_session, data, width, height, time.monotonic())
        self.items[item.id] = item
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
        return item

    def get(self, photo_id, owner_session):
        self.prune()
        item = self.items.get(photo_id)
        if item is None or item.owner_session != owner_session:
            raise PhotoError("PHOTO_EXPIRED", "Фото больше не в памяти сервера. Отправьте его ещё раз")
        return item

    def clear_owner(self, owner_session):
        for key, photo in list(self.items.items()):
            if photo.owner_session == owner_session:
                self.items.pop(key, None)
