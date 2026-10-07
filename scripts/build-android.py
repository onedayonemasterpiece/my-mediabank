#!/usr/bin/env python3
"""Build and verify this app without a vendored Gradle wrapper or borrowed signer.

Only exact installed toolchain locations are inspected. Missing Gradle/SDK
components are installed into this app's toolchain cache from vendor HTTPS
archives whose published checksums are verified before extraction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
GRADLE_VERSION = "8.10.2"
SDK_PACKAGES = ("platforms;android-35", "build-tools;35.0.0")
GIB = 1024**3
PRIVATE_VALUES: list[str] = []


def log(message: str) -> None:
    for value in PRIVATE_VALUES:
        if value:
            message = message.replace(value, "[redacted]")
    print(message, flush=True)


def run(label: str, command: list[str], env: dict[str, str], *, quiet=False, input_text=None, timeout=1800) -> str:
    log(f"stage: {label}")
    if quiet or input_text is not None:
        result = subprocess.run(command, cwd=ROOT, env=env, input=input_text, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        output = result.stdout
        if result.returncode:
            log(output[-12000:])
            raise RuntimeError(f"{label} failed (exit {result.returncode})")
        return output
    process = subprocess.Popen(command, cwd=ROOT, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip())
        lines.append(line)
        if len(lines) > 5000:
            del lines[:1000]
    code = process.wait(timeout=timeout)
    if code:
        raise RuntimeError(f"{label} failed (exit {code})")
    return "".join(lines)


def check_disk(path: Path, required: int) -> None:
    probe = path
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    log(f"storage: {free // (1024**2)} MiB free; required {required // (1024**2)} MiB")
    if free < required:
        raise RuntimeError("Insufficient disk space for the bounded Android build/toolchain installation")


def first_executable(candidates: list[str | Path | None]) -> Path | None:
    for candidate in candidates:
        if candidate:
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return path.resolve()
    return None


def java_home(cache: Path, allow_install: bool) -> Path:
    configured = os.environ.get("JAVA_HOME")
    candidates = [Path(configured) / "bin/java" if configured else None, shutil.which("java"),
                  "/usr/lib/jvm/java-17-openjdk-amd64/bin/java",
                  "/usr/lib/jvm/java-17-openjdk-arm64/bin/java",
                  "/usr/lib/jvm/temurin-17-jdk-amd64/bin/java",
                  "/usr/lib/jvm/default-java/bin/java"]
    java_marker = cache / "temurin-17/java-home.json"
    if java_marker.is_file():
        cached = json.loads(java_marker.read_text())
        relative = Path(cached.get("directory", ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Invalid app-owned JDK cache marker")
        candidates.append(cache / "temurin-17" / relative / "bin/java")
    for candidate in candidates:
        java = first_executable([candidate])
        if not java or not (java.parent / "javac").is_file() or not (java.parent / "keytool").is_file():
            continue
        result = subprocess.run([str(java), "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=15)
        match = re.search(r'version "(\d+)', result.stdout)
        if result.returncode == 0 and match and 17 <= int(match[1]) <= 23:
            log(f"java: {java}; {result.stdout.splitlines()[0]}")
            return java.parent.parent
    if not allow_install:
        raise RuntimeError("A usable JDK 17–23 with javac/keytool was not found in PATH or known JDK locations")
    # About 2 GiB for the minimal JDK/Gradle/SDK and Java-only build cache,
    # plus 1 GiB reserve. Each actual archive is checked again before extraction;
    # our compressed bootstrap downloads are removed after successful install.
    log("bootstrap budget: approximately 2 GiB toolchains/build cache plus 1 GiB reserve")
    check_disk(cache, 3 * GIB)
    architecture = {"x86_64": "x64", "aarch64": "aarch64"}.get(platform.machine())
    if not architecture or platform.system() != "Linux":
        raise RuntimeError("Automatic JDK bootstrap is limited to supported Linux hosts")
    metadata_url = ("https://api.adoptium.net/v3/assets/latest/17/hotspot?architecture=" + architecture
                    + "&image_type=jdk&os=linux&vendor=eclipse")
    assets = json.loads(fetch_bytes(metadata_url))
    if not isinstance(assets, list) or not assets:
        raise RuntimeError("Temurin vendor metadata did not return a JDK 17 archive")
    asset = assets[0]
    package = asset["binary"]["package"]
    root_name = str(asset["release_name"])
    filename = str(package["name"])
    if not re.fullmatch(r"jdk-17[0-9A-Za-z.+_-]*", root_name) or not re.fullmatch(r"OpenJDK17U[-0-9A-Za-z_.]+\.tar\.gz", filename):
        raise RuntimeError("Unexpected Temurin JDK archive identity")
    if not str(package["link"]).startswith("https://"):
        raise RuntimeError("Temurin download must use HTTPS")
    archive = cache / "downloads" / filename
    download(str(package["link"]), archive, str(package["checksum"]), "sha256", int(package["size"]))
    destination = cache / "temurin-17"
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        expanded = sum(member.size for member in members if member.isfile())
        if expanded > 2 * GIB:
            raise RuntimeError("JDK archive expansion exceeds 2 GiB bound")
        check_disk(destination, expanded + GIB)
        for member in members:
            relative = Path(member.name)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != root_name:
                raise RuntimeError("Unexpected path in JDK archive")
        # Python's data filter rejects device files and escaping link targets.
        if not hasattr(tarfile, "data_filter"):
            raise RuntimeError("JDK bootstrap requires a Python tarfile data filter")
        bundle.extractall(destination, filter="data")
    result = destination / root_name
    if not (result / "bin/java").is_file() or not (result / "bin/javac").is_file():
        raise RuntimeError("Verified Temurin archive lacked the expected JDK tools")
    java_marker.write_text(json.dumps({"directory": root_name, "sha256": package["checksum"],
                                      "source": metadata_url}) + "\n")
    archive.unlink()
    log(f"java: installed verified Temurin 17 in {result}")
    return result


def sdk_candidates() -> list[Path]:
    return list(dict.fromkeys(Path(value) for value in [os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"), "/home/dev/Android/Sdk",
        "/home/dev/.local/share/android-sdk", "/opt/android-sdk", "/usr/lib/android-sdk"] if value))


def sdk_complete(path: Path) -> bool:
    return all((path / relative).is_file() for relative in ["platforms/android-35/android.jar",
        "build-tools/35.0.0/apksigner", "build-tools/35.0.0/aapt"])


def fetch_bytes(url: str, limit: int = 16 * 1024**2) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "MyMediaBank-AndroidBuild/1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError("Vendor metadata exceeded its expected size")
    return data


def download(url: str, path: Path, checksum: str, algorithm="sha256", expected_size=0) -> None:
    if algorithm not in ("sha256", "sha1") or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", checksum):
        raise RuntimeError("Vendor archive checksum was not valid")
    if path.is_file():
        with path.open("rb") as cached:
            verified = hashlib.file_digest(cached, algorithm).hexdigest() == checksum.lower()
        if verified:
            log(f"download: verified cached {path.name}")
            return
    check_disk(path.parent, max(2 * GIB, expected_size * 3))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    digest = hashlib.new(algorithm)
    size = 0
    log(f"download: {path.name} from vendor HTTPS source")
    request = urllib.request.Request(url, headers={"User-Agent": "MyMediaBank-AndroidBuild/1"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > 1024**3:
                raise RuntimeError("Toolchain archive exceeds 1 GiB bound")
            digest.update(block)
            output.write(block)
    if digest.hexdigest() != checksum.lower() or (expected_size and size != expected_size):
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"Checksum or length mismatch for {path.name}")
    partial.replace(path)
    log(f"download: verified {algorithm} for {path.name}; {size} bytes")


def unpack(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        expanded = sum(item.file_size for item in bundle.infolist())
        if expanded > 3 * GIB:
            raise RuntimeError("Toolchain archive expansion exceeds 3 GiB bound")
        check_disk(destination, expanded + GIB)
        for item in bundle.infolist():
            relative = Path(item.filename)
            mode = item.external_attr >> 16
            if relative.is_absolute() or ".." in relative.parts or stat.S_ISLNK(mode):
                raise RuntimeError("Unsafe path in toolchain archive")
        bundle.extractall(destination)
        for item in bundle.infolist():
            extracted = destination / item.filename
            if extracted.is_file() and (item.external_attr >> 16) & 0o111:
                extracted.chmod(0o755)


def gradle(cache: Path, env: dict[str, str], allow_install: bool) -> Path:
    candidates = [shutil.which("gradle"), cache / f"gradle-{GRADLE_VERSION}/bin/gradle",
        f"/opt/gradle/gradle-{GRADLE_VERSION}/bin/gradle", f"/opt/gradle-{GRADLE_VERSION}/bin/gradle",
        f"/home/dev/.local/share/gradle/gradle-{GRADLE_VERSION}/bin/gradle"]
    for candidate in candidates:
        executable = first_executable([candidate])
        if executable:
            try:
                info = run("check Gradle version", [str(executable), "--version"], env, quiet=True, timeout=40)
            except (RuntimeError, subprocess.TimeoutExpired):
                continue
            if re.search(rf"(?m)^Gradle {re.escape(GRADLE_VERSION)}$", info):
                log(f"gradle: {executable}")
                return executable
    if not allow_install:
        raise RuntimeError(f"Gradle {GRADLE_VERSION} is absent; installation is disabled")
    base = f"https://services.gradle.org/distributions/gradle-{GRADLE_VERSION}-bin.zip"
    checksum = fetch_bytes(base + ".sha256", 1024).decode().strip()
    archive = cache / "downloads" / f"gradle-{GRADLE_VERSION}-bin.zip"
    download(base, archive, checksum)
    unpack(archive, cache)
    executable = cache / f"gradle-{GRADLE_VERSION}/bin/gradle"
    if not executable.is_file():
        raise RuntimeError("Verified Gradle archive did not contain the expected executable")
    executable.chmod(0o755)
    archive.unlink()
    return executable


def install_cmdline_tools(sdk: Path, cache: Path) -> Path:
    # Pin command-line tools 12.0; package metadata supplies the vendor checksum.
    document = ET.fromstring(fetch_bytes("https://dl.google.com/android/repository/repository2-1.xml"))
    for element in document.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    package = next((item for item in document.findall("remotePackage") if item.get("path") == "cmdline-tools;12.0"), None)
    if package is None:
        raise RuntimeError("Pinned Android command-line tools 12.0 were absent from vendor metadata")
    archive = next((item for item in package.findall("archives/archive") if item.findtext("host-os") == "linux"), None)
    if archive is None:
        raise RuntimeError("Vendor metadata has no Linux command-line tools archive")
    complete = archive.find("complete")
    assert complete is not None
    filename = complete.findtext("url", "")
    if not re.fullmatch(r"commandlinetools-linux-[0-9]+_latest\.zip", filename):
        raise RuntimeError("Unexpected Android vendor archive name")
    checksum_node = complete.find("checksum")
    assert checksum_node is not None
    checksum = (checksum_node.text or "").strip()
    algorithm = checksum_node.get("type", "sha1")
    downloaded = cache / "downloads" / filename
    download("https://dl.google.com/android/repository/" + filename, downloaded, checksum, algorithm,
             int(complete.findtext("size", "0")))
    staging = cache / "cmdline-tools-12-staging"
    unpack(downloaded, staging)
    source = staging / "cmdline-tools"
    target = sdk / "cmdline-tools/12.0"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise RuntimeError("Incomplete command-line tools target already exists; inspect before replacing it")
    source.rename(target)
    downloaded.unlink()
    return target / "bin/sdkmanager"


def android_sdk(cache: Path, env: dict[str, str], allow_install: bool) -> Path:
    own_sdk = Path.home() / ".local/share/my-mediabank/android-sdk"
    candidates = [*sdk_candidates(), own_sdk]
    for candidate in candidates:
        if sdk_complete(candidate):
            log(f"android_sdk: {candidate}")
            return candidate
    if not allow_install:
        raise RuntimeError("Android SDK 35 and build-tools 35.0.0 are absent; installation is disabled")
    # An app-owned SDK avoids upgrading or altering another project's toolchain.
    check_disk(own_sdk, 2 * GIB)
    own_sdk.mkdir(parents=True, exist_ok=True)
    manager_candidates = [shutil.which("sdkmanager")]
    for candidate in candidates:
        manager_candidates.extend([candidate / "cmdline-tools/latest/bin/sdkmanager",
                                   candidate / "cmdline-tools/12.0/bin/sdkmanager",
                                   candidate / "tools/bin/sdkmanager"])
    manager = first_executable(manager_candidates) or install_cmdline_tools(own_sdk, cache)
    env["ANDROID_HOME"] = env["ANDROID_SDK_ROOT"] = str(own_sdk)
    run("accept required Android SDK licenses", [str(manager), f"--sdk_root={own_sdk}", "--licenses"],
        env, quiet=True, input_text="y\n" * 100, timeout=300)
    run("install only Android SDK 35 and build-tools 35.0.0",
        [str(manager), f"--sdk_root={own_sdk}", *SDK_PACKAGES], env, input_text="y\n" * 100, timeout=900)
    if not sdk_complete(own_sdk):
        raise RuntimeError("Android SDK installation did not produce all required components")
    return own_sdk


def signing(env: dict[str, str], java: Path) -> None:
    names = ["ANDROID_KEYSTORE_PATH", "ANDROID_KEYSTORE_PASSWORD", "ANDROID_KEY_ALIAS", "ANDROID_KEY_PASSWORD"]
    if all(env.get(name) for name in names):
        keystore = Path(env["ANDROID_KEYSTORE_PATH"]).resolve()
        if keystore.is_relative_to(ROOT) or not keystore.is_file():
            raise RuntimeError("Release signer must be an existing private file outside the public repository")
        PRIVATE_VALUES.extend([env["ANDROID_KEYSTORE_PASSWORD"], env["ANDROID_KEY_PASSWORD"]])
        log("signing: externally configured app release signer")
        return
    if any(env.get(name) for name in names):
        raise RuntimeError("Partial Android signing configuration; refusing to replace an existing identity")
    directory = Path.home() / ".config/my-mediabank/android-signing"
    if directory.is_symlink():
        raise RuntimeError("Private signer directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    config = directory / "signer.json"
    keystore = directory / "release.p12"
    if config.is_symlink() or keystore.is_symlink():
        raise RuntimeError("Private signer files must not be symlinks")
    if config.is_file():
        private = json.loads(config.read_text())
        if private.get("app") != "com.kenigevents.mymediabank" or not keystore.is_file():
            raise RuntimeError("Existing app signer is incomplete; inspect without replacing it")
        password = str(private["password"])
    else:
        if keystore.exists():
            raise RuntimeError("Existing keystore lacks its private config; refusing to overwrite it")
        password = secrets.token_urlsafe(48)
        PRIVATE_VALUES.append(password)
        private_env = {**env, "MY_MEDIABANK_SIGNING_PASSWORD": password}
        run("create dedicated persistent My MediaBank release signer", [str(java / "bin/keytool"),
            "-genkeypair", "-noprompt", "-alias", "my-mediabank", "-keyalg", "RSA", "-keysize", "3072",
            "-validity", "10000", "-storetype", "PKCS12", "-keystore", str(keystore),
            "-storepass:env", "MY_MEDIABANK_SIGNING_PASSWORD", "-keypass:env", "MY_MEDIABANK_SIGNING_PASSWORD",
            "-dname", "CN=My MediaBank, OU=Android, O=KenigEvents"], private_env, quiet=True, timeout=60)
        keystore.chmod(0o600)
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump({"app": "com.kenigevents.mymediabank", "alias": "my-mediabank", "password": password}, output)
    config.chmod(0o600)
    keystore.chmod(0o600)
    PRIVATE_VALUES.append(password)
    env.update(ANDROID_KEYSTORE_PATH=str(keystore), ANDROID_KEYSTORE_PASSWORD=password,
               ANDROID_KEY_ALIAS="my-mediabank", ANDROID_KEY_PASSWORD=password)
    log("signing: persistent app-specific release identity ready; private material remains outside source")


def verify(sdk: Path, apk: Path, env: dict[str, str], variant: str) -> dict:
    if not apk.is_file():
        raise RuntimeError("Expected installable APK was not produced")
    with zipfile.ZipFile(apk) as archive:
        if archive.testzip() is not None or not {"AndroidManifest.xml", "classes.dex"}.issubset(set(archive.namelist())):
            raise RuntimeError("APK archive content or CRC verification failed")
    tools = sdk / "build-tools/35.0.0"
    signature = run("verify APK signature for Android API 26+", [str(tools / "apksigner"), "verify", "--verbose",
                    "--print-certs", "--min-sdk-version", "26", str(apk)], env, quiet=True, timeout=60)
    badging = run("verify APK package and SDK metadata", [str(tools / "aapt"), "dump", "badging", str(apk)], env, quiet=True, timeout=60)
    package = re.search(r"package: name='([^']+)' versionCode='([^']+)' versionName='([^']+)'", badging)
    minimum = re.search(r"(?m)^sdkVersion:'(\d+)'", badging)
    target = re.search(r"(?m)^targetSdkVersion:'(\d+)'", badging)
    cert = re.search(r"Signer #1 certificate SHA-256 digest: (\S+)", signature)
    if not package or package[1] != "com.kenigevents.mymediabank" or not minimum or minimum[1] != "26" or not target or target[1] != "35" or not cert:
        raise RuntimeError("APK identity, Android compatibility, or signer metadata did not match this app")
    tests = failures = errors = skipped = 0
    for result in (ROOT / "android/app/build/test-results/testDebugUnitTest").glob("TEST-*.xml"):
        suite = ET.parse(result).getroot()
        tests += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
        skipped += int(suite.get("skipped", 0))
    if not tests or failures or errors:
        raise RuntimeError("Android unit test result evidence was absent or failing")
    with apk.open("rb") as binary:
        digest = hashlib.file_digest(binary, "sha256").hexdigest()
    return {"status": "verified", "variant": variant, "apk": str(apk.relative_to(ROOT)), "bytes": apk.stat().st_size,
        "sha256": digest, "package": package[1], "version_code": int(package[2]), "version_name": package[3],
        "min_sdk": 26, "target_sdk": 35, "signer_sha256": cert[1], "tests": {"count": tests, "failures": failures,
        "errors": errors, "skipped": skipped}, "physical_device_installation": "not_run", "verified_at_unix": int(time.time())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", action="store_true", help="Inspect bounded toolchain paths without installing or building")
    parser.add_argument("--debug-only", action="store_true", help="CI build without persistent release signing")
    parser.add_argument("--no-install", action="store_true", help="Require preinstalled pinned toolchain")
    parser.add_argument("--version-code", type=int, default=1)
    parser.add_argument("--version-name", default="0.1.0")
    args = parser.parse_args()
    if args.version_code < 1 or not re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", args.version_name):
        raise RuntimeError("Invalid app version")
    os.umask(0o077)
    env = dict(os.environ)
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "my-mediabank/android-build"
    check_disk(cache, 2 * GIB)
    java = java_home(cache, not args.no_install and not args.probe)
    env["JAVA_HOME"] = str(java)
    env["PATH"] = str(java / "bin") + os.pathsep + env.get("PATH", "")
    if args.probe:
        log(json.dumps({"java_home": str(java), "gradle_on_path": shutil.which("gradle"),
            "adb_on_path": shutil.which("adb"), "sdk_candidates": [{"path": str(path), "exists": path.is_dir(),
            "sdk35_complete": sdk_complete(path)} for path in sdk_candidates()], "cache": str(cache)}))
        return 0
    executable = gradle(cache, env, not args.no_install)
    sdk = android_sdk(cache, env, not args.no_install)
    env["ANDROID_HOME"] = env["ANDROID_SDK_ROOT"] = str(sdk)
    if not args.debug_only:
        signing(env, java)
    variant = "debug" if args.debug_only else "release"
    task = "assembleDebug" if args.debug_only else "assembleRelease"
    check_disk(ROOT / "android/app/build", 1536 * 1024**2)
    run("test and build Android APK", [str(executable), "-p", "android", "--no-daemon", "--console=plain",
        "--max-workers=2", "-Dorg.gradle.jvmargs=-Xmx1536m -Dfile.encoding=UTF-8", "testDebugUnitTest", task,
        f"-PversionCodeOverride={args.version_code}", f"-PversionNameOverride={args.version_name}"], env)
    apk = ROOT / f"android/app/build/outputs/apk/{variant}/app-{variant}.apk"
    evidence = verify(sdk, apk, env, variant)
    evidence_path = ROOT / "android/app/build/outputs/android-build-verification.json"
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n")
    log(json.dumps(evidence, indent=2))
    log(f"verification_artifact: {evidence_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        log(f"BUILD FAILED: {type(error).__name__}: {error}")
        raise SystemExit(1)
