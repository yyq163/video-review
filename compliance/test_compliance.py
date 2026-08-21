from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import uuid
from datetime import datetime
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
CANDIDATE_SPEC = importlib.util.spec_from_file_location(
    "verify_candidate_image",
    ROOT / "compliance" / "verify_candidate_image.py",
)
assert CANDIDATE_SPEC is not None and CANDIDATE_SPEC.loader is not None
verify_candidate_image = importlib.util.module_from_spec(CANDIDATE_SPEC)
CANDIDATE_SPEC.loader.exec_module(verify_candidate_image)
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_compliance_artifacts",
    ROOT / "compliance" / "generate_compliance_artifacts.py",
)
assert GENERATOR_SPEC is not None and GENERATOR_SPEC.loader is not None
generate_compliance_artifacts = importlib.util.module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(generate_compliance_artifacts)


def load_json(path: str) -> dict[str, object]:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def normalized_sha256(value: object) -> str:
    return str(value).removeprefix("sha256:")


def candidate_run_fake(
    *,
    tamper: str | None = None,
) -> tuple[object, list[tuple[str, ...]]]:
    lock = load_json("compliance/component-lock.json")
    components = {component["name"]: component for component in lock["components"]}
    apv = components["ffmpeg-apv"]
    candidate_uid = int(lock["candidate_runtime_uid"])
    candidate_gid = int(lock["candidate_runtime_gid"])
    runtime_user = f"{candidate_uid}:{candidate_gid}"
    legacy_records = {
        "/usr/share/fj-build/ffmpeg-version.txt": b"ffmpeg version 7.1.2\n",
        "/usr/share/fj-build/ffmpeg-buildconf.txt": b"--enable-gpl\n",
        "/usr/share/fj-build/ffprobe-version.txt": b"ffprobe version 7.1.2\n",
        "/usr/share/fj-build/ffmpeg-debian-packages.tsv": (
            f"ffmpeg\t{components['ffmpeg']['version']}\tamd64\n".encode()
        ),
        "/usr/share/fj-build/ffmpeg-dynamic-libraries.txt": (
            b"libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n"
        ),
    }
    apv_records = {
        "/opt/ffmpeg-apv/share/ffmpeg-apv-buildconf.txt": (
            "\n".join(sorted(verify_candidate_image.APV_REQUIRED_BUILD_FLAGS)) + "\n"
        ).encode(),
        "/opt/ffmpeg-apv/share/ffmpeg-apv-source-sha256.txt": (
            normalized_sha256(apv["source_sha256"]) + "\n"
        ).encode(),
        "/opt/ffmpeg-apv/share/ffmpeg-apv-dynamic-libraries.txt": (
            b"libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6\n"
        ),
    }
    material_content = {
        str(material["source"]).removeprefix("candidate-image:"): (
            ROOT / material["path"]
        ).read_bytes()
        for component in (components["ffmpeg"], apv)
        for material in component["license_materials"]
    }
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> bytes:
        calls.append(args)
        if args == (
            "docker",
            "inspect",
            "candidate",
            "--format",
            "{{.Image}}",
        ):
            return str(lock["candidate_backend_image_digest"]).encode() + b"\n"
        if args == (
            "docker",
            "exec",
            "candidate",
            "getent",
            "passwd",
            "app",
        ):
            passwd_uid = candidate_uid + 1 if tamper == "uid" else candidate_uid
            return f"app:x:{passwd_uid}:{candidate_gid}:app:/app:/usr/sbin/nologin\n".encode()
        if args == (
            "docker",
            "exec",
            "candidate",
            "getent",
            "group",
            "app",
        ):
            group_gid = candidate_gid + 1 if tamper == "gid" else candidate_gid
            return f"app:x:{group_gid}:\n".encode()
        expected_prefix = (
            "docker",
            "exec",
            "--user",
            runtime_user,
            "candidate",
        )
        if args[:5] != expected_prefix:
            raise AssertionError(f"unexpected candidate command: {args}")
        command = args[5:]
        if command[:1] == ("cat",):
            path = command[1]
            if path in legacy_records:
                return legacy_records[path]
            if path in apv_records:
                if tamper == "buildconf" and path.endswith("buildconf.txt"):
                    return apv_records[path] + b"--enable-gpl\n"
                if tamper == "source" and path.endswith("source-sha256.txt"):
                    return b"0" * 64 + b"\n"
                if tamper == "ldd" and path.endswith("dynamic-libraries.txt"):
                    return b"libmissing.so => not found\n"
                return apv_records[path]
            if path in material_content:
                if tamper == "license" and path.startswith("/opt/ffmpeg-apv/"):
                    return b"tampered license\n"
                return material_content[path]
        if command[:2] == ("uname", "-m"):
            return b"x86_64\n"
        if command[:1] == ("test",) and command[1:2] == ("-x",):
            return b""
        if command[:1] == ("sha256sum",):
            binary_path = command[1]
            binary_name = Path(binary_path).name
            digest = normalized_sha256(
                apv["binary_artifact_hashes"]["amd64"][binary_name]
            )
            if tamper == "binary" and binary_name == "ffmpeg":
                digest = "0" * 64
            return f"{digest}  {binary_path}\n".encode()
        if command == (
            verify_candidate_image.APV_BINARIES["ffmpeg"],
            "-hide_banner",
            "-decoders",
        ):
            if tamper == "decoder":
                return b" V....D h264 H.264\n"
            return b" V....D apv Apple Projected Media\n"
        if len(command) == 2 and command[1] == "-version":
            binary_name = Path(command[0]).name
            if command[0].startswith("/opt/ffmpeg-apv/"):
                if tamper == "version" and binary_name == "ffmpeg":
                    return b"ffmpeg version 0.0.0\n"
                return f"{binary_name} version {apv['version']}\n".encode()
            return f"{binary_name} version 7.1.2\n".encode()
        raise AssertionError(f"unexpected candidate command: {args}")

    return fake_run, calls


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
        assert (
            hashlib.sha256(license_path.read_bytes()).hexdigest()
            == (license_record["sha256"])
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
            assert (
                hashlib.sha256(notice_path.read_bytes()).hexdigest()
                == (notice["sha256"])
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


def test_both_ffmpeg_runtimes_are_fully_bound_in_both_sboms() -> None:
    lock = load_json("compliance/component-lock.json")
    ffmpeg_lock = next(
        component for component in lock["components"] if component["name"] == "ffmpeg"
    )
    apv_lock = next(
        component
        for component in lock["components"]
        if component["name"] == "ffmpeg-apv"
    )
    candidate_digest = lock["candidate_backend_image_digest"]
    assert lock["candidate_runtime_uid"] == 3001
    assert lock["candidate_runtime_gid"] == 3003
    assert ffmpeg_lock["candidate_image_digest"] == candidate_digest
    assert apv_lock["candidate_image_digest"] == candidate_digest
    assert set(ffmpeg_lock["artifact_hashes"]) == {"amd64", "arm64"}
    assert ffmpeg_lock["license"] == "GPL-2.0-or-later"
    assert set(apv_lock["distribution"]) == {"saas", "customer-container"}
    assert apv_lock["license"] == "LGPL-2.1-or-later"
    assert set(apv_lock["binary_artifact_hashes"]) == {"amd64"}
    assert set(apv_lock["binary_artifact_hashes"]["amd64"]) == {
        "ffmpeg",
        "ffprobe",
    }
    index = load_json("compliance/license-index.json")
    indexed_ffmpeg_materials = {
        (entry["source"], entry["sha256"])
        for entry in index["licenses"]
        if entry["component"] == "locked:deb:ffmpeg"
    }
    assert indexed_ffmpeg_materials == {
        (material["path"], material["sha256"])
        for material in ffmpeg_lock["license_materials"]
    }
    indexed_apv_entries = [
        entry
        for entry in index["licenses"]
        if entry["component"] == "locked:source:ffmpeg-apv"
    ]
    indexed_apv_materials = {
        (entry["source"], entry["sha256"]) for entry in indexed_apv_entries
    }
    assert indexed_apv_materials == {
        (material["path"], material["sha256"])
        for material in apv_lock["license_materials"]
    }
    for material in apv_lock["license_materials"]:
        entry = next(
            item for item in indexed_apv_entries if item["source"] == material["path"]
        )
        assert entry["upstream_source"] == material["source"]
        bundled = ROOT / entry["bundled_file"]
        assert hashlib.sha256(bundled.read_bytes()).hexdigest() == material["sha256"]
    expected_hashes = {
        normalized_sha256(ffmpeg_lock["source_sha256"]),
        *(
            normalized_sha256(value)
            for value in ffmpeg_lock["artifact_hashes"].values()
        ),
    }
    expected_apv_hashes = {
        normalized_sha256(apv_lock["source_sha256"]),
        normalized_sha256(apv_lock["signature_sha256"]),
        *(
            normalized_sha256(value)
            for binaries in apv_lock["binary_artifact_hashes"].values()
            for value in binaries.values()
        ),
    }

    lock_generated_at = datetime.fromisoformat(
        str(lock["generated_at"]).replace("Z", "+00:00")
    )
    serial_numbers: set[str] = set()
    for distribution in ("saas", "customer-container"):
        sbom = load_json(f"compliance/sbom/{distribution}.cdx.json")
        generated_at = datetime.fromisoformat(
            str(sbom["metadata"]["timestamp"]).replace("Z", "+00:00")
        )
        assert generated_at >= lock_generated_at
        serial_number = str(sbom["serialNumber"])
        assert serial_number.startswith("urn:uuid:")
        uuid.UUID(serial_number.removeprefix("urn:uuid:"))
        assert serial_number not in serial_numbers
        serial_numbers.add(serial_number)
        assert sbom["version"] == 1
        metadata_properties = {
            item["name"]: item["value"] for item in sbom["metadata"]["properties"]
        }
        assert metadata_properties["fcr:candidate-runtime-uid"] == "3001"
        assert metadata_properties["fcr:candidate-runtime-gid"] == "3003"
        ffmpeg = next(
            component
            for component in sbom["components"]
            if component["bom-ref"] == "locked:deb:ffmpeg"
        )
        assert {item["content"] for item in ffmpeg["hashes"]} == expected_hashes
        properties = {item["name"]: item["value"] for item in ffmpeg["properties"]}
        assert ffmpeg["licenses"] == [{"license": {"id": "GPL-2.0-or-later"}}]
        assert (
            properties["fcr:candidate-image-digest"]
            == ffmpeg_lock["candidate_image_digest"]
        )
        assert properties["fcr:source-sha256"] == normalized_sha256(
            ffmpeg_lock["source_sha256"]
        )
        for architecture, value in ffmpeg_lock["artifact_hashes"].items():
            assert properties[f"fcr:artifact-sha256:{architecture}"] == (
                normalized_sha256(value)
            )
        apv = next(
            component
            for component in sbom["components"]
            if component["bom-ref"] == "locked:source:ffmpeg-apv"
        )
        assert {item["content"] for item in apv["hashes"]} == (expected_apv_hashes)
        apv_properties = {item["name"]: item["value"] for item in apv["properties"]}
        assert apv["licenses"] == [{"license": {"id": "LGPL-2.1-or-later"}}]
        assert apv_properties["fcr:candidate-image-digest"] == candidate_digest
        assert apv_properties["fcr:source-sha256"] == normalized_sha256(
            apv_lock["source_sha256"]
        )
        assert apv_properties["fcr:signature-sha256"] == normalized_sha256(
            apv_lock["signature_sha256"]
        )
        assert (
            apv_properties["fcr:signing-key-fingerprint"]
            == apv_lock["signing_key_fingerprint"]
        )
        for architecture, binaries in apv_lock["binary_artifact_hashes"].items():
            for binary_name, value in binaries.items():
                property_name = (
                    f"fcr:binary-artifact-sha256:{architecture}:{binary_name}"
                )
                assert apv_properties[property_name] == normalized_sha256(value)


def test_generator_emits_apv_identity_for_both_distributions_and_licenses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = load_json("compliance/component-lock.json")
    candidate_digest = lock["candidate_backend_image_digest"]
    for distribution in ("saas", "customer-container"):
        sbom = generate_compliance_artifacts._candidate_sbom(
            distribution,
            [],
            [],
            generated_at="2026-08-21T00:00:00Z",
            serial_number=uuid.UUID(int=1),
        )
        metadata_properties = {
            item["name"]: item["value"] for item in sbom["metadata"]["properties"]
        }
        assert metadata_properties["fcr:candidate-runtime-uid"] == "3001"
        assert metadata_properties["fcr:candidate-runtime-gid"] == "3003"
        components = generate_compliance_artifacts._infrastructure_components(
            distribution
        )
        apv = next(
            component
            for component in components
            if component["bom-ref"] == "locked:source:ffmpeg-apv"
        )
        properties = {item["name"]: item["value"] for item in apv["properties"]}
        assert properties["fcr:distribution-form"] == distribution
        assert properties["fcr:candidate-image-digest"] == candidate_digest
        assert properties["fcr:signature-sha256"]
        assert properties["fcr:signing-key-fingerprint"]
        assert properties["fcr:binary-artifact-sha256:amd64:ffmpeg"]
        assert properties["fcr:binary-artifact-sha256:amd64:ffprobe"]

    captured: list[dict[str, object]] = []

    def capture_record(
        _index: list[dict[str, str]],
        **kwargs: object,
    ) -> None:
        captured.append(kwargs)

    monkeypatch.setattr(
        generate_compliance_artifacts,
        "_record_license",
        capture_record,
    )
    generate_compliance_artifacts._record_locked_component_materials([])
    apv_materials = [
        item for item in captured if item["bom_ref"] == "locked:source:ffmpeg-apv"
    ]
    assert len(apv_materials) == 2
    assert {str(item["source"]).rsplit("/", 1)[-1] for item in apv_materials} == {
        "FFmpeg-APV-LICENSE.md",
        "FFmpeg-APV-LGPL-2.1.txt",
    }


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
            payload["components"]["cadvisor"]["notice"]["immutable_tree_url"] = (
                "https://example.invalid/not-upstream"
            )
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
            for component in original_load_json("compliance/component-lock.json")[
                "components"
            ]
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
                item for item in ffmpeg["hashes"] if item["content"] != arm64_digest
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


def test_supply_chain_hard_gate_rejects_split_candidate_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = copy.deepcopy(original_load_json(path))
        if path == "compliance/component-lock.json":
            apv = next(
                component
                for component in payload["components"]
                if component["name"] == "ffmpeg-apv"
            )
            apv["candidate_image_digest"] = "sha256:" + "0" * 64
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="share one candidate backend image"):
        verify_compliance.verify_supply_chain()


@pytest.mark.parametrize("position", ["prepend", "append"])
def test_supply_chain_hard_gate_rejects_duplicate_component_bom_refs(
    monkeypatch: pytest.MonkeyPatch,
    position: str,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = copy.deepcopy(original_load_json(path))
        if path == "compliance/sbom/saas.cdx.json":
            duplicate = copy.deepcopy(
                next(
                    component
                    for component in payload["components"]
                    if component["bom-ref"] == "locked:source:ffmpeg-apv"
                )
            )
            duplicate["hashes"] = []
            duplicate["properties"] = [{"name": "fcr:release-state", "value": "PASS"}]
            if position == "prepend":
                payload["components"].insert(0, duplicate)
            else:
                payload["components"].append(duplicate)
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="bom-ref values are missing or duplicated"):
        verify_compliance.verify_supply_chain()


@pytest.mark.parametrize("target", ["metadata", "component"])
def test_supply_chain_hard_gate_rejects_duplicate_property_names(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = copy.deepcopy(original_load_json(path))
        if path == "compliance/sbom/saas.cdx.json":
            if target == "metadata":
                payload["metadata"]["properties"].append(
                    {
                        "name": "fcr:commercial-release-status",
                        "value": "PASS",
                    }
                )
            else:
                apv = next(
                    component
                    for component in payload["components"]
                    if component["bom-ref"] == "locked:source:ffmpeg-apv"
                )
                apv["properties"].append({"name": "fcr:release-state", "value": "PASS"})
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="contains duplicate property names"):
        verify_compliance.verify_supply_chain()


@pytest.mark.parametrize(
    "property_name",
    ["fcr:candidate-runtime-uid", "fcr:candidate-runtime-gid"],
)
def test_supply_chain_hard_gate_rejects_wrong_sbom_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
    property_name: str,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = copy.deepcopy(original_load_json(path))
        if path == "compliance/sbom/saas.cdx.json":
            property_item = next(
                item
                for item in payload["metadata"]["properties"]
                if item["name"] == property_name
            )
            property_item["value"] = "10001"
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="runtime UID/GID does not match"):
        verify_compliance.verify_supply_chain()


