#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP_USER = "10001:10001"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SIGNING_KEY_FINGERPRINT_PATTERN = re.compile(r"^[0-9A-F]{40}$")
LEGACY_BUILD_RECORDS = {
    name: f"/usr/share/fj-build/{name}"
    for name in (
        "ffmpeg-version.txt",
        "ffmpeg-buildconf.txt",
        "ffprobe-version.txt",
        "ffmpeg-debian-packages.tsv",
        "ffmpeg-dynamic-libraries.txt",
    )
}
APV_BUILD_RECORDS = {
    "ffmpeg-apv-buildconf.txt": ("/opt/ffmpeg-apv/share/ffmpeg-apv-buildconf.txt"),
    "ffmpeg-apv-source-sha256.txt": (
        "/opt/ffmpeg-apv/share/ffmpeg-apv-source-sha256.txt"
    ),
    "ffmpeg-apv-dynamic-libraries.txt": (
        "/opt/ffmpeg-apv/share/ffmpeg-apv-dynamic-libraries.txt"
    ),
}
APV_BINARIES = {
    "ffmpeg": "/opt/ffmpeg-apv/bin/ffmpeg",
    "ffprobe": "/opt/ffmpeg-apv/bin/ffprobe",
}
APV_REQUIRED_BUILD_FLAGS = frozenset(
    {
        "--disable-network",
        "--disable-autodetect",
        "--disable-everything",
        "--disable-cuda-llvm",
        "--disable-cuvid",
        "--disable-ffnvcodec",
        "--disable-nvdec",
        "--disable-nvenc",
        "--disable-vulkan",
        "--enable-decoder=apv",
        "--enable-encoder=mjpeg",
        "--enable-encoder=rawvideo",
        "--enable-demuxer=mov",
        "--enable-muxer=mp4",
        "--enable-muxer=image2",
        "--enable-muxer=rawvideo",
        "--enable-protocol=fd",
        "--enable-protocol=file",
        "--enable-protocol=pipe",
        "--enable-filter=select",
        "--enable-filter=scale",
        "--enable-filter=format",
    }
)
APV_FORBIDDEN_BUILD_FLAGS = frozenset(
    {
        "--enable-gpl",
        "--enable-nonfree",
        "--enable-liboapv",
        "--enable-cuda-llvm",
        "--enable-cuvid",
        "--enable-ffnvcodec",
        "--enable-nvdec",
        "--enable-nvenc",
        "--enable-vulkan",
    }
)
MACHINE_ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}


def _run(*args: str) -> bytes:
    return subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _normalized_sha256(value: object) -> str:
    normalized = str(value).removeprefix("sha256:")
    if SHA256_PATTERN.fullmatch(normalized) is None:
        raise RuntimeError("candidate lock contains an invalid SHA-256 value")
    return normalized


def _docker_exec(
    container: str,
    *command: str,
    user: str | None = None,
) -> bytes:
    arguments = ["docker", "exec"]
    if user is not None:
        arguments.extend(("--user", user))
    arguments.extend((container, *command))
    return _run(*arguments)


def _read_build_records(
    container: str,
    paths: dict[str, str],
) -> tuple[dict[str, bytes], dict[str, str]]:
    content_by_name: dict[str, bytes] = {}
    hashes: dict[str, str] = {}
    for name, path in paths.items():
        content = _docker_exec(container, "cat", path, user=APP_USER)
        if not content:
            raise RuntimeError(f"candidate build record is empty: {name}")
        content_by_name[name] = content
        hashes[name] = _sha256(content)
    return content_by_name, hashes


def _verify_license_materials(
    container: str,
    component: dict[str, Any],
) -> dict[str, str]:
    material_hashes: dict[str, str] = {}
    for material in component.get("license_materials", []):
        source = str(material["source"])
        prefix = "candidate-image:"
        if not source.startswith(prefix):
            raise RuntimeError("candidate license material source is invalid")
        container_content = _docker_exec(
            container,
            "cat",
            source.removeprefix(prefix),
            user=APP_USER,
        )
        digest = _sha256(container_content)
        expected = _normalized_sha256(material["sha256"])
        bundled = ROOT / str(material["path"])
        if digest != expected or _sha256(bundled.read_bytes()) != expected:
            raise RuntimeError(
                f"candidate {component['name']} license material mismatch"
            )
        material_hashes[str(material["path"])] = digest
    return material_hashes


def _verify_legacy_ffmpeg(
    container: str,
    component: dict[str, Any],
) -> dict[str, object]:
    record_content, record_hashes = _read_build_records(
        container,
        LEGACY_BUILD_RECORDS,
    )
    buildconf = record_content["ffmpeg-buildconf.txt"].decode("utf-8")
    packages = record_content["ffmpeg-debian-packages.tsv"].decode("utf-8")
    dynamic_libraries = record_content["ffmpeg-dynamic-libraries.txt"].decode("utf-8")
    if "--enable-gpl" not in buildconf or "--enable-nonfree" in buildconf:
        raise RuntimeError("candidate FFmpeg GPL/nonfree classification mismatch")
    if f"ffmpeg\t{component['version']}" not in packages:
        raise RuntimeError("candidate FFmpeg Debian package version mismatch")
    if "not found" in dynamic_libraries.lower():
        raise RuntimeError("candidate FFmpeg has an unresolved dynamic library")
    for binary_name in ("ffmpeg", "ffprobe"):
        binary_path = f"/usr/bin/{binary_name}"
        _docker_exec(container, "test", "-x", binary_path, user=APP_USER)
        version_output = _docker_exec(
            container,
            binary_path,
            "-version",
            user=APP_USER,
        ).decode("utf-8")
        if f"{binary_name} version" not in version_output:
            raise RuntimeError(f"candidate {binary_name} version output is invalid")
    return {
        "version": component["version"],
        "build_record_sha256": record_hashes,
        "license_material_sha256": _verify_license_materials(
            container,
            component,
        ),
    }


