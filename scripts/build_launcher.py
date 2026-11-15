#!/usr/bin/env python3
"""Build the single-file launcher, `dist/vacation-memories`, with PyInstaller (UI-SPEC.md, D15).

Run it only in the `launcher-build` container: `docker compose run --rm -T launcher-build`. The executable embeds
the compose files that `scripts/launcher.py` lists in PAYLOAD_FILES, read from the repository at build time, and a
`dist_config.json` with the Docker Hub repository of the runtime image (IMAGE_REPO) and the version (APP_VERSION).
Publishing the image is up to the user (`docker push`), this script never talks to a registry.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import launcher  # noqa: E402  (the module the executable is built from)

NAME = "vacation-memories"
IMAGE_REPO = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+$")
VERSION = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


def stage_payload(destination: Path, image: str | None, version: str) -> None:
    for name in launcher.PAYLOAD_FILES:
        source = ROOT / name
        if not source.is_file():
            raise SystemExit(f"missing file for the executable: {name}")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (destination / launcher.DIST_CONFIG).write_text(
        json.dumps({"image": image, "version": version}, indent=2) + "\n", encoding="utf-8")


def smoke_test(executable: Path, version: str) -> None:
    with tempfile.TemporaryDirectory() as home:
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "TMPDIR": home}
        output = subprocess.run([str(executable), "--version"], env=env, capture_output=True, text=True, check=True)
        if output.stdout.strip() != version:
            raise SystemExit(f"--version printed {output.stdout.strip()!r}, expected {version!r}")
        check = subprocess.run([str(executable), "--self-check"], env=env, capture_output=True, text=True)
        if check.returncode != 0:
            raise SystemExit(f"--self-check failed:\n{check.stdout}{check.stderr}")
        subprocess.run([str(executable), "--help"], env=env, capture_output=True, text=True, check=True)
        print(check.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image-repo", default=os.environ.get("IMAGE_REPO", ""),
                        help="Docker Hub repository of the runtime image, e.g. namespace/vacation-memories "
                             "(default: $IMAGE_REPO)")
    parser.add_argument("--version", default=os.environ.get("APP_VERSION") or "dev",
                        help="image tag and version of the executable (default: $APP_VERSION or dev)")
    parser.add_argument("--local-image", action="store_true",
                        help="build a launcher that never pulls (it expects vacation-memories:local), for tests")
    args = parser.parse_args()

    if args.local_image:
        image = None
    elif IMAGE_REPO.match(args.image_repo):
        image = args.image_repo
    else:
        print("Set IMAGE_REPO to the Docker Hub repository of the image (namespace/vacation-memories), "
              "or pass --local-image.", file=sys.stderr)
        return 2
    if not VERSION.match(args.version):
        print(f"invalid version: {args.version!r}", file=sys.stderr)
        return 2

    dist = ROOT / "dist"
    with tempfile.TemporaryDirectory() as work:
        work_dir = Path(work)
        stage = work_dir / "payload"
        stage_payload(stage, image, args.version)
        command = [
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--log-level", "WARN",
            "--name", NAME, "--distpath", str(dist), "--workpath", str(work_dir / "build"),
            "--specpath", str(work_dir), "--add-data", f"{stage}:payload", "--exclude-module", "tkinter",
            str(ROOT / "scripts" / "launcher.py"),
        ]
        subprocess.run(command, check=True)
    executable = dist / NAME
    smoke_test(executable, args.version)
    print(f"built {executable} ({executable.stat().st_size // 1024} KiB), image "
          f"{image + ':' + args.version if image else 'vacation-memories:local (no pull)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
