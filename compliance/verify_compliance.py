#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPLIANCE = ROOT / "compliance"
IMAGE_PATTERN = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def load_json(path: str) -> Any:
    return json.loads(read(path))


def sha256_value(value: object) -> str:
    normalized = str(value).removeprefix("sha256:")
    require(
        SHA256_PATTERN.fullmatch(normalized) is not None,
        "invalid SHA-256 value",
    )
    return normalized


def file_sha256(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def compose_network_names(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks", [])
    return set(networks if isinstance(networks, list) else networks.keys())


def verify_compose() -> None:
    compose = yaml.safe_load(read("docker-compose.yml"))
    services = compose["services"]
    for name in ("nginx", "prometheus", "cadvisor"):
        require(IMAGE_PATTERN.fullmatch(services[name]["image"]) is not None, f"{name} image is mutable")

    backend = services["backend"]
    require(backend["environment"]["PROTECTED_MEDIA_DIRECT_STREAM_ENABLED"] == "0", "backend bypasses NGINX")
    require(
        backend["environment"]["REVERSE_PROXY_TRUSTED_HOSTS"]
        == "${NGINX_INTERNAL_IP:-172.29.0.10}",
        "backend proxy trust is not bound to the exact NGINX address contract",
    )
    require("ports" not in backend and backend.get("expose") == ["8000"], "backend is published outside NGINX")
    require(
        compose_network_names(backend) == {"app-internal", "management"},
        "backend network boundary drifted",
    )

    nginx = services["nginx"]
    require(nginx["read_only"] is True, "NGINX root filesystem is writable")
    require(nginx["user"] == "10001:10001", "NGINX cannot read private app-owned media safely")
    require("edge_handoff_secret" in nginx["secrets"], "NGINX edge handoff secret is absent")
    require(all(str(volume).endswith(":ro") for volume in nginx["volumes"]), "NGINX mounts must be read-only")
    nginx_command = "\n".join(nginx["command"])
    require(
        'test "$${#edge_secret}" -ge 32' in nginx_command
        and 'test "$${#edge_secret}" -le 128' in nginx_command,
        "NGINX edge handoff secret length is unbounded",
    )

    prometheus = services["prometheus"]
    require("ports" not in prometheus, "Prometheus is host-published")
    require(
        compose_network_names(prometheus) == {"management"},
        "Prometheus is outside the management network",
    )

    cadvisor = services["cadvisor"]
    require(cadvisor["profiles"] == ["observability-host-approved"], "cAdvisor approval profile drifted")
    require(cadvisor["privileged"] is False, "cAdvisor is privileged")
    require(cadvisor["cap_drop"] == ["ALL"], "cAdvisor Linux capabilities are not dropped")
    require(all(str(volume).endswith(":ro") for volume in cadvisor["volumes"]), "cAdvisor mounts must be read-only")
    require(
        compose_network_names(cadvisor) == {"management"},
        "cAdvisor is outside the management network",
    )

    postgres_command = " ".join(services["postgres"]["command"])
    require("shared_preload_libraries=pg_stat_statements" in postgres_command, "pg_stat_statements is not preloaded")
    require("compute_query_id=on" in postgres_command, "PostgreSQL query identifiers are disabled")


def verify_nginx() -> None:
    config = read("ops/nginx/nginx.conf.template")
    require(
        "map_hash_bucket_size 256;" in config,
        "NGINX map hash cannot safely hold the bounded handoff secret",
    )
    for temp_name in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"):
        require(
            f"{temp_name}_temp_path /tmp/" in config,
            f"NGINX {temp_name} temp path is not on the non-root tmpfs",
        )
    require("location = /internal/metrics" in config, "exact metrics deny location is absent")
    metrics_block = config.split("location = /internal/metrics", 1)[1].split("}", 1)[0]
    require("return 404;" in metrics_block, "exact metrics location does not deny access")
    require("proxy_pass" not in metrics_block and "alias " not in metrics_block, "metrics deny can reach an upstream")
    require(
        'location ~ "^/_protected_media/(?<opaque_file_id>(?:file|media)_[0-9a-f]{32})$"' in config,
        "protected media identifier is not bounded",
    )
    protected = config.split('location ~ "^/_protected_media/', 1)[1].split(
        "location /_protected_media/",
        1,
    )[0]
    require("internal;" in protected, "protected media location is externally reachable")
    require("alias /data/storage/files/$opaque_file_id;" in protected, "protected media alias is not fixed")
    require("$uri" not in protected and "$request_uri" not in protected, "request path reaches the filesystem")
    log_line = next(line for line in config.splitlines() if "log_format bounded" in line)
    for forbidden in ("$remote_addr", "$uri", "$request_uri", "$args", "$http_"):
        require(forbidden not in log_line, f"NGINX access log includes sensitive/high-cardinality {forbidden}")
    require("${FCR_EDGE_HANDOFF_SECRET}" in config, "edge secret is not part of the fail-closed map")
    proxy_location = config.split("location / {", 1)[1]
    reject_offset = proxy_location.index("if ($edge_handoff_valid = 0)")
    proxy_offset = proxy_location.index("proxy_pass http://fcr_backend;")
    require(
        reject_offset < proxy_offset
        and 'proxy_set_header X-FCR-Edge-Handoff "";' in proxy_location,
        "NGINX does not reject invalid handoffs or clear the edge secret",
    )
    require(
        'proxy_set_header X-Write-Guard-Verified "true";' in proxy_location,
        "NGINX does not overwrite the client write-guard header after handoff validation",
    )


def verify_prometheus() -> None:
    config = yaml.safe_load(read("ops/prometheus/prometheus.yml"))
    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    require(set(jobs) == {
        "fj-final-cut-review-backend",
        "fj-final-cut-review-package-worker",
        "fj-final-cut-review-media-worker",
        "fj-final-cut-review-cadvisor",
    }, "Prometheus scrape scope drifted")
    expected_targets = {
        "fj-final-cut-review-backend": ["backend:8000"],
        "fj-final-cut-review-package-worker": ["package-worker:9101"],
        "fj-final-cut-review-media-worker": ["media-worker:9102"],
    }
    for job_name, targets in expected_targets.items():
        require(
            jobs[job_name]["static_configs"][0]["targets"] == targets,
            f"Prometheus target drifted for {job_name}",
        )
    cadvisor_relabels = jobs["fj-final-cut-review-cadvisor"]["metric_relabel_configs"]
    require(any(rule.get("action") == "labeldrop" for rule in cadvisor_relabels), "cAdvisor labels are not bounded")
    require(any(rule.get("action") == "keep" for rule in cadvisor_relabels), "cAdvisor scrape is not project-scoped")


def verify_observability() -> None:
    source = read("backend/app/observability.py")
    require("queryid" not in str(next(line for line in source.splitlines() if "SELECT calls" in line)), "query id selected")
    require(
        "fcr_observability.fcr_pg_stat_summary()" in source,
        "safe PostgreSQL aggregate is not used",
    )
    bootstrap = read("backend/scripts/bootstrap_database_roles.py")
    require(
        'schema_name = "fcr_observability"' in bootstrap
        and "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA" in bootstrap
        and "FROM PUBLIC" in bootstrap,
        "SQL text remains public",
    )
    require("SECURITY DEFINER" in bootstrap and "SET search_path = pg_catalog" in bootstrap, "safe aggregate is not hardened")
    require("pg_read_all_stats" in bootstrap, "runtime diagnostic-role denial is absent")


def verify_supply_chain() -> dict[str, int]:
    lock = load_json("compliance/component-lock.json")
    require(lock["commercial_release_status"] == "BLOCKED", "commercial release was incorrectly approved")
    blockers = lock.get("commercial_release_blockers", [])
    require(
        isinstance(blockers, list)
        and any("regions" in str(item) for item in blockers)
        and any(
            all(codec in str(item) for codec in ("H.264", "H.265", "AAC"))
            for item in blockers
        )
        and any(
            "candidate backend image digest" in str(item)
            and "runtime FFmpeg build records" in str(item)
            for item in blockers
        ),
        "commercial release blockers are incomplete",
    )
    locked_components = {component["name"]: component for component in lock["components"]}
    for component in lock["components"]:
        if component["type"] == "container":
            require(re.fullmatch(r"sha256:[0-9a-f]{64}", component["digest"]) is not None, "container digest missing")

    materials = load_json("compliance/upstream-component-materials.json")[
        "components"
    ]
    require(
        set(materials) == APACHE_INFRA_COMPONENTS,
        "Apache infrastructure LICENSE/NOTICE coverage is incomplete",
    )
    required_license_entries: set[tuple[str, str, str]] = set()
    for name in sorted(APACHE_INFRA_COMPONENTS):
        material = materials[name]
        locked = locked_components[name]
        repository, raw_repository = APACHE_UPSTREAMS[name]
        commit_sha = str(material.get("commit_sha", ""))
        require(
            locked["license"] == "Apache-2.0"
            and material.get("version") == locked["version"]
            and material.get("tag") == f"v{locked['version']}"
            and material.get("repository") == repository
            and re.fullmatch(r"[0-9a-f]{40}", commit_sha) is not None
            and material.get("modifications") == "none",
            f"invalid Apache material identity: {name}",
        )
        license_record = material["license"]
        license_hash = sha256_value(license_record["sha256"])
        require(
            file_sha256(license_record["path"]) == license_hash
            and license_record["source_url"]
            == f"{raw_repository}/{commit_sha}/LICENSE",
            f"Apache LICENSE provenance mismatch: {name}",
        )
        required_license_entries.add(
            (f"locked:container:{name}", license_hash, license_record["path"])
        )
        notice = material["notice"]
        if notice.get("status") == "present":
            notice_hash = sha256_value(notice["sha256"])
            require(
                file_sha256(notice["path"]) == notice_hash
                and notice["source_url"]
                == f"{raw_repository}/{commit_sha}/NOTICE",
                f"Apache NOTICE provenance mismatch: {name}",
            )
            required_license_entries.add(
                (f"locked:container:{name}", notice_hash, notice["path"])
            )
        else:
            require(
                notice.get("status") == "absent"
                and notice.get("path") is None
                and notice.get("immutable_tree_url")
                == f"{repository}/tree/{commit_sha}"
                and notice.get("expected_notice_url")
                == f"{raw_repository}/{commit_sha}/NOTICE"
                and bool(notice.get("checked_at"))
                and bool(notice.get("conclusion")),
                f"Apache NOTICE absence is not reproducibly documented: {name}",
            )

    dockerfile = read("backend/Dockerfile")
    require("FFMPEG_PACKAGE_VERSION=7:7.1.5-0+deb13u1" in dockerfile, "FFmpeg package is not locked")
    require("--enable-nonfree" in dockerfile and "! ffmpeg -buildconf" in dockerfile, "nonfree build rejection is absent")
    for record in (
        "ffmpeg-version.txt",
        "ffmpeg-buildconf.txt",
        "ffprobe-version.txt",
        "ffmpeg-debian-packages.tsv",
        "ffmpeg-dynamic-libraries.txt",
    ):
        require(record in dockerfile, f"FFmpeg build record is absent: {record}")

    npm_expected = sum(1 for key in load_json("package-lock.json")["packages"] if key)
    python_requirements = [
        line.split("==", 1)[0].split("[", 1)[0].lower()
        for line in read("backend/requirements.txt").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    results: dict[str, int] = {}
    locked_ffmpeg = locked_components["ffmpeg"]
    require(
        bool(locked_ffmpeg.get("source_sha256"))
        and set(locked_ffmpeg.get("artifact_hashes", {})) == {"amd64", "arm64"},
        "FFmpeg source and required architecture hashes are incomplete",
    )
    for name in ("saas", "customer-container"):
        sbom = load_json(f"compliance/sbom/{name}.cdx.json")
        require(sbom["bomFormat"] == "CycloneDX" and sbom["specVersion"] == "1.6", f"{name} SBOM schema drifted")
        metadata_properties = {
            item["name"]: item["value"]
            for item in sbom.get("metadata", {}).get("properties", [])
        }
        require(
            metadata_properties.get("fcr:commercial-release-status")
            == "BLOCKED",
            f"{name} SBOM commercial release status is not blocked",
        )
        expected_locked_refs = {
            f"locked:{component['type']}:{component['name']}"
            for component in lock["components"]
            if name in component["distribution"]
        }
        locked_items = {
            item["bom-ref"]: item
            for item in sbom["components"]
            if item["bom-ref"].startswith("locked:")
        }
        require(
            set(locked_items) == expected_locked_refs,
            f"{name} SBOM locked component coverage is incomplete",
        )
        for bom_ref, item in locked_items.items():
            properties = {
                property_item["name"]: property_item["value"]
                for property_item in item.get("properties", [])
            }
            require(
                properties.get("fcr:release-state") == "BLOCKED",
                f"{name} SBOM component release state is not blocked: {bom_ref}",
            )
        npm_components = [item for item in sbom["components"] if item["bom-ref"].startswith("npm:")]
        python_components = [item for item in sbom["components"] if item["bom-ref"].startswith("python:")]
        require(len(npm_components) == npm_expected, f"{name} SBOM omits npm dependencies")
        python_names = {item["name"].lower().replace("_", "-") for item in python_components}
        require(set(python_requirements).issubset(python_names), f"{name} SBOM omits direct Python dependencies")
        ffmpeg = next(
            (
                item
                for item in sbom["components"]
                if item["bom-ref"] == "locked:deb:ffmpeg"
            ),
            None,
        )
        require(ffmpeg is not None, f"{name} SBOM omits FFmpeg")
        expected_ffmpeg_hashes = {
            sha256_value(locked_ffmpeg["source_sha256"]),
            *(
                sha256_value(value)
                for value in locked_ffmpeg["artifact_hashes"].values()
            ),
        }
        actual_ffmpeg_hashes = {
            item["content"] for item in ffmpeg.get("hashes", [])
        }
        require(
            actual_ffmpeg_hashes == expected_ffmpeg_hashes,
            f"{name} SBOM FFmpeg hashes do not match component-lock",
        )
        ffmpeg_properties = {
            item["name"]: item["value"] for item in ffmpeg["properties"]
        }
        require(
            ffmpeg_properties.get("fcr:source-sha256")
            == sha256_value(locked_ffmpeg["source_sha256"])
            and all(
                ffmpeg_properties.get(f"fcr:artifact-sha256:{architecture}")
                == sha256_value(value)
                for architecture, value in locked_ffmpeg["artifact_hashes"].items()
            ),
            f"{name} SBOM FFmpeg hash roles are missing",
        )
        results[f"{name}_components"] = len(sbom["components"])

    license_index = load_json("compliance/license-index.json")
    require(bool(license_index["licenses"]), "license-text bundle is empty")
    actual_license_entries = {
        (entry["component"], entry["sha256"], entry["source"])
        for entry in license_index["licenses"]
    }
    require(
        required_license_entries.issubset(actual_license_entries),
        "Apache infrastructure LICENSE/NOTICE entries are absent from the bundle",
    )
    unresolved = load_json("compliance/unresolved-license-texts.json")
    require(
        unresolved["commercial_release_status"] == "BLOCKED",
        "missing license texts are not an explicit release blocker",
    )
    results["license_texts"] = len(license_index["licenses"])
    results["unresolved_license_texts"] = len(unresolved["components_without_installed_license_text"])
    return results


def main() -> int:
    verify_compose()
    verify_nginx()
    verify_prometheus()
    verify_observability()
    result = verify_supply_chain()
    print(json.dumps({"status": "PASS_STATIC", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