def test_supply_chain_hard_gate_rejects_missing_apv_binary_hash_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = copy.deepcopy(original_load_json(path))
        if path == "compliance/sbom/saas.cdx.json":
            apv = next(
                component
                for component in payload["components"]
                if component["bom-ref"] == "locked:source:ffmpeg-apv"
            )
            apv["properties"] = [
                item
                for item in apv["properties"]
                if item["name"] != "fcr:binary-artifact-sha256:amd64:ffmpeg"
            ]
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="APV FFmpeg hash roles are missing"):
        verify_compliance.verify_supply_chain()


def test_supply_chain_hard_gate_rejects_customer_sbom_without_apv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load_json = verify_compliance.load_json

    def tampered_load_json(path: str) -> object:
        payload = copy.deepcopy(original_load_json(path))
        if path == "compliance/sbom/customer-container.cdx.json":
            payload["components"] = [
                component
                for component in payload["components"]
                if component["bom-ref"] != "locked:source:ffmpeg-apv"
            ]
        return payload

    monkeypatch.setattr(verify_compliance, "load_json", tampered_load_json)
    with pytest.raises(RuntimeError, match="locked component coverage"):
        verify_compliance.verify_supply_chain()


def test_candidate_verifier_checks_both_runtimes_and_unprivileged_apv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run, calls = candidate_run_fake()
    monkeypatch.setattr(verify_candidate_image, "_run", fake_run)

    result = verify_candidate_image.verify("candidate")

    assert result["status"] == "PASS_CANDIDATE_IMAGE"
    assert result["runtime_identity"] == {"user": "app", "uid": 3001, "gid": 3003}
    assert set(result["runtimes"]) == {"ffmpeg", "ffmpeg-apv"}
    assert result["runtimes"]["ffmpeg-apv"]["binary_sha256"] == {
        "ffmpeg": "e43a4982f352dadeb922814b46cfd42e954bf86872ddd4909e2a630de218c5c3",
        "ffprobe": "07f0b3495714bb2153271a686fe5daa93195baab235fe2dcba92c1f939b45601",
    }
    assert (
        "docker",
        "inspect",
        "candidate",
        "--format",
        "{{.Image}}",
    ) in calls
    assert (
        "docker",
        "exec",
        "candidate",
        "getent",
        "passwd",
        "app",
    ) in calls
    assert (
        "docker",
        "exec",
        "candidate",
        "getent",
        "group",
        "app",
    ) in calls
    assert (
        "docker",
        "exec",
        "--user",
        "3001:3003",
        "candidate",
        verify_candidate_image.APV_BINARIES["ffmpeg"],
        "-hide_banner",
        "-decoders",
    ) in calls
    for binary_path in verify_candidate_image.APV_BINARIES.values():
        assert (
            "docker",
            "exec",
            "--user",
            "3001:3003",
            "candidate",
            "test",
            "-x",
            binary_path,
        ) in calls
    assert all("10001:10001" not in command for call in calls for command in call)


