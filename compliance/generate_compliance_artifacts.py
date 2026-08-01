#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import re
import urllib.parse
import uuid
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from packaging.requirements import Requirement
from packaging.utils import NormalizedName, canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
COMPLIANCE_ROOT = ROOT / "compliance"
SBOM_ROOT = COMPLIANCE_ROOT / "sbom"
LICENSE_ROOT = COMPLIANCE_ROOT / "license-texts"
PYTHON_SITE_PACKAGES = ROOT / "backend" / ".venv" / "lib" / "python3.13" / "site-packages"
LICENSE_NAME = re.compile(r"^(licen[cs]e|copying|notice|copyright)(?:[._-].*)?$", re.IGNORECASE)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
APACHE_INFRA_COMPONENTS = frozenset({"prometheus", "cadvisor"})
APACHE_UPSTREAMS = {
    "prometheus": (
        "https://github.com/prometheus/prometheus",
        "https://raw.githubusercontent.com/prometheus/prometheus",
    ),
    "cadvisor": (
        "https://github.com/google/cadvisor",
        "https://raw.githubusercontent.com/google/cadvisor",
    ),
}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _license_expression(metadata: importlib.metadata.PackageMetadata) -> str:
    expression = metadata.get("License-Expression")
    if expression:
        return expression.strip()
    value = metadata.get("License", "").strip()
    if value and "\n" not in value and len(value) <= 128:
        return value
    classifiers = metadata.get_all("Classifier", [])
    licenses = [item.rsplit("::", 1)[-1].strip() for item in classifiers if "License ::" in item]
    return " OR ".join(sorted(set(licenses))) if licenses else "NOASSERTION"


