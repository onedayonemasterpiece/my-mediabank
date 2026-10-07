"""Add only my-mediabank to the existing HTTPS/WSS edge after local readiness.

No other app route or certificate is changed. Current nginx configuration uses a
single-file bind mount, so verified writes preserve its inode. Backup and rollback
follow the existing DevCoveer edge publication mechanism.
"""
import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

HOST = "my-mediabank.kenigevents.ru"
NGINX = Path("/home/dev/.local/state/my-data-hub-control-plane/edge/nginx.conf")
CERT = Path("/home/dev/.local/state/devcoveer-public-edge")
ACME = Path("/home/dev/.local/state/my-data-hub-control-plane/edge/acme-webroot")
CONTAINER = "record-idea-hub-backend-edge-1"
YC = "/home/dev/yandex-cloud/bin/yc"

BLOCK = '''
    # my-mediabank: isolated photo-review MVP
    server {
        listen 443 ssl;
        http2 on;
        server_name my-mediabank.kenigevents.ru;
        ssl_certificate /etc/letsencrypt/live/my-mediabank/fullchain.pem;
        ssl_certificate_key /etc/letsencrypt/live/my-mediabank/privkey.pem;
        ssl_protocols TLSv1.2 TLSv1.3;
        client_max_body_size 2m;
        location / {
            proxy_pass http://127.0.0.1:8204;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Authorization $http_authorization;
            proxy_set_header Connection $connection_upgrade;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Forwarded "";
            proxy_set_header X-Forwarded-For "";
            proxy_set_header X-Forwarded-Host $host;
            proxy_set_header X-Forwarded-Port 443;
            proxy_set_header X-Forwarded-Proto https;
            proxy_set_header X-Real-IP "";
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 300s;
            proxy_send_timeout 300s;
        }
    }
'''


def run(args, timeout=60):
    return subprocess.run([str(x) for x in args], check=True, timeout=timeout,
                          text=True, capture_output=True)


def read_health(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)


def write_in_place(path, data):
    mode = path.stat().st_mode & 0o777
    with path.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(mode)


def publish(expected_sha, source_sha):
    health = read_health("http://127.0.0.1:8204/health")
    if (health.get("release") != source_sha or not health.get("static_ready") or
            not health.get("resource_configured") or health.get("resource_consumer") != "my-mediabank"):
        raise RuntimeError("Exact local release is not ready")
    old = NGINX.read_bytes()
    already = BLOCK.encode() in old
    if not already and hashlib.sha256(old).hexdigest() != expected_sha:
        raise RuntimeError("Edge configuration changed; reread before mutation")
    if not already and HOST.encode() in old:
        raise RuntimeError("Unexpected existing my-mediabank edge block")
    addresses = {item[4][0] for item in socket.getaddrinfo("mcp-datahub.kenigevents.ru", 443, socket.AF_INET, socket.SOCK_STREAM)}
    if len(addresses) != 1:
        raise RuntimeError("Existing edge has ambiguous IPv4 target")
    address = addresses.pop()
    zones = json.loads(run([YC, "dns", "zone", "list", "--format", "json", "--no-user-output"]).stdout)
    matches = [zone for zone in zones if zone.get("zone") == "kenigevents.ru."]
    if len(matches) != 1:
        raise RuntimeError("Canonical DNS zone is ambiguous")
    zone_id = matches[0]["id"]
    records = json.loads(run([YC, "dns", "zone", "list-records", "--id", zone_id, "--format", "json", "--no-user-output"]).stdout)
    records = records.get("record_sets", []) if isinstance(records, dict) else records
    existing = [record for record in records if record.get("name") == HOST + "."]
    if existing and not all(record.get("type") == "A" and set(record.get("data", [])) == {address} for record in existing):
        raise RuntimeError("Conflicting DNS record; no update performed")
    if not existing:
        run([YC, "dns", "zone", "add-records", "--id", zone_id, "--record", f"{HOST}. 300 A {address}", "--no-user-output"])
    # Reconcile DNS before ACME, no repeated record creation after a lost response.
    for _ in range(12):
        try:
            resolved = {item[4][0] for item in socket.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM)}
            if resolved == {address}:
                break
        except socket.gaierror:
            pass
        time.sleep(5)
    else:
        raise RuntimeError("DNS is created but not visible yet; continue this same publication later")
    fullchain = CERT / "letsencrypt/live/my-mediabank/fullchain.pem"
    if not fullchain.exists():
        run(["/usr/bin/certbot", "certonly", "--non-interactive", "--webroot", "-w", ACME,
             "--cert-name", "my-mediabank", "-d", HOST,
             "--config-dir", CERT / "letsencrypt", "--work-dir", CERT / "work", "--logs-dir", CERT / "logs"], timeout=180)
    if not already:
        if NGINX.read_bytes() != old:
            raise RuntimeError("Edge configuration changed during certificate provisioning")
        run(["docker", "exec", CONTAINER, "nginx", "-t"])
        directory = Path(run(["/home/dev/.local/bin/dev-artifacts", "new", "my-mediabank", "edge-publication", "--retain", "--reason", "First runtime route rollback evidence"]).stdout.strip().splitlines()[-1])
        if not directory.is_dir() or not (directory / ".artifact.json").is_file():
            raise RuntimeError("Managed rollback directory was not created")
        backup = directory / "nginx.before.conf"
        backup.write_bytes(old)
        backup.chmod(0o600)
        text = old.decode()
        if not text.rstrip().endswith("}"):
            raise RuntimeError("Unexpected nginx root structure")
        position = text.rfind("}")
        updated = (text[:position] + BLOCK + text[position:]).encode()
        write_in_place(NGINX, updated)
        try:
            run(["docker", "exec", CONTAINER, "nginx", "-t"])
            run(["docker", "exec", CONTAINER, "nginx", "-s", "reload"])
        except Exception:
            if NGINX.read_bytes() == updated:
                write_in_place(NGINX, old)
                run(["docker", "exec", CONTAINER, "nginx", "-t"])
                run(["docker", "exec", CONTAINER, "nginx", "-s", "reload"])
            raise
    public = read_health(f"https://{HOST}/health")
    if public.get("release") != source_sha:
        raise RuntimeError("Public release readback mismatch")
    print(json.dumps({"status": "published", "origin": f"https://{HOST}",
                      "release": source_sha, "nginx_sha256": hashlib.sha256(NGINX.read_bytes()).hexdigest(),
                      "tls_verified": True, "health": public}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-nginx-sha", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    try:
        publish(args.expected_nginx_sha, args.source_sha)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, RuntimeError) else "Edge action failed; reconcile existing DNS/certificate/route before retry"}))
        raise SystemExit(1)
