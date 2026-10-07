from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from live_interaction import LiveError, LiveSocketSessionHost
from live_interaction.provider import run as provider_run
from live_interaction.socket_host import SOCKET_PROTOCOL
from live_interaction.socket_transport import serve_socket, socket_ticket

from . import __version__
from .access import AccessStore, COOKIE
from .adapter import MediaBankAdapter
from .config import Settings, resource_environment
from .photos import MAX_UPLOAD, PhotoCache, PhotoError, normalize_image
from .prompts import PROMPT_VERSION

LOG = logging.getLogger("my_mediabank")


def failure(code, message, status=400):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(settings: Settings | None = None, *, managed_runner=None):
    settings = settings or Settings.from_env()
    access = AccessStore(settings.db_path)
    photos = PhotoCache()
    start_lock = asyncio.Lock()
    image_slots = asyncio.Semaphore(1)
    # Limit accepted upload sockets as well as image decoding, before buffering payloads.
    upload_owners: set[str] = set()

    def readiness():
        if managed_runner is not None:
            return True, None
        if importlib.util.find_spec("ai_resource_control") is None:
            return False, "RESOURCE_SDK_MISSING"
        env = resource_environment()
        if not env.get("AI_RESOURCE_CONTROL_URL") or not env.get("AI_RESOURCE_CONTROL_SERVICE_KEY"):
            return False, "RESOURCE_CONFIG_MISSING"
        return True, None

    async def guarded_runner(*, session, reader, on_event):
        from ai_resource_control import run_guarded
        # The new product is its own registered consumer. No provider key belongs here.
        await run_guarded(consumer="my-mediabank", environment=resource_environment(),
                          reader=reader, on_event=on_event, provider_run=provider_run,
                          binding=session.resource_id)

    host = LiveSocketSessionHost(
        adapter_factory=lambda **hooks: MediaBankAdapter(photos=photos, **hooks),
        managed_runner=managed_runner or guarded_runner,
        models=(settings.model,), max_sessions=2,
        ready_timeout_ms=35_000, client_liveness_timeout_ms=30_000,
    )

    @asynccontextmanager
    async def lifespan(_app):
        async def expire():
            while True:
                await asyncio.sleep(30)
                photos.prune()
                # A closed provider task may still have a final event ring awaiting a client.
                # Reclaim only closed sessions after a bounded grace period.
                import time
                for session in list(host.sessions.values()):
                    if session.closed and time.time() * 1000 - session.last_client_at_ms > 35_000:
                        await host.stop(session_id=session.id, resource_id=session.resource_id, actor=session.actor)
        task = asyncio.create_task(expire(), name="my-mediabank-expiry")
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await host.stop_all()
            photos.items.clear()

    app = FastAPI(title="My MediaBank", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.access, app.state.photos, app.state.live = access, photos, host
    app.state.settings = settings

    @app.middleware("http")
    async def headers_and_origin(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and request.headers.get("origin") != settings.origin:
            return failure("ORIGIN_INVALID", "Откройте приложение с его обычного адреса", 403)
        response = await call_next(request)
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; frame-src 'none'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    def actor_for(request):
        return access.authenticate(request.cookies.get(COOKIE))

    async def small_json(request: Request, limit=4096):
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit:
                raise ValueError("Request too large")
        import json
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Object required")
        return value

    @app.get("/health")
    async def health():
        configured, reason = readiness()
        try:
            sdk_version = importlib.metadata.version("ai-resource-control")
        except importlib.metadata.PackageNotFoundError:
            sdk_version = None
        return {"status": "ok", "version": __version__, "release": settings.release,
                "model": settings.model, "prompt_version": PROMPT_VERSION,
                "resource_consumer": "my-mediabank", "resource_configured": configured,
                "resource_sdk_version": sdk_version, "unavailable_reason": reason,
                "framework_version": importlib.metadata.version("live-interaction"),
                "static_ready": (settings.static_dir / "index.html").is_file(),
                "archive_enabled": False, "phone_delete_enabled": False}

    @app.get("/api/me")
    async def me(request: Request):
        configured, reason = readiness()
        return {"authenticated": actor_for(request) is not None, "model": settings.model,
                "mira_available": configured, "unavailable_reason": reason,
                "prompt_version": PROMPT_VERSION, "version": __version__}

    @app.post("/api/login")
    async def login(request: Request):
        try:
            body = await small_json(request)
        except (ValueError, TypeError):
            return failure("INVALID_ARGUMENT", "Введите код подключения")
        token = access.login(body.get("code"), ttl_seconds=settings.session_days * 86400)
        if token is None:
            return failure("INVITE_INVALID", "Код истёк, уже использован или введён неверно", 401)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, max_age=settings.session_days * 86400,
                            httponly=True, secure=True, samesite="strict", path="/")
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        actor = actor_for(request)
        if actor:
            for session in list(host.sessions.values()):
                if session.resource_id == actor.resource_id:
                    await host.stop(session_id=session.id, resource_id=actor.resource_id, actor=actor.wire)
            photos.clear_owner(actor.session)
            access.logout(request.cookies.get(COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return response

    @app.post("/api/live")
    async def start(request: Request):
        actor = actor_for(request)
        if not actor:
            return failure("AUTH_REQUIRED", "Подключите приложение кодом владельца", 401)
        configured, reason = readiness()
        if not configured:
            return failure(reason, "Подключение Миры на сервере пока недоступно", 503)
        try:
            body = await small_json(request)
        except (ValueError, TypeError):
            return failure("INVALID_ARGUMENT", "Не удалось открыть сессию")
        try:
            async with start_lock:
                # Supersede this login's old session after explicit client Stop/Start.
                for previous in list(host.sessions.values()):
                    if previous.resource_id == actor.resource_id:
                        await host.stop(session_id=previous.id, resource_id=actor.resource_id, actor=actor.wire)
                result = await host.start(resource_id=actor.resource_id, actor=actor.wire,
                                          owner_session=actor.session, attempt_id=body.get("attempt_id"), model=settings.model)
            result["socket_url"] = f"/api/live/{result['session_id']}/socket"
            LOG.info("live_started session=%s consumer=my-mediabank model=%s release=%s",
                     result["session_id"], settings.model, settings.release)
            return result
        except (LiveError, TimeoutError) as exc:
            code = getattr(exc, "code", "LIVE_START_TIMEOUT")
            LOG.warning("live_start_failed code=%s", code)
            return failure(code, "Мира пока не подключилась. Можно повторить подключение", 503)

    @app.post("/api/live/{session_id}/socket-ticket")
    async def renew(session_id: str, request: Request):
        actor = actor_for(request)
        if not actor:
            return failure("AUTH_REQUIRED", "Нужно снова подключить приложение", 401)
        try:
            return {**host.issue_socket_ticket(session_id=session_id, resource_id=actor.resource_id, actor=actor.wire),
                    "socket_url": f"/api/live/{session_id}/socket"}
        except LiveError as exc:
            return failure(exc.code, "Сессия закончилась. Подключите Миру снова", 409)

    @app.post("/api/live/{session_id}/stop")
    async def stop(session_id: str, request: Request):
        actor = actor_for(request)
        if not actor:
            return failure("AUTH_REQUIRED", "Нужно снова подключить приложение", 401)
        try:
            return await host.stop(session_id=session_id, resource_id=actor.resource_id, actor=actor.wire)
        except LiveError as exc:
            return failure(exc.code, "Эта сессия недоступна", 404)

    async def socket_authorized(socket: WebSocket):
        if socket.url.query or socket.headers.get("origin") != settings.origin:
            await socket.close(code=1008, reason="ORIGIN_INVALID")
            return None
        actor = actor_for(socket)
        if not actor:
            await socket.close(code=1008, reason="AUTH_REQUIRED")
            return None
        return actor

    @app.websocket("/api/live/{session_id}/socket")
    async def live_socket(socket: WebSocket, session_id: str):
        actor = await socket_authorized(socket)
        if not actor:
            return
        try:
            ticket = socket_ticket(socket.scope.get("subprotocols", []))
            # Cookie and ticket must belong to this same actor/resource.
            binding = host.open_socket(session_id=session_id, resource_id=actor.resource_id, ticket=ticket)
        except LiveError as exc:
            await socket.close(code=1008, reason=exc.code)
            return
        try:
            await socket.accept(subprotocol=SOCKET_PROTOCOL)

            async def receive():
                message = await socket.receive()
                if message["type"] == "websocket.disconnect":
                    return None
                return message.get("bytes") if message.get("bytes") is not None else message.get("text")

            async def send(payload):
                if isinstance(payload, bytes):
                    await socket.send_bytes(payload)
                else:
                    await socket.send_text(payload)

            async def close(code, reason):
                await socket.close(code=code, reason=reason)

            await serve_socket(binding, receive=receive, send=send, close=close)
        except WebSocketDisconnect:
            await binding.close()
        except Exception:
            # The relay owns framing/recovery; this boundary logs no private payloads.
            LOG.warning("socket_adapter_closed session=%s", session_id)
            await binding.close()

    @app.websocket("/api/photos")
    async def upload(socket: WebSocket):
        actor = await socket_authorized(socket)
        if not actor:
            return
        if actor.session in upload_owners or len(upload_owners) >= 2:
            await socket.close(code=1013, reason="PHOTO_BUSY")
            return
        upload_owners.add(actor.session)
        try:
            await socket.accept()
            message = await asyncio.wait_for(socket.receive(), timeout=20)
            if message["type"] == "websocket.disconnect":
                return
            raw = message.get("bytes")
            if not isinstance(raw, bytes) or not raw or len(raw) > MAX_UPLOAD:
                raise PhotoError("PHOTO_SIZE", "Передайте одну фотографию размером до 2 МБ")
            async with image_slots:
                data, width, height = await asyncio.to_thread(normalize_image, raw)
            del raw
            item = photos.put(actor.session, data, width, height)
            await socket.send_json(item.public())
            await socket.close(code=1000)
        except PhotoError as exc:
            await socket.send_json({"error": {"code": exc.code, "message": str(exc)}})
            await socket.close(code=1003, reason=exc.code)
        except TimeoutError:
            await socket.close(code=1008, reason="PHOTO_TIMEOUT")
        except WebSocketDisconnect:
            pass
        finally:
            upload_owners.discard(actor.session)

    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.static_dir, html=True), name="web")
    else:
        @app.get("/")
        async def missing_web():
            return failure("WEB_BUILD_MISSING", "Экран приложения ещё не собран", 503)
    return app
