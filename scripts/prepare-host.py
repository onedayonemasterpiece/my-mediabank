"""One-time, narrow DevCoveer provisioning for this owner-authorized product.

Reuses canonical resource authority helpers and host credential aliases without
printing values. Only the my-mediabank consumer, its own env, and own user unit
can be created. It neither resets the ledger nor edits another application's keys.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = Path("/home/dev/projects/ai-resource-control")
RESOURCE_HELPER_BLOB = "5d21989ad64e616b280ae9e4bb045e16a4d6127c"
CONFIG = Path("/home/dev/.config/my-mediabank")
STATE = Path("/home/dev/.local/share/my-mediabank/state")
ORIGIN = "https://my-mediabank.kenigevents.ru"


def run(args, **kwargs):
    if args[:2] == ["systemctl", "--user"]:
        runtime = f"/run/user/{os.getuid()}"
        env = dict(os.environ)
        env.update(XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS=f"unix:path={runtime}/bus")
        kwargs["env"] = env
    return subprocess.run(args, check=True, timeout=60, capture_output=True, text=True, **kwargs)


def helper():
    path = RESOURCE_ROOT / "tools/apply_production_migrations.py"
    blob = run(["git", "-C", str(RESOURCE_ROOT), "hash-object", str(path)]).stdout.strip()
    if blob != RESOURCE_HELPER_BLOB:
        raise RuntimeError("Canonical authority helper changed; review before provisioning")
    spec = importlib.util.spec_from_file_location("canonical_resource_provisioning", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(source_sha):
    if run(["git", "rev-parse", "HEAD"], cwd=ROOT).stdout.strip() != source_sha:
        raise RuntimeError("Source checkpoint changed")
    module = helper()
    kind, credential = module.resolve_query_backend(use_devcoveer_host_env=True)
    query = module.database_query if kind == "database" else module.management_query
    existing = query(credential, "SELECT consumer, concurrent_limit, binding_limit, enabled FROM public.ai_resource_consumers WHERE consumer='my-mediabank'")
    if existing and existing != [{"consumer": "my-mediabank", "concurrent_limit": 2, "binding_limit": 1, "enabled": True}]:
        raise RuntimeError("Existing consumer configuration conflicts with the new app")
    if not existing:
        query(credential, "INSERT INTO public.ai_resource_consumers(consumer,concurrent_limit,binding_limit,enabled) VALUES('my-mediabank',2,1,true) ON CONFLICT(consumer) DO NOTHING;")
    verified = query(credential, "SELECT consumer, concurrent_limit, binding_limit, enabled FROM public.ai_resource_consumers WHERE consumer='my-mediabank'")
    if verified != [{"consumer": "my-mediabank", "concurrent_limit": 2, "binding_limit": 1, "enabled": True}]:
        raise RuntimeError("Consumer readback did not match")
    values = module._parse_dotenv_aliases(module.DEVCOVEER_HOST_ENV, {"AI_SUPABASE_URL", "AI_SUPABASE_SECRET_KEY"})
    if not all(values.get(key) for key in ("AI_SUPABASE_URL", "AI_SUPABASE_SECRET_KEY")):
        raise RuntimeError("Canonical resource service binding is unavailable")
    if values["AI_SUPABASE_URL"].rstrip("/") != "https://epyznmylqmchteykjsqj.supabase.co":
        raise RuntimeError("Unexpected resource authority")
    CONFIG.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {
        "MY_MEDIABANK_ORIGIN": ORIGIN,
        "MY_MEDIABANK_STATE_DIR": str(STATE),
        "MY_MEDIABANK_STATIC_DIR": str(ROOT / "dist"),
        "MY_MEDIABANK_LIVE_MODEL": "gemini-3.8-live",
        "MY_MEDIABANK_RELEASE": source_sha,
        "AI_RESOURCE_CONTROL_URL": values["AI_SUPABASE_URL"],
        "AI_RESOURCE_CONTROL_SERVICE_KEY": values["AI_SUPABASE_SECRET_KEY"],
    }
    target = CONFIG / "service.env"
    if target.is_symlink():
        raise RuntimeError("Refusing a symlink configuration")
    if target.exists():
        old = module._parse_dotenv_aliases(target, set(env))
        if any(old.get(key) != value for key, value in env.items() if key != "MY_MEDIABANK_RELEASE"):
            raise RuntimeError("Existing own runtime configuration differs; review instead of overwriting")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        for key, value in env.items():
            output.write(f"{key}={json.dumps(value)}\n")
        output.flush()
        os.fsync(output.fileno())
    target.chmod(0o600)
    unit = Path("/home/dev/.config/systemd/user/my-mediabank.service")
    unit.parent.mkdir(parents=True, exist_ok=True)
    desired = (ROOT / "deploy/my-mediabank.service").read_bytes()
    if unit.is_symlink() or (unit.exists() and unit.read_bytes() != desired):
        raise RuntimeError("An unexpected service unit already exists")
    unit.write_bytes(desired)
    unit.chmod(0o644)
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", "my-mediabank.service"])
    run(["systemctl", "--user", "restart", "my-mediabank.service"])
    for _ in range(15):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8204/health", timeout=2) as response:
                health = json.load(response)
            if health.get("release") == source_sha and health.get("static_ready") and health.get("resource_configured"):
                print(json.dumps({"status": "ready", "service": "my-mediabank.service", "consumer": verified[0], "health": health}))
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError("Own backend did not become ready; inspect its service log")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    if len(args.source_sha) != 40 or any(c not in "0123456789abcdef" for c in args.source_sha):
        parser.error("An exact source SHA is required")
    try:
        prepare(args.source_sha)
    except Exception as exc:
        # Subprocess command/error bodies may include private configuration. Only the class is logged.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, RuntimeError) else "Provisioning failed; inspect bounded operator state"}))
        raise SystemExit(1)