def _verify_apv_ffmpeg(
    container: str,
    component: dict[str, Any],
) -> dict[str, object]:
    record_content, record_hashes = _read_build_records(
        container,
        APV_BUILD_RECORDS,
    )
    buildconf = record_content["ffmpeg-apv-buildconf.txt"].decode("utf-8")
    if not all(flag in buildconf for flag in APV_REQUIRED_BUILD_FLAGS) or any(
        flag in buildconf for flag in APV_FORBIDDEN_BUILD_FLAGS
    ):
        raise RuntimeError("candidate APV FFmpeg capability or license flags drifted")
    if record_content["ffmpeg-apv-source-sha256.txt"].decode(
        "ascii"
    ).strip() != _normalized_sha256(component["source_sha256"]):
        raise RuntimeError("candidate APV FFmpeg source hash record mismatch")
    dynamic_libraries = record_content["ffmpeg-apv-dynamic-libraries.txt"].decode(
        "utf-8"
    )
    if not dynamic_libraries.strip() or "not found" in dynamic_libraries.lower():
        raise RuntimeError("candidate APV FFmpeg dynamic-library record is invalid")

    machine = (
        _docker_exec(container, "uname", "-m", user=APP_USER).decode("ascii").strip()
    )
    architecture = MACHINE_ARCHITECTURES.get(machine)
    if architecture is None:
        raise RuntimeError("candidate APV FFmpeg architecture is unsupported")
    binary_artifact_hashes = component.get("binary_artifact_hashes")
    if not isinstance(binary_artifact_hashes, dict):
        raise RuntimeError("candidate APV FFmpeg binary hash inventory is incomplete")
    architecture_hashes = binary_artifact_hashes.get(architecture)
    if not isinstance(architecture_hashes, dict) or set(architecture_hashes) != set(
        APV_BINARIES
    ):
        raise RuntimeError("candidate APV FFmpeg binary hash inventory is incomplete")

    binary_hashes: dict[str, str] = {}
    for binary_name, binary_path in APV_BINARIES.items():
        _docker_exec(container, "test", "-x", binary_path, user=APP_USER)
        version_output = _docker_exec(
            container,
            binary_path,
            "-version",
            user=APP_USER,
        ).decode("utf-8")
        if f"{binary_name} version {component['version']}" not in version_output:
            raise RuntimeError(f"candidate APV {binary_name} version mismatch")
        digest_output = (
            _docker_exec(
                container,
                "sha256sum",
                binary_path,
                user=APP_USER,
            )
            .decode("ascii")
            .split()
        )
        if not digest_output or SHA256_PATTERN.fullmatch(digest_output[0]) is None:
            raise RuntimeError(f"candidate APV {binary_name} hash output is invalid")
        actual_digest = digest_output[0]
        if actual_digest != _normalized_sha256(architecture_hashes[binary_name]):
            raise RuntimeError(f"candidate APV {binary_name} binary hash mismatch")
        binary_hashes[binary_name] = actual_digest

    decoder_output = _docker_exec(
        container,
        APV_BINARIES["ffmpeg"],
        "-hide_banner",
        "-decoders",
        user=APP_USER,
    ).decode("utf-8")
    if re.search(r"(?m)^\s*V\S*\s+apv\s", decoder_output) is None:
        raise RuntimeError("candidate APV FFmpeg decoder is absent")
    if (
        SIGNING_KEY_FINGERPRINT_PATTERN.fullmatch(
            str(component.get("signing_key_fingerprint", ""))
        )
        is None
    ):
        raise RuntimeError("candidate APV signing-key fingerprint is invalid")

    return {
        "version": component["version"],
        "source_sha256": _normalized_sha256(component["source_sha256"]),
        "signature_sha256": _normalized_sha256(component["signature_sha256"]),
        "signing_key_fingerprint": component["signing_key_fingerprint"],
        "architecture": architecture,
        "binary_sha256": binary_hashes,
        "build_record_sha256": record_hashes,
        "license_material_sha256": _verify_license_materials(
            container,
            component,
        ),
    }


def verify(container: str) -> dict[str, object]:
    lock = json.loads(
        (ROOT / "compliance" / "component-lock.json").read_text(encoding="utf-8")
    )
    component_items = lock.get("components")
    if not isinstance(component_items, list):
        raise RuntimeError("candidate component lock is invalid")
    components = {component["name"]: component for component in component_items}
    if len(components) != len(component_items):
        raise RuntimeError("candidate component lock contains duplicate names")
    ffmpeg = components["ffmpeg"]
    ffmpeg_apv = components["ffmpeg-apv"]
    expected_image_id = str(lock.get("candidate_backend_image_digest", ""))
    if (
        ffmpeg.get("candidate_image_digest") != expected_image_id
        or ffmpeg_apv.get("candidate_image_digest") != expected_image_id
        or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image_id) is None
    ):
        raise RuntimeError("both FFmpeg runtimes must bind one candidate image")
    image_id = (
        _run("docker", "inspect", container, "--format", "{{.Image}}")
        .decode("ascii")
        .strip()
    )
    if image_id != expected_image_id:
        raise RuntimeError(
            "running candidate image digest does not match component-lock.json"
        )

    return {
        "status": "PASS_CANDIDATE_IMAGE",
        "container": container,
        "image_digest": image_id,
        "runtimes": {
            "ffmpeg": _verify_legacy_ffmpeg(container, ffmpeg),
            "ffmpeg-apv": _verify_apv_ffmpeg(container, ffmpeg_apv),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--container", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.container), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
