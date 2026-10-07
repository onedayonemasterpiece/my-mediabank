"""One real, bounded Mira canary against this product's public HTTPS/WSS origin.

Run on the installed DevCoveer host with the product's venv. A separate five-minute
invite is minted locally, consumed through HTTPS, and revoked during cleanup. One
synthetic geometry image and one Live start are allowed; there is no retry, other
model, microphone input, archive, or phone operation. This proves plumbing, not
the taste of the score or the quality of speech on a physical Android device.

Example (the exact deployed Git SHA is required):
  .venv/bin/python scripts/check-live.py --expected-release <40-character-sha>

The authority check reuses prepare-host.py's pinned canonical read-only query
helper in a bounded child process. No private SDK source or credentials are copied.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import io
import json
import os
import re
import signal
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://my-mediabank.kenigevents.ru"
MODEL = "gemini-3.8-live"
PROTOCOL = "wl-live-v1"
FRAMEWORK = "0.3.27"
SDK = "0.1.15"
STATE = Path("/home/dev/.local/share/my-mediabank/state")
SAFE_ID = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")
SAFE_CODE = re.compile(r"[A-Za-z0-9_]{1,80}\Z")


class CheckFailed(RuntimeError):
    """Only this controlled code, never an exception body, reaches the log."""


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def error_code(exc):
    if isinstance(exc, CheckFailed) and SAFE_CODE.fullmatch(str(exc)):
        return str(exc)
    if isinstance(exc, (TimeoutError, asyncio.CancelledError)):
        return "TIMEOUT_OR_CANCELLED"
    return type(exc).__name__[:80]


def progress(stage):
    print(json.dumps({"check": "live_canary", "stage": stage}), flush=True)


def fixture():
    # Original programmatic geometry: no downloaded photo, personal data or text.
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (800, 600), "#e5b879")
    draw = ImageDraw.Draw(image)
    draw.ellipse((525, 65, 625, 165), fill="#ffe4a1")
    draw.rectangle((0, 275, 800, 600), fill="#326779")
    draw.polygon([(0, 350), (145, 290), (260, 420), (480, 515), (800, 570), (800, 600), (0, 600)], fill="#173c40")
    draw.polygon([(385, 225), (385, 390), (475, 390)], fill="#c25944")
    draw.polygon([(385, 250), (310, 390), (378, 390)], fill="#f9e3ba")
    draw.polygon([(300, 398), (480, 398), (450, 419), (325, 419)], fill="#263632")
    data = io.BytesIO()
    image.save(data, "JPEG", quality=82, optimize=True)
    image.close()
    return data.getvalue()


def decode_pcm(frame):
    require(isinstance(frame, bytes) and 14 <= len(frame) <= 256 * 1024 and len(frame) % 2 == 0,
            "PCM_FRAME_LENGTH")
    magic, seq, rate = struct.unpack("!III", frame[:12])
    require(magic == 0x574C4F31 and seq > 0 and 8000 <= rate <= 96000, "PCM_FRAME_HEADER")
    return seq, rate, len(frame) - 12, any(frame[12:])


def authority_worker(binding):
    """Only aggregates for this unique session binding; no admin mutation."""
    require(re.fullmatch(r"[0-9a-f]{64}", binding) is not None, "BINDING_INVALID")
    spec = importlib.util.spec_from_file_location("canary_provisioning", ROOT / "scripts/prepare-host.py")
    provisioning = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provisioning)
    canonical = provisioning.helper()  # Checks the already reviewed helper blob.
    kind, credential = canonical.resolve_query_backend(use_devcoveer_host_env=True)
    query = canonical.database_query if kind == "database" else canonical.management_query
    rows = query(credential, """
        SELECT count(*)::int AS leases,
          count(*) FILTER (WHERE activated_at IS NOT NULL)::int AS activated,
          count(*) FILTER (WHERE state='released' AND closed_at IS NOT NULL
                           AND capacity_until<=clock_timestamp())::int AS released,
          count(*) FILTER (WHERE state='active' OR capacity_until>clock_timestamp())::int AS holding_capacity,
          count(*) FILTER (WHERE model<>'gemini-3.8-live')::int AS wrong_model
        FROM public.ai_resource_leases
        WHERE consumer='my-mediabank' AND binding='%s'
    """ % binding)
    require(isinstance(rows, list) and len(rows) == 1, "AUTHORITY_RESPONSE")
    counts = rows[0]
    require(set(counts) == {"leases", "activated", "released", "holding_capacity", "wrong_model"}
            and all(type(value) is int and value >= 0 for value in counts.values()), "AUTHORITY_RESPONSE")
    return counts


async def authority_readback(binding):
    # The canonical helper's own DB timeout is longer. Bound this whole process
    # group to 25 seconds, including any psql child, and never print its stderr.
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), "--lease-readback", binding,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 25)
        require(process.returncode == 0 and len(output) <= 4096, "AUTHORITY_READBACK_FAILED")
        result = json.loads(output)
        require(isinstance(result, dict) and "leases" in result, "AUTHORITY_RESPONSE")
        return result
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()


async def request(client, method, path, *, body=None, timeout=10, status=200):
    response = await client.request(method, path, json=body, timeout=timeout)
    require(len(response.content) <= 65536, "HTTP_RESPONSE_SIZE")
    try:
        result = response.json()
    except ValueError:
        raise CheckFailed("HTTP_RESPONSE_JSON") from None
    if response.status_code != status:
        code = result.get("error", {}).get("code") if isinstance(result, dict) else None
        raise CheckFailed(code if isinstance(code, str) and SAFE_CODE.fullmatch(code)
                          else "HTTP_STATUS_" + str(response.status_code))
    require(isinstance(result, dict), "HTTP_RESPONSE_OBJECT")
    return result


async def run_check(args):
    import httpx  # Installed with the private SDK / the project's test extra.
    from websockets.legacy.client import connect
    from my_mediabank.access import AccessStore, COOKIE, digest

    task = asyncio.current_task()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    started = time.monotonic()
    report = {"check": "live_canary", "status": "failed", "fixture": "synthetic_geometry",
              "quality_assessment": False, "model": MODEL, "consumer": "my-mediabank",
              "live_start_attempts": 0, "live_starts": 0, "uploaded_images": 0, "audio_chunks": 0, "audio_bytes": 0,
              "audio_nonzero": False, "turn_complete": False, "tool_confirmed": False,
              "cleanup_errors": []}
    store = code = token = binding = session_id = socket = None
    photo_id = request_id = None
    authenticated = False

    async with httpx.AsyncClient(base_url=ORIGIN, headers={"Origin": ORIGIN},
                                 follow_redirects=False, trust_env=False) as client:
        try:
            async with asyncio.timeout(args.timeout_seconds):
                progress("preflight")
                health = await request(client, "GET", "/health")
                require(health.get("release") == args.expected_release, "DEPLOYED_RELEASE_MISMATCH")
                require(health.get("model") == MODEL and health.get("resource_consumer") == "my-mediabank",
                        "MODEL_OR_CONSUMER_MISMATCH")
                require(health.get("framework_version") == FRAMEWORK and health.get("resource_sdk_version") == SDK,
                        "PINNED_DEPENDENCY_MISMATCH")
                require(health.get("resource_configured") is True and health.get("static_ready") is True,
                        "BACKEND_NOT_READY")
                require(health.get("archive_enabled") is False and health.get("phone_delete_enabled") is False,
                        "UNEXPECTED_PRODUCT_MODE")
                report.update(release=health["release"], framework_version=FRAMEWORK, resource_sdk_version=SDK)
                db = args.state_dir / "access.sqlite3"
                require(db.is_file() and not db.is_symlink(), "INSTALLED_ACCESS_STORE_REQUIRED")
                store = AccessStore(db)
                suffix = os.urandom(8).hex()
                code = store.invite(ttl_seconds=300, subject="live-canary-" + suffix)
                require((await request(client, "POST", "/api/login", body={"code": code})).get("ok") is True,
                        "LOGIN_FAILED")
                token = client.cookies.get(COOKIE)
                require(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{20,128}", token), "SESSION_COOKIE_MISSING")
                authenticated = True
                require((await request(client, "GET", "/api/me")).get("authenticated") is True, "LOGIN_READBACK_FAILED")
                resource_id = "review:" + digest(token)[:24]
                binding = hashlib.sha256(resource_id.encode()).hexdigest()
                require((await authority_readback(binding))["leases"] == 0, "CANARY_BINDING_NOT_FRESH")
                cookie = {"Cookie": COOKIE + "=" + token}
                connection = dict(origin=ORIGIN, extra_headers=cookie, compression=None,
                                  max_size=256 * 1024, max_queue=8, open_timeout=10,
                                  close_timeout=2, ping_interval=None)

                progress("upload_one_synthetic_image")
                async with connect(ORIGIN.replace("https:", "wss:") + "/api/photos", **connection) as upload:
                    await upload.send(fixture())
                    raw = await asyncio.wait_for(upload.recv(), 15)
                    require(isinstance(raw, str), "PHOTO_ACK_INVALID")
                    photo = json.loads(raw)
                    require(isinstance(photo, dict) and not photo.get("error"), "PHOTO_UPLOAD_FAILED")
                    photo_id = photo.get("photo_id")
                    require(isinstance(photo_id, str) and SAFE_ID.fullmatch(photo_id), "PHOTO_ID_INVALID")
                    require(0 < photo.get("width", 0) <= 1024 and 0 < photo.get("height", 0) <= 1024
                            and 0 < photo.get("bytes", 0) <= 180 * 1024, "PREVIEW_BOUND_EXCEEDED")
                    report["uploaded_images"] = 1

                progress("start_one_live_session")
                attempt_id, request_id = "canary_" + suffix, "photo_canary_" + suffix
                report["live_start_attempts"] = 1
                live = await request(client, "POST", "/api/live", body={"attempt_id": attempt_id}, timeout=40)
                report["live_starts"] = 1
                session_id = live.get("session_id")
                require(isinstance(session_id, str) and SAFE_ID.fullmatch(session_id), "LIVE_SESSION_INVALID")
                require(live.get("transport_protocol") == PROTOCOL and live.get("attempt_id") == attempt_id,
                        "LIVE_PROTOCOL_MISMATCH")
                require(live.get("socket_url") == f"/api/live/{session_id}/socket", "SOCKET_URL_MISMATCH")
                ticket = live.get("socket_ticket")
                require(isinstance(ticket, str) and re.fullmatch(r"[A-Za-z0-9_-]{20,512}", ticket), "SOCKET_TICKET_INVALID")
                socket = await connect(ORIGIN.replace("https:", "wss:") + live["socket_url"],
                                       subprotocols=[PROTOCOL, "wl-ticket." + ticket], **connection)
                require(socket.subprotocol == PROTOCOL, "SOCKET_SUBPROTOCOL_MISMATCH")
                await socket.send(json.dumps({"type": "hello", "protocol": PROTOCOL, "attempt_id": attempt_id,
                                              "cursor": 0, "connection_generation": 1}))
                hello = json.loads(await asyncio.wait_for(socket.recv(), 8))
                require(hello.get("type") == "hello_ack" and hello.get("protocol") == PROTOCOL
                        and hello.get("connection_generation") == 1, "HELLO_ACK_INVALID")
                report["https_wss_handshake"] = True
                # This is the real app command. The backend sends its bounded
                # snapshot followed by its own scoring instruction to Live.
                await socket.send(json.dumps({"type": "input", "message": {
                    "photo_id": photo_id, "request_id": request_id}}))
                progress("await_score_description_and_audio")
                selected = evaluated = False
                seq, last_ping = 0, time.monotonic()
                while True:
                    if time.monotonic() - last_ping >= 6:
                        await socket.send('{"type":"ping"}')
                        last_ping = time.monotonic()
                    try:
                        frame = await asyncio.wait_for(socket.recv(), 6)
                    except TimeoutError:
                        continue
                    if isinstance(frame, bytes):
                        next_seq, rate, size, nonzero = decode_pcm(frame)
                        require(next_seq > seq, "EVENT_SEQUENCE_INVALID")
                        seq = next_seq
                        if evaluated:
                            report["audio_chunks"] += 1
                            report["audio_bytes"] += size
                            report["audio_nonzero"] |= nonzero
                            report["audio_sample_rate"] = rate
                        continue
                    message = json.loads(frame)
                    if message.get("type") == "pong":
                        continue
                    require(message.get("type") == "event" and isinstance(message.get("event"), dict),
                            "EVENT_ENVELOPE_INVALID")
                    event = message["event"]
                    next_seq = event.get("seq")
                    require(type(next_seq) is int and next_seq > seq, "EVENT_SEQUENCE_INVALID")
                    seq = next_seq
                    kind = event.get("type")
                    if kind in {"error", "resource_fallback", "closed"}:
                        code_value = event.get("code")
                        raise CheckFailed(code_value if isinstance(code_value, str) and SAFE_CODE.fullmatch(code_value)
                                          else "LIVE_EVENT_" + str(kind).upper())
                    if kind == "tool_result" and event.get("status") == "error":
                        raise CheckFailed("PHOTO_TOOL_FAILED")
                    if kind == "photo_selected":
                        require(event.get("photo_id") == photo_id and event.get("request_id") == request_id,
                                "PHOTO_SELECTION_MISMATCH")
                        selected = True
                    elif kind == "tool_call":
                        calls = event.get("calls", [])
                        require(calls and all(call.get("name") == "record_photo_evaluation" for call in calls),
                                "UNEXPECTED_TOOL_CALL")
                        report["tool_confirmed"] = True
                    elif kind == "photo_evaluation":
                        require(selected and report["tool_confirmed"] and event.get("photo_id") == photo_id
                                and event.get("request_id") == request_id and event.get("model") == MODEL,
                                "EVALUATION_BINDING_MISMATCH")
                        score = event.get("score")
                        require(type(score) is int and 1 <= score <= 10, "SCORE_INVALID")
                        for key, limit in (("description", 700), ("reason", 500)):
                            value = event.get(key)
                            require(isinstance(value, str) and 0 < len(value.strip()) <= limit,
                                    "EVALUATION_TEXT_INVALID")
                        report.update(score=score, description_chars=len(event["description"]),
                                      reason_chars=len(event["reason"]), prompt_version=event.get("prompt_version"))
                        evaluated = True
                    elif kind == "turn_complete" and evaluated and report["audio_nonzero"]:
                        report["turn_complete"] = True
                        break
                report["status"] = "passed"
        except (Exception, asyncio.CancelledError) as exc:
            report["error_code"] = error_code(exc)
        finally:
            progress("stop_and_release")
            # WSS stop is exercised first. HTTPS stop is the idempotent cleanup
            # fallback and readback, never a second Live start.
            if socket is not None:
                try:
                    await asyncio.wait_for(socket.send('{"type":"stop"}'), 2)
                    report["wss_stop_sent"] = True
                    await asyncio.wait_for(socket.wait_closed(), 5)
                except Exception:
                    report["wss_stop_confirmed"] = False
                else:
                    report["wss_stop_confirmed"] = True
            if session_id and authenticated:
                try:
                    stopped = await request(client, "POST", f"/api/live/{session_id}/stop")
                    require(stopped.get("ok") is True, "STOP_NOT_CONFIRMED")
                    await request(client, "POST", f"/api/live/{session_id}/socket-ticket", status=409, timeout=8)
                    report["http_stop_confirmed"] = True
                except Exception as exc:
                    report["cleanup_errors"].append(error_code(exc))
            if socket is not None:
                try:
                    await asyncio.wait_for(socket.close(code=1000), 3)
                except Exception:
                    pass
            if authenticated:
                try:
                    require((await request(client, "POST", "/api/logout", timeout=8)).get("ok") is True,
                            "LOGOUT_FAILED")
                    require((await request(client, "GET", "/api/me", timeout=8)).get("authenticated") is False,
                            "LOGOUT_READBACK_FAILED")
                    report["logout_confirmed"] = True
                except Exception as exc:
                    report["cleanup_errors"].append(error_code(exc))
            if store is not None:
                try:
                    if token:
                        store.logout(token)  # Revoke only this canary's local cookie, even if HTTPS failed.
                    if code:
                        with store.connection() as con:
                            con.execute("DELETE FROM invites WHERE hash=?", (digest(code),))
                except Exception as exc:
                    report["cleanup_errors"].append(error_code(exc))
            if binding:
                try:
                    counts = await authority_readback(binding)
                    report["authority"] = counts
                    require(counts["holding_capacity"] == 0 and counts["wrong_model"] == 0
                            and counts["released"] == counts["leases"], "RESOURCE_RELEASE_NOT_CONFIRMED")
                    if report["status"] == "passed":
                        require(counts["leases"] == 1 and counts["activated"] == 1, "EXPECTED_ONE_ACTIVATED_LEASE")
                    report["resource_release_confirmed"] = True
                except Exception as exc:
                    report["cleanup_errors"].append(error_code(exc))
            if report["status"] == "passed" and not (report.get("wss_stop_confirmed")
                    and report.get("http_stop_confirmed") and report.get("logout_confirmed")
                    and report.get("resource_release_confirmed")):
                report["cleanup_errors"].append("CLEAN_STOP_NOT_CONFIRMED")
            if report["cleanup_errors"]:
                report["status"] = "failed"
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--expected-release", help="Exact installed source commit, checked before login or inference")
    parser.add_argument("--state-dir", type=Path, default=STATE, help="Installed product's own state directory")
    parser.add_argument("--timeout-seconds", type=int, default=90, help="Whole main flow bound (20–90 seconds); cleanup is separately bounded")
    parser.add_argument("--lease-readback", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.lease_readback:
            print(json.dumps(authority_worker(args.lease_readback)), flush=True)
            return 0
        require(isinstance(args.expected_release, str)
                and re.fullmatch(r"[0-9a-f]{40}", args.expected_release), "EXACT_RELEASE_REQUIRED")
        require(20 <= args.timeout_seconds <= 90, "TIMEOUT_OUT_OF_RANGE")
        result = asyncio.run(run_check(args))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if result["status"] == "passed" else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(json.dumps({"check": "live_canary", "status": "failed", "error_code": error_code(exc)}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
