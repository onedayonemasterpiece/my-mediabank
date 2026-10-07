#!/usr/bin/env python3
"""Publish only the verified My MediaBank 0.1.0 APK to its fixed public repository.

Run after the owner-authorized source commit. Runtime limitations are stated
explicitly in the prerelease notes:
    python3 scripts/publish-apk.py --source-sha <exact-40-character-HEAD>

--dry-run prepares the ignored release files and reads GitHub state, without
creating a release or uploading anything. Existing assets are never replaced
or deleted. An interrupted upload may resume only missing, verified assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
REPO = "onedayonemasterpiece/my-mediabank"
TAG = "v0.1.0"
APK_NAME = "my-mediabank-0.1.0.apk"
APK_SHA256 = "b5e0044d5a128c508598d131e9adf4dc455de4fbd0227d154582d41bd1286e5f"
SIGNER_SHA256 = "afdb067e59187287e237e87887ce31c9a991c4950ffb352d97b3dc1c5b711162"
SOURCE_APK = ROOT / "android/app/build/outputs/apk/release/app-release.apk"
BUILD_EVIDENCE = ROOT / "android/app/build/outputs/android-build-verification.json"
RELEASE_DIR = ROOT / "build/release"


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def invoke(command: list[str], env: dict[str, str], label: str, *, allow_failure=False) -> subprocess.CompletedProcess:
    # Do not echo environment, credentials, command diagnostics or API response bodies.
    result = subprocess.run(command, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=180)
    if result.returncode and not allow_failure:
        raise RuntimeError(f"{label} failed (exit {result.returncode}); inspect authenticated tool status")
    return result


def source_guard(source_sha: str, git: str, env: dict[str, str]) -> str:
    head = invoke([git, "rev-parse", "HEAD"], env, "read HEAD").stdout.strip()
    if head != source_sha:
        raise RuntimeError("HEAD does not equal the explicitly supplied source SHA")
    dirty = invoke([git, "status", "--porcelain=v1", "--untracked-files=all", "--", "android"],
                   env, "check Android source tree").stdout
    if dirty.strip():
        raise RuntimeError("Android source tree differs from HEAD; refusing to publish an ambiguous build")
    tree = invoke([git, "rev-parse", f"{source_sha}:android"], env, "read Android source tree hash").stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", tree):
        raise RuntimeError("The source commit does not contain a valid Android tree")
    ignored = invoke([git, "check-ignore", "--quiet", "--", "build/release"], env,
                     "check ignored release directory", allow_failure=True)
    if ignored.returncode != 0:
        raise RuntimeError("build/release must be Git-ignored before staging release files")
    return tree


def android_tools(env: dict[str, str]) -> tuple[Path, Path]:
    if not shutil.which("java", path=env.get("PATH")):
        cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "my-mediabank/android-build/temurin-17"
        marker = json.loads((cache / "java-home.json").read_text())
        relative = Path(marker.get("directory", ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("Invalid app-owned JDK marker")
        java = cache / relative
        if not (java / "bin/java").is_file():
            raise RuntimeError("The app build JDK is unavailable for APK verification")
        env["JAVA_HOME"] = str(java)
        env["PATH"] = str(java / "bin") + os.pathsep + env.get("PATH", "")
    candidates = [value for value in [env.get("ANDROID_HOME"), env.get("ANDROID_SDK_ROOT"),
                  str(Path.home() / ".local/share/my-mediabank/android-sdk"),
                  "/home/dev/Android/Sdk", "/home/dev/.local/share/android-sdk",
                  "/opt/android-sdk", "/usr/lib/android-sdk"] if value]
    for value in candidates:
        tools = Path(value) / "build-tools/35.0.0"
        if (tools / "apksigner").is_file() and (tools / "aapt").is_file():
            return tools / "apksigner", tools / "aapt"
    raise RuntimeError("Android build-tools 35.0.0 are unavailable for release verification")


def validate_apk(env: dict[str, str]) -> dict:
    if SOURCE_APK.is_symlink() or BUILD_EVIDENCE.is_symlink() or not SOURCE_APK.is_file() or not BUILD_EVIDENCE.is_file():
        raise RuntimeError("Verified APK and build evidence must be regular local build files")
    evidence = json.loads(BUILD_EVIDENCE.read_text())
    expected = {"status": "verified", "variant": "release", "package": "com.kenigevents.mymediabank",
                "version_code": 1, "version_name": "0.1.0", "min_sdk": 26, "target_sdk": 35,
                "sha256": APK_SHA256, "signer_sha256": SIGNER_SHA256,
                "physical_device_installation": "not_run"}
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise RuntimeError("Build evidence does not describe the approved 0.1.0 release APK")
    if SOURCE_APK.stat().st_size != evidence.get("bytes") or digest(SOURCE_APK) != APK_SHA256:
        raise RuntimeError("APK size or SHA256 differs from the verified artifact")
    expected_tests = {"count": 6, "failures": 0, "errors": 0, "skipped": 0}
    if evidence.get("tests") != expected_tests:
        raise RuntimeError("Build evidence must contain six successful Android unit tests")
    actual = {"count": 0, "failures": 0, "errors": 0, "skipped": 0}
    for report in (ROOT / "android/app/build/test-results/testDebugUnitTest").glob("TEST-*.xml"):
        suite = ET.parse(report).getroot()
        for target, source in [("count", "tests"), ("failures", "failures"), ("errors", "errors"), ("skipped", "skipped")]:
            actual[target] += int(suite.get(source, 0))
    if actual != expected_tests:
        raise RuntimeError("Local JUnit reports do not match successful build evidence")
    with zipfile.ZipFile(SOURCE_APK) as archive:
        if archive.testzip() is not None or not {"classes.dex", "AndroidManifest.xml"}.issubset(set(archive.namelist())):
            raise RuntimeError("APK ZIP content or CRC verification failed")
    signer, aapt = android_tools(env)
    verified = invoke([str(signer), "verify", "--verbose", "--print-certs", "--min-sdk-version", "26", str(SOURCE_APK)],
                      env, "verify APK signature").stdout
    match = re.search(r"Signer #1 certificate SHA-256 digest: (\S+)", verified)
    if not match or match[1].lower() != SIGNER_SHA256:
        raise RuntimeError("APK signer does not match the dedicated My MediaBank release identity")
    badging = invoke([str(aapt), "dump", "badging", str(SOURCE_APK)], env, "verify Android package metadata").stdout
    if not re.search(r"package: name='com\.kenigevents\.mymediabank' versionCode='1' versionName='0\.1\.0'", badging):
        raise RuntimeError("APK package/version metadata differs from the expected release")
    if not re.search(r"(?m)^sdkVersion:'26'$", badging) or not re.search(r"(?m)^targetSdkVersion:'35'$", badging):
        raise RuntimeError("APK Android SDK compatibility metadata is unexpected")
    return evidence


def write_once(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise RuntimeError("Release staging files must not be symlinks")
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError(f"Existing staged file differs: {path.name}; it will not be overwritten")
        return
    with path.open("xb") as target:
        target.write(content)


def stage(evidence: dict, source_sha: str, android_tree: str) -> tuple[list[Path], Path]:
    if not RELEASE_DIR.resolve().is_relative_to(ROOT):
        raise RuntimeError("Release directory must remain inside this repository's ignored build output")
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)
    apk = RELEASE_DIR / APK_NAME
    write_once(apk, SOURCE_APK.read_bytes())
    public_evidence = {"repository": REPO, "tag": TAG, "source_sha": source_sha, "android_git_tree": android_tree,
        "apk": APK_NAME, "status": "verified", "variant": "release", "bytes": evidence["bytes"],
        "sha256": APK_SHA256, "signer_sha256": SIGNER_SHA256, "package": "com.kenigevents.mymediabank",
        "version_code": 1, "version_name": "0.1.0", "min_sdk": 26, "target_sdk": 35,
        "tests": evidence["tests"], "physical_device_installation": "not_run",
        "verified_at_unix": evidence["verified_at_unix"]}
    report = RELEASE_DIR / "my-mediabank-0.1.0-verification.json"
    write_once(report, (json.dumps(public_evidence, indent=2, sort_keys=True) + "\n").encode())
    checksums = RELEASE_DIR / "my-mediabank-0.1.0-SHA256SUMS.txt"
    write_once(checksums, (f"{APK_SHA256}  {APK_NAME}\n{digest(report)}  {report.name}\n").encode())
    notes = RELEASE_DIR / "my-mediabank-0.1.0-notes.md"
    text = f"""## My MediaBank 0.1.0 — первый тест Миры

