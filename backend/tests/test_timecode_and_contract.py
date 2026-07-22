from __future__ import annotations

import copy
import importlib.util
import os
import sqlite3
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from backend.app.modules.final_cut_review.domain.timecode import frame_from_timestamp_ms, format_review_timecode, timestamp_ms_from_frame
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import OperationLogModel
from backend.app.modules.review_contracts.generated import Envelope, ErrorBody, ErrorEnvelope, ReviewPlaybackTarget, ResponseMeta, validate_domain_event_payload


def _load_contract_generator() -> ModuleType:
    root = Path(__file__).resolve().parents[2]
    path = root / "backend" / "scripts" / "generate_contracts.py"
    spec = importlib.util.spec_from_file_location("contract_generator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_timecode_roundtrip_for_required_frame_rates() -> None:
    for fps_num, fps_den in [(24, 1), (25, 1), (30, 1), (24000, 1001), (30000, 1001)]:
        frame = frame_from_timestamp_ms(10_000, fps_num, fps_den)
        timestamp = timestamp_ms_from_frame(frame, fps_num, fps_den)
        assert frame >= 0
        assert timestamp <= 10_000
        assert isinstance(format_review_timecode(frame, fps_num, fps_den), str)


def test_review_playback_target_contract_is_snake_case() -> None:
    target = ReviewPlaybackTarget(
        project_ref_id="p",
        review_item_id="i",
        version_id="v",
        issue_id="iss",
        revision_id="rev",
        annotation_set_id="ann",
        timestamp_ms=1000,
        frame_number=25,
    )
    assert set(target.model_dump()) == {
        "project_ref_id",
        "review_item_id",
        "version_id",
        "issue_id",
        "revision_id",
        "annotation_set_id",
        "timestamp_ms",
        "frame_number",
    }


def test_contract_generation_check_passes() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [str(root / "backend/.venv/bin/python"), "backend/scripts/generate_contracts.py", "--check"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_contract_loader_rejects_duplicate_yaml_mapping_keys(tmp_path: Path) -> None:
    generator = _load_contract_generator()
    contract = tmp_path / "duplicate.yaml"
    contract.write_text("components:\n  responses:\n    Duplicate: one\n    Duplicate: two\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate YAML mapping key: Duplicate"):
        generator.load_yaml(contract)


def test_contract_loader_rejects_duplicate_yaml_merge_keys(tmp_path: Path) -> None:
    generator = _load_contract_generator()
    contract = tmp_path / "duplicate-merge.yaml"
    contract.write_text(
        "base_one: &base_one\n  first: one\n"
        "base_two: &base_two\n  second: two\n"
        "merged:\n  <<: *base_one\n  <<: *base_two\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate YAML mapping key: <<"):
        generator.load_yaml(contract)


def test_openapi_contract_has_complete_runtime_shapes() -> None:
    root = Path(__file__).resolve().parents[2]
    generator = _load_contract_generator()
    bodyless_operation_ids = {
        "abort_upload",
        "authorize_package_download",
        "complete_upload",
    }
    assert generator.BODYLESS_OPERATION_IDS == bodyless_operation_ids
    openapi = yaml.safe_load((root / "contracts/final-cut-review/v1/openapi.yaml").read_text(encoding="utf-8"))
    schemas = openapi["components"]["schemas"]
    assert {"ErrorBody", "ErrorEnvelope", "DomainEventEnvelope", "DomainEventType", "CommandEnvelopeBase"} <= set(schemas)
    assert "ListResponseMeta" in schemas
    assert "DomainEventPayload" in schemas
    assert "details" in schemas["ErrorBody"]["properties"]
    assert "PRINCIPAL_AUTHENTICATION_REQUIRED" in schemas["ErrorBody"]["properties"]["code"]["enum"]
    seen_201 = False
    seen_202 = False
    for path, methods in openapi["paths"].items():
        for method, operation in methods.items():
            if method.startswith("x-"):
                continue
            responses = operation["responses"]
            seen_201 = seen_201 or "201" in responses
            seen_202 = seen_202 or "202" in responses
            success_status = "202" if operation["operationId"] == "review_prepare_package" else "200"
            for status in (success_status, "400", "401", "403", "404", "409", "422", "500", "503"):
                assert status in responses, f"{method.upper()} {path} missing {status}"
            assert responses[success_status].get("description") != "ok"
            if success_status == "202":
                assert "200" not in responses, f"{method.upper()} {path} declares unreachable 200"
            if operation["operationId"] in bodyless_operation_ids:
                assert "requestBody" not in operation
            elif method.lower() in {"post", "put", "patch"}:
                assert "requestBody" in operation, f"{method.upper()} {path} missing requestBody"
    assert seen_201
    assert seen_202
    operations = {
        operation["operationId"]: operation
        for methods in openapi["paths"].values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    }
    complete_upload_params = {parameter["$ref"] for parameter in operations["complete_upload"]["parameters"]}
    assert "#/components/parameters/IdempotencyKey" in complete_upload_params
    upload_part_params = {parameter["$ref"] for parameter in operations["upload_part"]["parameters"]}
    assert "#/components/parameters/IdempotencyKey" not in upload_part_params
    finalized_download = operations["download_finalized_original"]
    finalized_download_params = {parameter["$ref"] for parameter in finalized_download["parameters"]}
    assert "#/components/parameters/RangeHeader" in finalized_download_params
    assert {"200", "206"} <= set(finalized_download["responses"])
    assert "Accept-Ranges" in finalized_download["responses"]["200"]["headers"]
    assert {"Accept-Ranges", "Content-Range"} <= set(finalized_download["responses"]["206"]["headers"])
    expected_media_types = {"video/mp4", "video/quicktime", "application/octet-stream"}
    for operation_id in ("stream_review_version", "download_finalized_original"):
        for status in ("200", "206"):
            assert set(operations[operation_id]["responses"][status]["content"]) == expected_media_types
    assert set(operations["download_package"]["responses"]["200"]["content"]) == {"application/zip"}


@pytest.mark.parametrize(
    ("operation_id", "invalid_media_type"),
    [("list_projects", "application/zip"), ("download_package", "application/json")],
)
def test_openapi_validation_rejects_success_media_type_mutations(operation_id: str, invalid_media_type: str) -> None:
    generator = _load_contract_generator()
    openapi = generator.load_yaml(generator.CONTRACT_ROOT / "openapi.yaml")
    errors = generator.load_yaml(generator.CONTRACT_ROOT / "errors.yaml")["errors"]
    events_data = generator.load_yaml(generator.CONTRACT_ROOT / "events" / "events.yaml")
    mutated = copy.deepcopy(openapi)
    operation = next(
        operation
        for methods in mutated["paths"].values()
        for operation in methods.values()
        if isinstance(operation, dict) and operation.get("operationId") == operation_id
    )
    success_status = "202" if operation_id == "review_prepare_package" else "200"
    schema = next(iter(operation["responses"][success_status]["content"].values()))["schema"]
    operation["responses"][success_status]["content"] = {invalid_media_type: {"schema": schema}}

    with pytest.raises(ValueError, match="media types drift"):
        generator.validate_openapi_contract(
            mutated,
            errors,
            events_data["events"],
            events_data["payload_schemas"],
        )


def test_generated_production_typescript_modules_do_not_exceed_300_lines() -> None:
    root = Path(__file__).resolve().parents[2]
    generated_dir = root / "src" / "modules" / "final-cut-review" / "contracts-generated"
    generated_files = sorted(generated_dir.glob("backend-contract*.ts"))
    assert generated_files
    assert all(len(path.read_text(encoding="utf-8").splitlines()) <= 300 for path in generated_files)


def test_generated_envelopes_reject_shapes_outside_openapi_contract() -> None:
    with pytest.raises(ValidationError):
        Envelope(meta=ResponseMeta(request_id="rid"))  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        ErrorBody(
            code="NOT_A_CONTRACT_ERROR",  # type: ignore[arg-type]
            message="bad",
            http_status=200,
            details={},
            request_id="rid",
            timestamp="2026-06-19T00:00:00Z",
        )

    valid_error = ErrorBody(
        code="VALIDATION_ERROR",
        message="bad",
        http_status=422,
        details={},
        request_id="rid",
        timestamp="2026-06-19T00:00:00Z",
    )
    assert ErrorEnvelope(error=valid_error).error is valid_error

    with pytest.raises(ValidationError):
        ErrorBody(
            code="VALIDATION_ERROR",
            message="bad",
            http_status=200,
            details={},
            request_id="rid",
            timestamp="2026-06-19T00:00:00Z",
        )


def test_generated_types_include_command_ancestry_fields() -> None:
    root = Path(__file__).resolve().parents[2]
    generated = (
        root / "src/modules/final-cut-review/contracts-generated/backend-contract-payloads.ts"
    ).read_text(encoding="utf-8")
    assert "export interface UpdateProjectPayload" in generated
    assert "project_ref_id: string;" in generated
    assert "project_name: string;" in generated
    assert "description: string;" in generated
    assert "export interface UploadReviewVersionPayload" in generated
    assert "review_item_id: string;" in generated
    assert "export interface FinalizeVersionPayload" in generated
    assert "version_id: string;" in generated


def test_operation_log_attribution_migration_is_reversible_and_follows_project_description() -> None:
    root = Path(__file__).resolve().parents[2]
    migration_path = root / "backend/alembic/versions/20260714_0015_operation_log_attribution.py"
    migration = migration_path.read_text(encoding="utf-8")
    assert 'revision = "20260714_0015"' in migration
    assert 'down_revision = "20260714_0014"' in migration
    assert 'server_default="LegacyOperation"' in migration
    assert 'server_default="anonymous"' in migration
    assert 'server_default="request"' in migration
    assert migration.count("server_default=None") == 3
    for column in (
        "command_type",
        "principal_kind",
        "principal_id",
        "client_ip",
        "user_agent",
        "idempotency_key_hash",
        "resource_type",
        "resource_id",
        "failure_stage",
    ):
        assert f'add_column(sa.Column("{column}"' in migration
        assert f'drop_column("{column}")' in migration


def test_operation_log_compatibility_defaults_follow_attribution_migration() -> None:
    root = Path(__file__).resolve().parents[2]
    migration_path = root / "backend/alembic/versions/20260714_0016_operation_log_compatibility_defaults.py"
    migration = migration_path.read_text(encoding="utf-8")
    assert 'revision = "20260714_0016"' in migration
    assert 'down_revision = "20260714_0015"' in migration
    assert migration.count("server_default=sa.text") == 3
    assert migration.count("server_default=None") == 3


def test_operation_log_metadata_preserves_legacy_writer_server_defaults() -> None:
    columns = OperationLogModel.__table__.c
    assert str(columns.command_type.server_default.arg) == "LegacyOperation"
    assert str(columns.principal_kind.server_default.arg) == "anonymous"
    assert str(columns.resource_type.server_default.arg) == "request"


def test_operation_log_attribution_migrates_existing_rows_supports_legacy_writes_and_downgrades(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    database_path = tmp_path / "operation-log-existing.sqlite3"
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{database_path}",
        "ALLOW_SQLITE_FOR_TESTS": "true",
    }

    def alembic(*args: str) -> None:
        result = subprocess.run(
            [str(root / "backend/.venv/bin/alembic"), "-c", "backend/alembic.ini", *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr + result.stdout

    alembic("upgrade", "20260714_0014")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            insert into operation_logs
                (request_id, entry_source, capability, result, error_code, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            ("legacy-request", "edit", "review.project.update", "ok", None, "2026-07-14 00:00:00"),
        )
        connection.commit()

    alembic("upgrade", "20260714_0015")
    with sqlite3.connect(database_path) as connection:
        columns_at_0015 = {
            row[1]: row for row in connection.execute("pragma table_info(operation_logs)")
        }
    assert columns_at_0015["command_type"][4] is None
    assert columns_at_0015["principal_kind"][4] is None
    assert columns_at_0015["resource_type"][4] is None

    alembic("upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row for row in connection.execute("pragma table_info(operation_logs)")
        }
        migrated_row = connection.execute(
            "select request_id, command_type, principal_kind, resource_type "
            "from operation_logs where request_id = ?",
            ("legacy-request",),
        ).fetchone()
        connection.execute(
            """
            insert into operation_logs
                (request_id, entry_source, capability, result, error_code, created_at)
            values (?, ?, ?, ?, ?, ?)
            """,
            ("legacy-writer", "embedded", None, "ok", None, "2026-07-14 00:01:00"),
        )
        legacy_writer_row = connection.execute(
            "select request_id, command_type, principal_kind, resource_type "
            "from operation_logs where request_id = ?",
            ("legacy-writer",),
        ).fetchone()
        connection.commit()
    assert columns["command_type"][4] == "'LegacyOperation'"
    assert columns["principal_kind"][4] == "'anonymous'"
    assert columns["resource_type"][4] == "'request'"
    assert migrated_row == ("legacy-request", "LegacyOperation", "anonymous", "request")
    assert legacy_writer_row == ("legacy-writer", "LegacyOperation", "anonymous", "request")

    alembic("downgrade", "20260714_0014")
    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {row[1] for row in connection.execute("pragma table_info(operation_logs)")}
        legacy_rows = connection.execute(
            "select request_id, entry_source, capability, result from operation_logs "
            "order by request_id"
        ).fetchall()
    assert "command_type" not in downgraded_columns
    assert "principal_kind" not in downgraded_columns
    assert legacy_rows == [
        ("legacy-request", "edit", "review.project.update", "ok"),
        ("legacy-writer", "embedded", None, "ok"),
    ]


def test_domain_event_payload_schema_is_validated() -> None:
    validate_domain_event_payload("review.project.created", {"project_code": "P001"})
    validate_domain_event_payload("review.package.failed", {"error_code": "PACKAGE_SOURCE_MISSING"})
    try:
        validate_domain_event_payload("review.package.failed", {"error_code": "X", "extra": True})
    except ValueError as exc:
        assert "unknown fields" in str(exc)
    else:
        raise AssertionError("event payload schema accepted an unknown field")