def _direct_python_requirements() -> list[Requirement]:
    result: list[Requirement] = []
    for line in (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            result.append(Requirement(stripped))
    return result


class DistributionLike(Protocol):
    requires: list[str] | None


def _resolved_python_names(
    installed: Mapping[NormalizedName, DistributionLike],
    roots: list[Requirement],
) -> list[NormalizedName]:
    queue: deque[tuple[NormalizedName, frozenset[str]]] = deque(
        (canonicalize_name(requirement.name), frozenset(requirement.extras))
        for requirement in roots
    )
    visited: set[tuple[NormalizedName, frozenset[str]]] = set()
    selected: set[NormalizedName] = set()
    while queue:
        state = queue.popleft()
        if state in visited:
            continue
        visited.add(state)
        name, extras = state
        if name not in installed:
            raise RuntimeError(f"required Python distribution is not installed: {name}")
        selected.add(name)
        environments = [{"extra": ""}, *({"extra": extra} for extra in sorted(extras))]
        for raw_requirement in installed[name].requires or ():
            requirement = Requirement(raw_requirement)
            if requirement.marker and not any(
                requirement.marker.evaluate(environment) for environment in environments
            ):
                continue
            queue.append((canonicalize_name(requirement.name), frozenset(requirement.extras)))
    return sorted(selected)


def _python_dependency_closure() -> list[importlib.metadata.Distribution]:
    installed = {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in importlib.metadata.distributions(path=[str(PYTHON_SITE_PACKAGES)])
        if distribution.metadata.get("Name")
    }
    names = _resolved_python_names(
        cast(Mapping[NormalizedName, DistributionLike], installed),
        _direct_python_requirements(),
    )
    return [installed[name] for name in names]


def _npm_name(package_path: str) -> str:
    suffix = package_path.rsplit("node_modules/", 1)[-1]
    parts = suffix.split("/")
    return "/".join(parts[:2]) if parts[0].startswith("@") else parts[0]


def _npm_components() -> tuple[list[dict[str, Any]], dict[str, Path]]:
    lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    components: list[dict[str, Any]] = []
    package_paths: dict[str, Path] = {}
    for package_path, entry in sorted(lock["packages"].items()):
        if not package_path or "version" not in entry:
            continue
        name = _npm_name(package_path)
        version = str(entry["version"])
        bom_ref = f"npm:{package_path}"
        component: dict[str, Any] = {
            "type": "library",
            "bom-ref": bom_ref,
            "name": name,
            "version": version,
            "purl": f"pkg:npm/{urllib.parse.quote(name, safe='@')}" f"@{urllib.parse.quote(version)}",
            "licenses": [{"license": {"id": entry.get("license", "NOASSERTION")}}],
            "properties": [
                {"name": "npm:package-path", "value": package_path},
                {"name": "npm:development", "value": str(bool(entry.get("dev"))).lower()},
            ],
        }
        integrity = entry.get("integrity", "")
        if integrity.startswith("sha512-"):
            component["hashes"] = [
                {
                    "alg": "SHA-512",
                    "content": base64.b64decode(integrity.removeprefix("sha512-")).hex(),
                }
            ]
        components.append(component)
        package_paths[bom_ref] = ROOT / package_path
    return components, package_paths


def _python_components(
    distributions: list[importlib.metadata.Distribution],
) -> tuple[list[dict[str, Any]], dict[str, importlib.metadata.Distribution]]:
    components: list[dict[str, Any]] = []
    by_ref: dict[str, importlib.metadata.Distribution] = {}
    for distribution in distributions:
        name = distribution.metadata["Name"]
        version = distribution.version
        bom_ref = f"python:{canonicalize_name(name)}"
        components.append(
            {
                "type": "library",
                "bom-ref": bom_ref,
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{urllib.parse.quote(canonicalize_name(name))}@{urllib.parse.quote(version)}",
                "licenses": [{"license": {"id": _license_expression(distribution.metadata)}}],
            }
        )
        by_ref[bom_ref] = distribution
    return components, by_ref


def _infrastructure_components(distribution_form: str) -> list[dict[str, Any]]:
    lock = json.loads((COMPLIANCE_ROOT / "component-lock.json").read_text(encoding="utf-8"))
    components: list[dict[str, Any]] = []
    for entry in lock["components"]:
        if distribution_form not in entry["distribution"]:
            continue
        digest = entry.get("digest")
        properties = [
            {"name": "fcr:distribution-form", "value": distribution_form},
            {"name": "fcr:release-state", "value": "BLOCKED"},
        ]
        component: dict[str, Any] = {
            "type": "container" if entry["type"] == "container" else "library",
            "bom-ref": f"locked:{entry['type']}:{entry['name']}",
            "name": entry["name"],
            "version": entry["version"],
            "licenses": [{"license": {"id": entry["license"]}}],
            "externalReferences": [
                {"type": "distribution", "url": entry["source"]}
            ],
            "properties": properties,
        }
        hashes: list[dict[str, str]] = []
        if digest:
            hashes.append(
                {"alg": "SHA-256", "content": _sha256_value(digest)}
            )
        source_sha256 = entry.get("source_sha256")
        if source_sha256:
            normalized_source = _sha256_value(source_sha256)
            hashes.append({"alg": "SHA-256", "content": normalized_source})
            properties.append(
                {"name": "fcr:source-sha256", "value": normalized_source}
            )
        for architecture, artifact_digest in sorted(
            entry.get("artifact_hashes", {}).items()
        ):
            normalized_artifact = _sha256_value(artifact_digest)
            hashes.append({"alg": "SHA-256", "content": normalized_artifact})
            properties.append(
                {
                    "name": f"fcr:artifact-sha256:{architecture}",
                    "value": normalized_artifact,
                }
            )
        candidate_image_digest = entry.get("candidate_image_digest")
        if candidate_image_digest:
            properties.append(
                {
                    "name": "fcr:candidate-image-digest",
                    "value": f"sha256:{_sha256_value(candidate_image_digest)}",
                }
            )
        if hashes:
            component["hashes"] = list(
                {item["content"]: item for item in hashes}.values()
            )
        components.append(component)
    return components


def _sha256_value(value: object) -> str:
    normalized = str(value).removeprefix("sha256:")
    if SHA256.fullmatch(normalized) is None:
        raise RuntimeError("invalid SHA-256 value in component lock")
    return normalized


def _candidate_sbom(
    distribution_form: str,
    python_components: list[dict[str, Any]],
    npm_components: list[dict[str, Any]],
    *,
    generated_at: str,
    serial_number: uuid.UUID,
) -> dict[str, Any]:
    components = [
        *_infrastructure_components(distribution_form),
        *python_components,
        *npm_components,
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{serial_number}",
        "version": 1,
        "metadata": {
            "timestamp": generated_at,
            "component": {
                "type": "application",
                "bom-ref": "application:fj-final-cut-review",
                "name": "fj-final-cut-review",
                "version": "1.3.0",
            },
            "properties": [
                {"name": "fcr:inventory-kind", "value": "source-candidate"},
                {"name": "fcr:commercial-release-status", "value": "BLOCKED"},
            ],
        },
        "components": components,
    }


def _candidate_license_files(path: Path) -> list[Path]:
    if not path.exists() or not path.is_dir():
        return []
    return sorted(
        child
        for child in path.iterdir()
        if child.is_file() and LICENSE_NAME.fullmatch(child.name)
    )


def _record_license(
    index: list[dict[str, str]],
    *,
    bom_ref: str,
    source: Path,
    expected_sha256: str | None = None,
    upstream_source: str | None = None,
) -> None:
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise RuntimeError(f"upstream license material hash mismatch: {bom_ref}")
    target = LICENSE_ROOT / f"{digest}.txt"
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    item = {
        "component": bom_ref,
        "source": source.relative_to(ROOT).as_posix(),
        "sha256": digest,
        "bundled_file": target.relative_to(ROOT).as_posix(),
    }
    if upstream_source is not None:
        item["upstream_source"] = upstream_source
    index.append(item)


def _record_apache_infrastructure_materials(
    index: list[dict[str, str]],
) -> set[Path]:
    lock = json.loads(
        (COMPLIANCE_ROOT / "component-lock.json").read_text(encoding="utf-8")
    )
    locked_components = {entry["name"]: entry for entry in lock["components"]}
    manifest = json.loads(
        (COMPLIANCE_ROOT / "upstream-component-materials.json").read_text(
            encoding="utf-8"
        )
    )
    materials = manifest.get("components", {})
    if set(materials) != APACHE_INFRA_COMPONENTS:
        raise RuntimeError("Apache infrastructure material coverage is incomplete")

    recorded_paths: set[Path] = set()
    for name in sorted(APACHE_INFRA_COMPONENTS):
        material = materials[name]
        locked = locked_components.get(name)
        repository, raw_repository = APACHE_UPSTREAMS[name]
        commit_sha = str(material.get("commit_sha", ""))
        if (
            locked is None
            or locked.get("license") != "Apache-2.0"
            or material.get("version") != locked.get("version")
            or material.get("tag") != f"v{locked.get('version')}"
            or material.get("repository") != repository
            or re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None
            or material.get("modifications") != "none"
        ):
            raise RuntimeError(f"invalid Apache material identity: {name}")

        license_record = material.get("license", {})
        if license_record.get("source_url") != (
            f"{raw_repository}/{commit_sha}/LICENSE"
        ):
            raise RuntimeError(f"unverifiable upstream LICENSE source: {name}")
        license_path = ROOT / str(license_record.get("path", ""))
        _record_license(
            index,
            bom_ref=f"locked:container:{name}",
            source=license_path,
            expected_sha256=_sha256_value(license_record.get("sha256")),
            upstream_source=str(license_record.get("source_url", "")),
        )
        recorded_paths.add(license_path)

        notice = material.get("notice", {})
        notice_status = notice.get("status")
        if notice_status == "present":
            if notice.get("source_url") != (
                f"{raw_repository}/{commit_sha}/NOTICE"
            ):
                raise RuntimeError(f"unverifiable upstream NOTICE source: {name}")
            notice_path = ROOT / str(notice.get("path", ""))
            _record_license(
                index,
                bom_ref=f"locked:container:{name}",
                source=notice_path,
                expected_sha256=_sha256_value(notice.get("sha256")),
                upstream_source=str(notice.get("source_url", "")),
            )
            recorded_paths.add(notice_path)
        elif notice_status == "absent":
            if (
                notice.get("path") is not None
                or notice.get("immutable_tree_url")
                != f"{repository}/tree/{commit_sha}"
                or notice.get("expected_notice_url")
                != f"{raw_repository}/{commit_sha}/NOTICE"
                or not str(notice.get("checked_at", ""))
                or not str(notice.get("conclusion", ""))
            ):
                raise RuntimeError(f"unverifiable upstream NOTICE absence: {name}")
        else:
            raise RuntimeError(f"unknown upstream NOTICE status: {name}")
    return recorded_paths


def _record_ffmpeg_materials(index: list[dict[str, str]]) -> set[Path]:
    lock = json.loads(
        (COMPLIANCE_ROOT / "component-lock.json").read_text(encoding="utf-8")
    )
    ffmpeg = next(
        (entry for entry in lock["components"] if entry["name"] == "ffmpeg"),
        None,
    )
    if ffmpeg is None or ffmpeg.get("license") != "GPL-2.0-or-later":
        raise RuntimeError("locked FFmpeg GPL identity is absent")
    materials = ffmpeg.get("license_materials", [])
    expected_paths = {
        "compliance/upstream-license-texts/FFmpeg-GPL-2.txt",
        "compliance/upstream-license-texts/FFmpeg-Debian-copyright.txt",
    }
    if {material.get("path") for material in materials} != expected_paths:
        raise RuntimeError("FFmpeg GPL/copyright material coverage is incomplete")
    recorded_paths: set[Path] = set()
    for material in materials:
        path = ROOT / str(material["path"])
        _record_license(
            index,
            bom_ref="locked:deb:ffmpeg",
            source=path,
            expected_sha256=_sha256_value(material["sha256"]),
            upstream_source=str(material["source"]),
        )
        recorded_paths.add(path)
    return recorded_paths


def _generate_license_index(
    python_distributions: dict[str, importlib.metadata.Distribution],
    npm_paths: dict[str, Path],
) -> None:
    index: list[dict[str, str]] = []
    discovered: set[str] = set()
    material_paths = _record_apache_infrastructure_materials(index)
    material_paths.update(_record_ffmpeg_materials(index))
    for bom_ref, distribution in sorted(python_distributions.items()):
        for item in distribution.files or ():
            path = Path(str(distribution.locate_file(item)))
            if path.is_file() and LICENSE_NAME.fullmatch(path.name):
                _record_license(index, bom_ref=bom_ref, source=path)
                discovered.add(bom_ref)
    for bom_ref, package_path in sorted(npm_paths.items()):
        for path in _candidate_license_files(package_path):
            _record_license(index, bom_ref=bom_ref, source=path)
            discovered.add(bom_ref)
    for path in sorted((COMPLIANCE_ROOT / "upstream-license-texts").iterdir()):
        if path in material_paths:
            continue
        _record_license(index, bom_ref=f"locked:upstream:{path.stem}", source=path)
    _write_json(COMPLIANCE_ROOT / "license-index.json", {"licenses": index})
    expected = {*python_distributions, *npm_paths}
    _write_json(
        COMPLIANCE_ROOT / "unresolved-license-texts.json",
        {
            "commercial_release_status": "BLOCKED",
            "components_without_installed_license_text": sorted(expected - discovered),
            "resolution": (
                "Retrieve and verify the license text from the exact source artifact "
                "before customer distribution; do not infer it from metadata alone."
            ),
        },
    )


def main() -> int:
    python_distributions = _python_dependency_closure()
    python_components, python_by_ref = _python_components(python_distributions)
    npm_components, npm_paths = _npm_components()
    generated_at = (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    _write_json(
        SBOM_ROOT / "saas.cdx.json",
        _candidate_sbom(
            "saas",
            python_components,
            npm_components,
            generated_at=generated_at,
            serial_number=uuid.uuid4(),
        ),
    )
    _write_json(
        SBOM_ROOT / "customer-container.cdx.json",
        _candidate_sbom(
            "customer-container",
            python_components,
            npm_components,
            generated_at=generated_at,
            serial_number=uuid.uuid4(),
        ),
    )
    _generate_license_index(python_by_ref, npm_paths)
    print(
        json.dumps(
            {
                "python_components": len(python_components),
                "npm_components": len(npm_components),
                "sboms": 2,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