Чёрный экран с одной фотографией, просмотр от новых снимков к старым и разговор с Мирой через сервер по WSS. Мира описывает изображение и предлагает оценку открыточности от 1 до 10.

**Статус этой сборки:** APK собран и подписан. Подключение публичного сервера и заключительная проверка Live ещё не завершены; готовность к тесту на телефоне будет подтверждена отдельно.

- Подписанный APK: Android 8.0 и новее.
- Фотографии пока не удаляются, не архивируются и не публикуются в Telegram.
- Шесть Android-тестов прошли; подпись APK, его SHA256 и целостность архива проверены.
- Установка и работа на физическом телефоне пока не проверены. Качество оценок предстоит проверить на реальных снимках.

Исходный commit: `{source_sha}`.
"""
    write_once(notes, text.encode())
    return [apk, checksums, report], notes


class GitHub:
    def __init__(self, executable: str, env: dict[str, str]):
        self.executable = executable
        self.env = env

    def command(self, arguments: list[str], label: str, *, allow_failure=False):
        return invoke([self.executable, *arguments], self.env, label, allow_failure=allow_failure)

    def api(self, suffix: str, *, missing_ok=False):
        response = self.command(["api", f"repos/{REPO}/{suffix}"], "read fixed repository metadata", allow_failure=True)
        if response.returncode:
            if missing_ok and "HTTP 404" in response.stderr:
                return None
            raise RuntimeError(f"GitHub read failed (exit {response.returncode}); no publication was attempted by this read")
        return json.loads(response.stdout)

    def release_view(self):
        response = self.command(["release", "view", TAG, "--repo", REPO, "--json",
            "tagName,isPrerelease,isDraft,targetCommitish,url,assets"], "inspect release", allow_failure=True)
        if response.returncode:
            if response.returncode == 1 and response.stderr.strip().lower() == "release not found":
                return None
            if "HTTP 404" in response.stderr:
                return None
            raise RuntimeError(f"Release inspection failed (exit {response.returncode}); refusing to infer absence")
        return json.loads(response.stdout)

    def tag_commit(self, *, missing_ok=False):
        reference = self.api(f"git/ref/tags/{TAG}", missing_ok=missing_ok)
        if reference is None:
            return None
        target = reference["object"]
        for _ in range(4):
            if target.get("type") == "commit":
                return target["sha"]
            if target.get("type") != "tag" or not re.fullmatch(r"[0-9a-f]{40}", target.get("sha", "")):
                break
            target = self.api(f"git/tags/{target['sha']}")["object"]
        raise RuntimeError("Release tag does not resolve to a bounded commit reference")

    def verify_remote(self, source_sha: str, assets: list[Path]) -> tuple[dict, list[Path]]:
        view = self.release_view()
        if view is None:
            raise RuntimeError("Release is not visible during readback")
        if view.get("tagName") != TAG or not view.get("isPrerelease") or view.get("isDraft"):
            raise RuntimeError("Existing release is not the expected public prerelease")
        if self.tag_commit() != source_sha:
            raise RuntimeError("Existing release tag points to a different commit; it will not be moved")
        release = self.api(f"releases/tags/{TAG}")
        remote_assets = {asset["name"]: asset for asset in release.get("assets", [])}
        present = [path for path in assets if path.name in remote_assets]
        missing = [path for path in assets if path.name not in remote_assets]
        for path in present:
            remote = remote_assets[path.name]
            if remote.get("size") != path.stat().st_size or remote.get("state") != "uploaded":
                raise RuntimeError(f"Existing release asset size/state differs: {path.name}")
            advertised_digest = remote.get("digest")
            if advertised_digest and advertised_digest != "sha256:" + digest(path):
                raise RuntimeError(f"Existing release asset digest differs: {path.name}")
        if present:
            # Download only our named assets into a fresh ignored directory. Preserve
            # readback evidence; never request source archives or unrelated assets.
            directory = Path(tempfile.mkdtemp(prefix="readback-", dir=RELEASE_DIR))
            arguments = ["release", "download", TAG, "--repo", REPO, "--dir", str(directory)]
            for path in present:
                arguments.extend(["--pattern", path.name])
            self.command(arguments, "download own release assets for readback")
            for path in present:
                received = directory / path.name
                if not received.is_file() or received.stat().st_size != path.stat().st_size or digest(received) != digest(path):
                    raise RuntimeError(f"Downloaded release asset failed byte verification: {path.name}")
        return release, missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        raise RuntimeError("--source-sha must be the full exact lowercase commit SHA")
    git = shutil.which("git")
    executable = shutil.which("gh")
    if not git or not executable:
        raise RuntimeError("Existing git and gh CLI installations are required")
    env = dict(os.environ)
    env.pop("GH_DEBUG", None)
    env.pop("GH_TRACE", None)
    env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1", GH_PAGER="cat")
    android_tree = source_guard(args.source_sha, git, env)
    evidence = validate_apk(env)
    assets, notes = stage(evidence, args.source_sha, android_tree)
    github = GitHub(executable, env)
    # The first GitHub operation is read-only release inspection.
    existing = github.release_view()
    repository = github.api("")
    if repository.get("full_name") != REPO or repository.get("private") is not False:
        raise RuntimeError("Target repository is not the explicitly authorized public My MediaBank repository")
    if github.api(f"commits/{args.source_sha}").get("sha") != args.source_sha:
        raise RuntimeError("The exact source commit is not present in the target GitHub repository")
    tag_commit = github.tag_commit(missing_ok=True)
    if tag_commit is not None and tag_commit != args.source_sha:
        raise RuntimeError("Existing v0.1.0 tag points elsewhere; it will not be changed")
    missing = assets
    release = None
    if existing:
        release, missing = github.verify_remote(args.source_sha, assets)
    if args.dry_run:
        print(json.dumps({"status": "ready", "dry_run": True, "repository": REPO, "tag": TAG,
            "source_sha": args.source_sha, "release_exists": existing is not None,
            "missing_assets": [path.name for path in missing], "notes_file": str(notes.relative_to(ROOT)),
            "assets": [{"name": path.name, "bytes": path.stat().st_size, "sha256": digest(path)} for path in assets]}, indent=2))
        return 0
    source_guard(args.source_sha, git, env)
    if existing is None:
        created = github.command(["release", "create", TAG, *[str(path) for path in assets], "--repo", REPO,
            "--target", args.source_sha, "--prerelease", "--latest=false", "--title", "My MediaBank 0.1.0",
            "--notes-file", str(notes)], "create fixed prerelease", allow_failure=True)
        # A lost response can still have created a release or uploaded some assets.
        # Read that same release back before deciding whether anything remains.
        if created.returncode and github.release_view() is None:
            raise RuntimeError(f"Release creation failed (exit {created.returncode}); no visible release to reconcile")
        release, missing = github.verify_remote(args.source_sha, assets)
    if missing:
        source_guard(args.source_sha, git, env)
        # No overwrite flag: only absent names are resumed after all existing bytes
        # and the tag's exact commit have already been verified.
        github.command(["release", "upload", TAG, *[str(path) for path in missing], "--repo", REPO],
                       "upload missing verified assets", allow_failure=True)
        release, missing = github.verify_remote(args.source_sha, assets)
    if missing:
        raise RuntimeError("Release exists but verified assets remain missing; rerun the same command to resume")
    if release is None:
        raise RuntimeError("Release verification did not return a verified publication")
    remote = {asset["name"]: asset for asset in release["assets"]}
    print(json.dumps({"status": "published_and_verified", "repository": REPO, "tag": TAG,
        "source_sha": args.source_sha, "release_url": release["html_url"], "prerelease": True,
        "assets": [{"name": path.name, "bytes": path.stat().st_size, "sha256": digest(path),
                    "url": remote[path.name]["browser_download_url"]} for path in assets],
        "physical_device_installation": "not_run"}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Our exception messages contain only controlled status and artifact names.
        # Never print a subprocess command, environment or authentication response.
        print(f"PUBLISH FAILED: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
