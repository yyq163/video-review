from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFY_SPEC = importlib.util.spec_from_file_location(
    "verify_compliance",
    ROOT / "compliance" / "verify_compliance.py",
)
assert VERIFY_SPEC is not None and VERIFY_SPEC.loader is not None
verify_compliance = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(verify_compliance)


def load_json(path: str) -> dict[str, object]:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def normalized_sha256(value: object) -> str:
    return str(value).removeprefix("sha256:")


def test_apache_infrastructure_materials_are_directly_bundled() -> None:
    manifest = load_json("compliance/upstream-component-materials.json")
    materials = manifest["components"]
    assert isinstance(materials, dict)
    assert set(materials) == {"prometheus", "cadvisor"}

    index = load_json("compliance/license-index.json")
    entries = {
        (entry["component"], entry["source"], entry["sha256"])
        for entry in index["licenses"]
    }
    for name, material in materials.items():
        commit_sha = material["commit_sha"]
        license_record = material["license"]
        license_path = ROOT / license_record["path"]
        assert hashlib.sha256(license_path.read_bytes()).hexdigest() == (
            license_record["sha256"]
        )
        assert commit_sha in license_record["source_url"]
        assert (
            f"locked:container:{name}",
            license_record["path"],
            license_record["sha256"],
        ) in entries

        notice = material["notice"]
        if notice["status"] == "present":
            notice_path = ROOT / notice["path"]
            assert hashlib.sha256(notice_path.read_bytes()).hexdigest() == (
                notice["sha256"]
            )
            assert (
                f"locked:container:{name}",
                notice["path"],
                notice["sha256"],
            ) in entries
        else:
            assert notice["status"] == "absent"
            assert "path" not in notice
            assert commit_sha in notice["immutable_tree_url"]
            assert commit_sha in notice["expected_notice_url"]
            assert notice["checked_at"]
            assert notice["conclusion"]


def test_ffmpeg_source_and_architecture_hashes_are_in_both_sboms() -> None:
    lock = load_json("compliance/component-lock.json")
    ffmpeg_lock = next(
        component
        for component in lock["components"]
        if component["name"] == "ffmpeg"
    )
    assert set(ffmpeg_lock["artifact_hashes"]) == {"amd64", "arm64"}
    expected_hashes = {
        normalized_sha256(ffmpeg_lock["source_sha256"]),
        *(
            normalized_sha256(value)
            for value in ffmpeg_lock["artifact_hashes"].values()
        ),
    }

    for distribution in ("saas", "customer-container"):
        sbom = load_json(f"compliance/sbom/{distribution}.cdx.json")
        ffmpeg = next(
            component
            for component in sbom["components"]
            if component["bom-ref"] == "locked:deb:ffmpeg"
        )
        assert {item["content"] for item in ffmpeg["hashes"]} == expected_hashes
        properties = {
            item["name"]: item["value"] for item in ffmpeg["properties"]
        }
        assert properties["fcr:source-sha256"] == normalized_sha256(
            ffmpeg_lock["source_sha256"]
        )
        for architecture, value in ffmpeg_lock["artifact_hashes"].items():
            assert properties[f"fcr:artifact-sha256:{architecture}"] == (
                normalized_sha256(value)
            )


def test_supply_chain_hard_gate_accepts_generated_artifacts() -> None:
    result = verify_compliance.verify_supply_chain()
    assert result["saas_components"] > 0
    assert result["customer-container_components"] > 0
    assert result["license_texts"] > 0


def test_supply_chain_hard_gate_rejects_missing_ffmpeg_artifact_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = original_load_json(path)
        if path == "compliance/sbom/saas.cdx.json":
            payload = copy.deepcopy(payload)
            ffmpeg = next(
                component
                for component in payload["components"]
                if component["bom-ref"] == "locked:deb:ffmpeg"
            )
            ffmpeg["hashes"].pop()
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="FFmpeg hashes do not match"):
        verify_compliance.verify_supply_chain()


def test_supply_chain_hard_gate_rejects_unverifiable_notice_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = original_load_json(path)
        if path == "compliance/upstream-component-materials.json":
            payload = copy.deepcopy(payload)
            payload["components"]["cadvisor"]["notice"][
                "immutable_tree_url"
            ] = "https://example.invalid/not-upstream"
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="NOTICE absence is not reproducibly"):
        verify_compliance.verify_supply_chain()


def test_supply_chain_hard_gate_rejects_missing_locked_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = original_load_json(path)
        if path == "compliance/sbom/saas.cdx.json":
            payload = copy.deepcopy(payload)
            payload["components"] = [
                component
                for component in payload["components"]
                if component["bom-ref"] != "locked:container:prometheus"
            ]
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="locked component coverage"):
        verify_compliance.verify_supply_chain()


def test_supply_chain_hard_gate_requires_both_ffmpeg_architectures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load_json = verify_compliance.load_json
    arm64_digest = normalized_sha256(
        next(
            component
            for component in original_load_json(
                "compliance/component-lock.json"
            )["components"]
            if component["name"] == "ffmpeg"
        )["artifact_hashes"]["arm64"]
    )

    def tampered_load_json(path: str) -> object:
        payload = copy.deepcopy(original_load_json(path))
        if path == "compliance/component-lock.json":
            ffmpeg = next(
                component
                for component in payload["components"]
                if component["name"] == "ffmpeg"
            )
            del ffmpeg["artifact_hashes"]["arm64"]
        elif path.startswith("compliance/sbom/"):
            ffmpeg = next(
                component
                for component in payload["components"]
                if component["bom-ref"] == "locked:deb:ffmpeg"
            )
            ffmpeg["hashes"] = [
                item
                for item in ffmpeg["hashes"]
                if item["content"] != arm64_digest
            ]
            ffmpeg["properties"] = [
                item
                for item in ffmpeg["properties"]
                if item["name"] != "fcr:artifact-sha256:arm64"
            ]
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="required architecture hashes"):
        verify_compliance.verify_supply_chain()


def test_supply_chain_hard_gate_rejects_release_status_flip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = copy.deepcopy(original_load_json(path))
        if path.startswith("compliance/sbom/"):
            for item in payload["metadata"]["properties"]:
                if item["name"] == "fcr:commercial-release-status":
                    item["value"] = "PASS"
            for component in payload["components"]:
                if not component["bom-ref"].startswith("locked:"):
                    continue
                for item in component.get("properties", []):
                    if item["name"] == "fcr:release-state":
                        item["value"] = "PASS"
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="commercial release status"):
        verify_compliance.verify_supply_chain()