def test_candidate_verifier_rejects_wrong_running_image_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_run, calls = candidate_run_fake()

    def wrong_image_run(*args: str) -> bytes:
        if args == (
            "docker",
            "inspect",
            "candidate",
            "--format",
            "{{.Image}}",
        ):
            calls.append(args)
            return b"sha256:" + b"0" * 64 + b"\n"
        return valid_run(*args)

    monkeypatch.setattr(verify_candidate_image, "_run", wrong_image_run)
    with pytest.raises(RuntimeError, match="does not match component-lock"):
        verify_candidate_image.verify("candidate")
    assert calls == [
        (
            "docker",
            "inspect",
            "candidate",
            "--format",
            "{{.Image}}",
        )
    ]


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("binary", "APV ffmpeg binary hash mismatch"),
        ("buildconf", "capability or license flags drifted"),
        ("source", "source hash record mismatch"),
        ("ldd", "dynamic-library record is invalid"),
        ("decoder", "decoder is absent"),
        ("license", "license material mismatch"),
        ("version", "APV ffmpeg version mismatch"),
        ("uid", "app UID/GID does not match"),
        ("gid", "app UID/GID does not match"),
    ],
)
def test_candidate_verifier_rejects_tampered_candidate_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
    message: str,
) -> None:
    fake_run, _calls = candidate_run_fake(tamper=tamper)
    monkeypatch.setattr(verify_candidate_image, "_run", fake_run)

    with pytest.raises(RuntimeError, match=message):
        verify_candidate_image.verify("candidate")
