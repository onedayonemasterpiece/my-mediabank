"""Reproducible build setup from the already authorized local shared repositories.

Public framework archives are versioned deliverables. Private SDK source is
installed host-side only and is never copied into this public repository.
The build/ directory is an explicit repository-owned dependency build output.
"""
import argparse
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.3.27"
LIVE_SHA = "557532aa6277b5fc06424eb171d99e96f1445387"
RESOURCE_SHA = "d1af03b174be71b829b518306fd68d4acb8e779c"


def run(args, cwd=ROOT):
    subprocess.run([str(x) for x in args], cwd=cwd, check=True, timeout=600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-source", type=Path, default=Path("/home/dev/projects/live-interaction"))
    parser.add_argument("--resource-source", type=Path, default=Path("/home/dev/projects/ai-resource-control"))
    args = parser.parse_args()
    build = ROOT / "build" / "dependencies"
    build.mkdir(parents=True, exist_ok=True)
    vendor = ROOT / "vendor"
    vendor.mkdir(exist_ok=True)
    for name, source, sha in (("live", args.live_source, LIVE_SHA), ("resource", args.resource_source, RESOURCE_SHA)):
        # Exact Git objects, not mutable worktree contents.
        raw = subprocess.check_output(["git", "-C", str(source), "archive", "--format=tar", sha], timeout=30)
        target = build / name
        target.mkdir(exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            archive.extractall(target, filter="data")
        if name == "live":
            manifest = json.loads((target / "package.json").read_text())
            if manifest["version"] != VERSION:
                raise RuntimeError("Framework source version mismatch")
            run(["npm", "pack", "--pack-destination", vendor], cwd=target)
            packed = vendor / f"onedayonemasterpiece-live-interaction-{VERSION}.tgz"
            archive_path = vendor / f"live-interaction-{VERSION}.tgz"
            packed.replace(archive_path)
            python_archive = vendor / f"live-interaction-{VERSION}-py.tar.gz"
            python_archive.write_bytes(gzip.compress(raw, mtime=0))
            receipt = {"version": VERSION, "source_commit": LIVE_SHA,
                       "source_repository": "onedayonemasterpiece/live-interaction",
                       "sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                       "python_sha256": hashlib.sha256(python_archive.read_bytes()).hexdigest()}
            (vendor / f"live-interaction-{VERSION}.json").write_text(json.dumps(receipt, indent=2) + "\n")
    environment = ROOT / ".venv"
    if not (environment / "bin/python").exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / "bin/python"
    run([python, "-m", "pip", "install", vendor / f"live-interaction-{VERSION}-py.tar.gz"])
    run([python, "-m", "pip", "install", build / "resource"])
    run([python, "-m", "pip", "install", "-e", ".[test]"])
    run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund"])
    run(["npm", "run", "build"])
    run([python, "-m", "unittest", "discover", "-s", "tests", "-v"])
    print(json.dumps({"status": "built_and_tested", "framework": VERSION, "resource_source": RESOURCE_SHA}))


if __name__ == "__main__":
    main()
