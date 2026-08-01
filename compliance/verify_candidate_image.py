#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_RECORDS = (
    "ffmpeg-version.txt",
    "ffmpeg-buildconf.txt",
    "ffprobe-version.txt",
    "ffmpeg-debian-packages.tsv",
    "ffmpeg-dynamic-libraries.txt",
)


def _run(*args: str) -> bytes:
    return subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def verify(container: str) -> dict[str, object]:
    lock = json.loads(
        (ROOT / "compliance" / "component-lock.json").read_text(encoding="utf-8")
    )
    ffmpeg = next(
        component
        for component in lock["components"]
        if component["name"] == "ffmpeg"
    )
    image_id = _run(
        "docker", "inspect", container, "--format", "{{.Image}}"
    ).decode("ascii").strip()
    if image_id != ffmpeg["candidate_image_digest"]:
        raise RuntimeError(
            "running candidate image digest does not match component-lock.json"
        )

    records: dict[str, str] = {}
    record_content: dict[str, bytes] = {}
    for name in BUILD_RECORDS:
        content = _run(
            "docker", "exec", container, "cat", f"/usr/share/fj-build/{name}"
        )
        if not content:
            raise RuntimeError(f"candidate build record is empty: {name}")
        record_content[name] = content
        records[name] = _sha256(content)

    buildconf = record_content["ffmpeg-buildconf.txt"].decode("utf-8")
    packages = record_content["ffmpeg-debian-packages.tsv"].decode("utf-8")
    if "--enable-gpl" not in buildconf or "--enable-nonfree" in buildconf:
        raise RuntimeError("candidate FFmpeg GPL/nonfree classification mismatch")
    if f"ffmpeg\t{ffmpeg['version']}" not in packages:
        raise RuntimeError("candidate FFmpeg Debian package version mismatch")

    material_hashes: dict[str, str] = {}
    for material in ffmpeg["license_materials"]:
        source = str(material["source"])
        prefix = "candidate-image:"
        if not source.startswith(prefix):
            raise RuntimeError("candidate license material source is invalid")
        container_content = _run(
            "docker", "exec", container, "cat", source.removeprefix(prefix)
        )
        digest = _sha256(container_content)
        expected = str(material["sha256"]).removeprefix("sha256:")
        bundled = ROOT / str(material["path"])
        if digest != expected or _sha256(bundled.read_bytes()) != expected:
            raise RuntimeError("candidate FFmpeg license material mismatch")
        material_hashes[str(material["path"])] = digest

    return {
        "status": "PASS_CANDIDATE_IMAGE",
        "container": container,
        "image_digest": image_id,
        "build_record_sha256": records,
        "license_material_sha256": material_hashes,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.container), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
