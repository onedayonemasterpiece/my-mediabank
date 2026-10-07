from __future__ import annotations

import base64
import logging
import re
from collections import OrderedDict

from live_interaction import LiveError

from .photos import PhotoCache, PhotoError
from .prompts import CORE, REVIEW, FUNCTIONS, PROMPT_VERSION

LOG = logging.getLogger("my_mediabank.review")
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,96}\Z")


class MediaBankAdapter:
    def __init__(self, *, photos: PhotoCache, emit, write, **hooks):
        self.photos, self.emit, self.write = photos, emit, write

    def initialize(self, *, resource_id, actor, model, owner_session=None, **args):
        if (not isinstance(actor, dict) or actor.get("tenant_id") != "my-mediabank"
                or not actor.get("subject") or not owner_session
                or resource_id != "review:" + owner_session[:24]):
            raise LiveError("FORBIDDEN", "Нет доступа к этой сессии")
        return {
            "capability": "photo_review",
            "configuration": {"system_instruction": CORE + "\n\n" + REVIEW,
                "functions": FUNCTIONS, "media_resolution": "MEDIA_RESOLUTION_MEDIUM",
                "voice": "Aoede", "manual_activity_detection": True, "search_enabled": False},
            "context": {"mode": "photo_review", "prompt_version": PROMPT_VERSION,
                        "instruction": "Жди текущую фотографию, не начинай оценку без неё."},
            "state": {"owner_session": owner_session, "photo": None, "request_id": None,
                      "seen": OrderedDict(), "evaluation": None},
            "response": {"prompt_version": PROMPT_VERSION},
        }

    def _snapshot(self, session):
        photo = session.state.get("photo")
        if photo is not None:
            self.write(session, {"type": "snapshot", "data": base64.b64encode(photo.data).decode("ascii"),
                                 "mime_type": "image/jpeg", "optional": False})

    def input(self, session, message):
        if "photo_id" not in message:
            return
        photo_id, request_id = message.get("photo_id"), message.get("request_id")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id) or not isinstance(photo_id, str):
            raise LiveError("INVALID_ARGUMENT", "Нужен идентификатор фотографии и запроса")
        if "text" in message:
            raise LiveError("INVALID_ARGUMENT", "Инструкция к оценке задаётся сервером")
        seen = session.state["seen"]
        if request_id in seen:
            if seen[request_id] != photo_id:
                raise LiveError("REQUEST_CONFLICT", "Запрос уже относится к другой фотографии")
            self.emit(session, {"type": "photo_selected", "photo_id": photo_id, "request_id": request_id, "duplicate": True})
            if session.state.get("evaluation") and session.state["evaluation"].get("request_id") == request_id:
                self.emit(session, session.state["evaluation"])
            return
        try:
            photo = self.photos.get(photo_id, session.state["owner_session"])
        except PhotoError as exc:
            raise LiveError(exc.code, str(exc)) from None
        # Assign selection before scheduling provider input. Late tool calls are fenced below.
        session.state.update(photo=photo, request_id=request_id, evaluation=None)
        self._snapshot(session)
        self.write(session, {"type": "text", "text":
            f"Сейчас выбрана новая фотография: photo_id={photo.id}, request_id={request_id}. "
            f"Только что отправлен её кадр {photo.width}×{photo.height}. "
            "Оцени этот кадр по шкале открыточности, запиши результат инструментом и коротко озвучь его."})
        seen[request_id] = photo_id
        while len(seen) > 64:
            seen.popitem(last=False)
        self.emit(session, {"type": "photo_selected", "photo_id": photo_id, "request_id": request_id})
        LOG.info("photo_selected session=%s photo=%s width=%d height=%d bytes=%d prompt=%s",
                 session.id, photo.id, photo.width, photo.height, len(photo.data), PROMPT_VERSION)

    def execute_tool(self, session, call):
        name, args = call.get("name"), call.get("args")
        if not isinstance(args, dict):
            raise LiveError("INVALID_ARGUMENT", "Некорректный результат оценки")
        photo = session.state.get("photo")
        if photo is None or args.get("photo_id") != photo.id:
            raise LiveError("PHOTO_STALE", "Текущая фотография уже изменилась")
        if name == "next_photo":
            self.emit(session, {"type": "photo_next", "photo_id": photo.id})
            return {"ok": True, "action": "next_requested", "deleted": False, "archived": False}
        if name != "record_photo_evaluation":
            raise LiveError("TOOL_UNAVAILABLE", "Эта функция недоступна")
        if args.get("request_id") != session.state["request_id"]:
            raise LiveError("PHOTO_STALE", "Оценка относится к предыдущему просмотру")
        score = args.get("score")
        if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 10:
            raise LiveError("SCORE_INVALID", "Оценка должна быть целым числом от 1 до 10")
        description, reason = args.get("description"), args.get("reason")
        if any(not isinstance(text, str) or not text.strip() for text in (description, reason)):
            raise LiveError("EVALUATION_INVALID", "Нужны описание и причина оценки")
        if len(description) > 700 or len(reason) > 500:
            raise LiveError("EVALUATION_INVALID", "Описание должно быть коротким")
        result = {"type": "photo_evaluation", "photo_id": photo.id,
                  "request_id": session.state["request_id"], "score": score,
                  "description": description.strip(), "reason": reason.strip(),
                  "model": session.model, "prompt_version": PROMPT_VERSION,
                  "preview_width": photo.width, "preview_height": photo.height}
        session.state["evaluation"] = result
        self.emit(session, result)
        LOG.info("photo_evaluated session=%s photo=%s score=%d model=%s prompt=%s",
                 session.id, photo.id, score, session.model, PROMPT_VERSION)
        return {"ok": True, **{key: value for key, value in result.items() if key != "type"}}

    def on_resumed(self, session):
        # Current visual only, never replay a previous evaluation command or speech.
        self._snapshot(session)

    def on_stopped(self, session):
        session.state["photo"] = None
        session.state["evaluation"] = None
