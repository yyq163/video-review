#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_ROOT = ROOT / "contracts" / "final-cut-review" / "v1"
PY_OUT = ROOT / "backend" / "app" / "modules" / "review_contracts" / "generated.py"
TS_OUT = ROOT / "src" / "modules" / "final-cut-review" / "contracts-generated" / "backend-contract.ts"
STANDARD_ERROR_RESPONSES: dict[str, str] = {
    "400": "BadRequest",
    "401": "Unauthorized",
    "403": "Forbidden",
    "404": "NotFound",
    "409": "Conflict",
    "422": "ValidationError",
    "500": "InternalServerError",
    "503": "StorageUnavailable",
}
OPERATION_REQUIRED_RESPONSES: dict[str, tuple[str, ...]] = {
    "review_prepare_package": ("413",),
    "get_package": ("410",),
    "download_package": ("410",),
    "init_upload": ("413",),
    "upload_part": ("413",),
    "complete_upload": ("422",),
}
OPERATION_SUCCESS_RESPONSES: dict[str, str] = {
    "review_prepare_package": "202",
}
BINARY_MEDIA_RESPONSES: dict[str, dict[str, set[str]]] = {
    "stream_review_version": {
        "200": {"video/mp4", "video/quicktime", "application/octet-stream"},
        "206": {"video/mp4", "video/quicktime", "application/octet-stream"},
    },
    "download_finalized_original": {
        "200": {"video/mp4", "video/quicktime", "application/octet-stream"},
        "206": {"video/mp4", "video/quicktime", "application/octet-stream"},
    },
    "download_package": {
        "200": {"application/zip"},
    },
}
BODYLESS_OPERATION_IDS = {
    "abort_upload",
    "authorize_package_download",
    "complete_upload",
}
EVENT_PAYLOAD_SCHEMA_NAMES: dict[str, str] = {
    "review.project.created": "ReviewProjectCreatedEventPayload",
    "review.project.updated": "ReviewProjectUpdatedEventPayload",
    "review.project.archived": "ReviewProjectArchivedEventPayload",
    "review.project.restored": "ReviewProjectRestoredEventPayload",
    "review.project.deleted": "ReviewProjectDeletedEventPayload",
    "review.item.created": "ReviewItemCreatedEventPayload",
    "review.item.updated": "ReviewItemUpdatedEventPayload",
    "review.item.deleted": "ReviewItemDeletedEventPayload",
    "review.version.uploaded": "ReviewVersionUploadedEventPayload",
    "review.session.started": "ReviewSessionStartedEventPayload",
    "review.issue.created": "ReviewIssueCreatedEventPayload",
    "review.issue.updated": "ReviewIssueUpdatedEventPayload",
    "review.issue.message_added": "ReviewIssueMessageAddedEventPayload",
    "review.issue.resolved": "ReviewIssueResolvedEventPayload",
    "review.issue.reopened": "ReviewIssueReopenedEventPayload",
    "review.issue.deleted": "ReviewIssueDeletedEventPayload",
    "review.changes_requested": "ReviewChangesRequestedEventPayload",
    "review.version.finalized": "ReviewVersionFinalizedEventPayload",
    "review.finalized_original.download_requested": "ReviewFinalizedOriginalDownloadRequestedEventPayload",
    "review.package.requested": "ReviewPackageRequestedEventPayload",
    "review.package.ready": "ReviewPackageReadyEventPayload",
    "review.package.failed": "ReviewPackageFailedEventPayload",
}


class UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: UniqueKeySafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    merge_seen = False
    for key_node, _value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            if merge_seen:
                raise ValueError("duplicate YAML mapping key: <<")
            merge_seen = True
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ValueError(f"duplicate YAML mapping key: {key}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeySafeLoader)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def collect_commands() -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    for path in sorted((CONTRACT_ROOT / "commands").glob("*.json")):
        data = load_json(path)
        if data.get("contract_version") != "1.0":
            raise ValueError(f"{path} has invalid contract_version")
        commands.extend(data.get("commands", []))
    return commands


def collect_command_definitions() -> dict[str, Any]:
    definitions: dict[str, Any] = {}
    for path in sorted((CONTRACT_ROOT / "commands").glob("*.json")):
        data = load_json(path)
        definitions.update(data.get("definitions", {}))
    return definitions


def validate_openapi_contract(openapi: dict[str, Any], errors: dict[str, int], events: list[str], event_payload_schemas: dict[str, Any]) -> None:
    components = openapi.get("components", {})
    schemas = components.get("schemas", {})
    responses = components.get("responses", {})
    if "ErrorBody" not in schemas or "ErrorEnvelope" not in schemas:
        raise ValueError("OpenAPI must define ErrorBody and ErrorEnvelope components")
    envelope_any = schemas.get("EnvelopeAny")
    if not envelope_any:
        raise ValueError("OpenAPI must define EnvelopeAny component")
    if sorted(envelope_any.get("required", [])) != ["data", "meta"]:
        raise ValueError("OpenAPI EnvelopeAny must require data and meta")
    if envelope_any.get("additionalProperties") is not False:
        raise ValueError("OpenAPI EnvelopeAny must forbid additional properties")
    if envelope_any.get("properties", {}).get("meta", {}).get("$ref") != "#/components/schemas/ResponseMeta":
        raise ValueError("OpenAPI EnvelopeAny.meta must reference ResponseMeta")
    error_body = schemas["ErrorBody"]
    if sorted(error_body.get("required", [])) != ["code", "contract_version", "details", "http_status", "message", "request_id", "timestamp"]:
        raise ValueError("OpenAPI ErrorBody required fields drift")
    if error_body.get("additionalProperties") is not False:
        raise ValueError("OpenAPI ErrorBody must forbid additional properties")
    status_schema = error_body.get("properties", {}).get("http_status", {})
    if status_schema.get("type") != "integer" or status_schema.get("minimum") != 400 or status_schema.get("maximum") != 599:
        raise ValueError("OpenAPI ErrorBody.http_status must be integer 400..599")
    error_envelope = schemas["ErrorEnvelope"]
    if error_envelope.get("required") != ["error"]:
        raise ValueError("OpenAPI ErrorEnvelope must require error")
    if error_envelope.get("additionalProperties") is not False:
        raise ValueError("OpenAPI ErrorEnvelope must forbid additional properties")
    if error_envelope.get("properties", {}).get("error", {}).get("$ref") != "#/components/schemas/ErrorBody":
        raise ValueError("OpenAPI ErrorEnvelope.error must reference ErrorBody")
    if "DomainEventEnvelope" not in schemas or "DomainEventType" not in schemas:
        raise ValueError("OpenAPI must define event contract schemas")
    package_schema = schemas.get("PackageSnapshotDTO", {})
    package_properties = package_schema.get("properties", {})
    if package_schema.get("additionalProperties") is not False:
        raise ValueError("OpenAPI PackageSnapshotDTO must forbid additional properties")
    if package_properties.get("sha256") != {"type": "string", "pattern": "^[0-9a-f]{64}$"}:
        raise ValueError("OpenAPI PackageSnapshotDTO.sha256 must be an optional 64-lowercase-hex string")
    failure_schema = package_properties.get("failure_details", {})
    if failure_schema.get("type") != "object" or failure_schema.get("additionalProperties") is not True:
        raise ValueError("OpenAPI PackageSnapshotDTO.failure_details must be an optional non-null object")
    event_enum = schemas["DomainEventType"].get("enum", [])
    if sorted(event_enum) != sorted(events):
        raise ValueError("OpenAPI DomainEventType enum drift from events.yaml")
    if set(event_payload_schemas) != set(events):
        raise ValueError("events.yaml payload_schemas must cover every event exactly")
    if "DomainEventPayload" not in schemas:
        raise ValueError("OpenAPI must define DomainEventPayload schema")
    for event in events:
        schema_name = EVENT_PAYLOAD_SCHEMA_NAMES[event]
        if schema_name not in schemas:
            raise ValueError(f"OpenAPI missing payload schema for event {event}: {schema_name}")
    error_enum = schemas["ErrorBody"]["properties"]["code"].get("enum", [])
    if sorted(error_enum) != sorted(errors):
        raise ValueError("OpenAPI ErrorBody enum drift from errors.yaml")
    for status, response_name in STANDARD_ERROR_RESPONSES.items():
        response = responses.get(response_name)
        if not response:
            raise ValueError(f"OpenAPI missing reusable error response for {status}")
        schema_ref = response.get("content", {}).get("application/json", {}).get("schema", {}).get("$ref")
        if schema_ref != "#/components/schemas/ErrorEnvelope":
            raise ValueError(f"OpenAPI reusable response {response_name} must reference ErrorEnvelope")
    for response_name in ("Gone", "PayloadTooLarge"):
        if response_name not in responses:
            raise ValueError(f"OpenAPI missing reusable error response {response_name}")
    has_created_response = False
    has_accepted_response = False
    for route, methods in openapi.get("paths", {}).items():
        for method, operation in methods.items():
            if method.startswith("x-"):
                continue
            lower = method.lower()
            operation_id = operation.get("operationId", "")
            if (
                lower in {"post", "put", "patch"}
                and "requestBody" not in operation
                and operation_id not in BODYLESS_OPERATION_IDS
            ):
                raise ValueError(f"OpenAPI operation missing requestBody: {method.upper()} {route}")
            responses_for_op = operation.get("responses", {})
            has_created_response = has_created_response or "201" in responses_for_op
            has_accepted_response = has_accepted_response or "202" in responses_for_op
            success_status = OPERATION_SUCCESS_RESPONSES.get(operation_id, "200")
            for status in (success_status, *STANDARD_ERROR_RESPONSES):
                if status not in responses_for_op:
                    raise ValueError(f"OpenAPI operation missing {status} response: {method.upper()} {route}")
            if success_status != "200" and "200" in responses_for_op:
                raise ValueError(f"OpenAPI operation declares unreachable 200 response: {method.upper()} {route}")
            for status in OPERATION_REQUIRED_RESPONSES.get(operation_id, ()):
                if status not in responses_for_op:
                    raise ValueError(f"OpenAPI operation missing runtime {status} response: {method.upper()} {route}")
            expected_media_responses = BINARY_MEDIA_RESPONSES.get(
                operation_id,
                {success_status: {"application/json"}},
            )
            for status, expected_media_types in expected_media_responses.items():
                declared_media_types = set(responses_for_op.get(status, {}).get("content", {}))
                if declared_media_types != expected_media_types:
                    raise ValueError(
                        f"OpenAPI operation {operation_id} {status} media types drift: "
                        f"expected={sorted(expected_media_types)!r} actual={sorted(declared_media_types)!r}"
                    )
            success = responses_for_op.get(success_status, {})
            if success.get("description") == "ok":
                raise ValueError(f"OpenAPI operation uses placeholder ok response: {method.upper()} {route}")
            content = success.get("content", {})
            for media_type in expected_media_responses.get(success_status, set()):
                schema = content.get(media_type, {}).get("schema") if isinstance(content, dict) else None
                if not schema:
                    raise ValueError(
                        f"OpenAPI {success_status} {media_type} response missing schema: {method.upper()} {route}"
                    )
    if not has_created_response:
        raise ValueError("OpenAPI must include at least one 201 created command response")
    if not has_accepted_response:
        raise ValueError("OpenAPI must include at least one 202 accepted async command response")


def validate_queries_contract(openapi: dict[str, Any]) -> None:
    declared = load_yaml(CONTRACT_ROOT / "queries" / "queries.yaml")["queries"]
    get_operation_ids = sorted(
        operation["operationId"]
        for methods in openapi.get("paths", {}).values()
        for method, operation in methods.items()
        if method.lower() == "get"
    )
    if sorted(declared) != get_operation_ids:
        missing = sorted(set(get_operation_ids) - set(declared))
        extra = sorted(set(declared) - set(get_operation_ids))
        raise ValueError(f"queries.yaml drift from OpenAPI GET operationIds; missing={missing!r} extra={extra!r}")


def validate_command_schema_strictness(schema: dict[str, Any], definitions: dict[str, Any], path: str) -> None:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/definitions/"):
        schema = definitions[ref.removeprefix("#/definitions/")]
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{path} object schema must set additionalProperties=false")
        for name, child in schema.get("properties", {}).items():
            validate_command_schema_strictness(child, definitions, f"{path}.{name}")
    if schema.get("type") == "array":
        validate_command_schema_strictness(schema.get("items", {}), definitions, f"{path}[]")


def normalize_contract_schema(schema: dict[str, Any], openapi_schemas: dict[str, Any], command_definitions: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref.startswith("#/definitions/"):
            return normalize_contract_schema(command_definitions[ref.removeprefix("#/definitions/")], openapi_schemas, command_definitions)
        if ref.startswith("#/components/schemas/"):
            return normalize_contract_schema(openapi_schemas[ref.removeprefix("#/components/schemas/")], openapi_schemas, command_definitions)
    if "allOf" in schema:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            normalized = normalize_contract_schema(part, openapi_schemas, command_definitions)
            merged["properties"].update(normalized.get("properties", {}))
            merged["required"].extend(normalized.get("required", []))
            if normalized.get("additionalProperties") is False:
                merged["additionalProperties"] = False
        merged["required"] = sorted(set(merged["required"]))
        return merged
    normalized = {key: value for key, value in schema.items() if key != "$ref"}
    if "properties" in normalized:
        normalized["properties"] = {
            key: normalize_contract_schema(value, openapi_schemas, command_definitions)
            for key, value in normalized["properties"].items()
        }
    if "items" in normalized:
        normalized["items"] = normalize_contract_schema(normalized["items"], openapi_schemas, command_definitions)
    if "required" in normalized:
        normalized["required"] = sorted(normalized["required"])
    return normalized


def validate_command_payloads_match_openapi(openapi: dict[str, Any], commands: list[dict[str, Any]], command_definitions: dict[str, Any]) -> None:
    openapi_schemas = openapi["components"]["schemas"]
    operation_payloads: dict[str, dict[str, Any]] = {}
    for methods in openapi.get("paths", {}).values():
        for operation in methods.values():
            command_type = operation.get("x-command-type")
            if not command_type:
                continue
            schema_ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            envelope = openapi_schemas[schema_ref.removeprefix("#/components/schemas/")]
            for part in envelope.get("allOf", []):
                normalized = normalize_contract_schema(part, openapi_schemas, command_definitions)
                payload_schema = normalized.get("properties", {}).get("payload")
                if payload_schema:
                    operation_payloads[command_type] = normalize_contract_schema(payload_schema, openapi_schemas, command_definitions)
    for command in commands:
        runtime_schema = normalize_contract_schema(command["payload_schema"], openapi_schemas, command_definitions)
        openapi_schema = operation_payloads.get(command["command_type"])
        if runtime_schema != openapi_schema:
            raise ValueError(f"Command payload schema drift from OpenAPI: {command['command_type']}")


def validate_contracts() -> tuple[list[str], dict[str, int], list[dict[str, Any]], dict[str, Any], list[str], dict[str, Any]]:
    capabilities_data = load_yaml(CONTRACT_ROOT / "capabilities.yaml")
    errors = load_yaml(CONTRACT_ROOT / "errors.yaml")["errors"]
    openapi = load_yaml(CONTRACT_ROOT / "openapi.yaml")
    manifest = load_json(CONTRACT_ROOT / "module-manifest.json")
    events_data = load_yaml(CONTRACT_ROOT / "events" / "events.yaml")
    events = events_data["events"]
    event_payload_schemas = events_data.get("payload_schemas", {})
    if capabilities_data.get("contract_version") != "1.0":
        raise ValueError("capabilities contract_version must be 1.0")
    capability_groups = capabilities_data["capabilities"]
    capabilities = sorted({cap for group in capability_groups.values() for cap in group})
    if manifest["contract_version"] != "1.0":
        raise ValueError("module manifest contract_version must be 1.0")
    if sorted(manifest["capabilities"]) != capabilities:
        raise ValueError("module manifest capabilities drift from capabilities.yaml")
    for route, methods in openapi.get("paths", {}).items():
        if "delete" in {method.lower() for method in methods}:
            raise ValueError(f"DELETE is forbidden in OpenAPI contract: {route}")
    commands = collect_commands()
    validate_openapi_contract(openapi, errors, events, event_payload_schemas)
    validate_queries_contract(openapi)
    command_definitions = collect_command_definitions()
    validate_command_payloads_match_openapi(openapi, commands, command_definitions)
    for command in commands:
        if command["capability"] not in capabilities:
            raise ValueError(f"Unknown capability for {command['command_type']}: {command['capability']}")
        validate_command_schema_strictness(command["payload_schema"], command_definitions, command["command_type"])
    for event, payload_schema in event_payload_schemas.items():
        validate_command_schema_strictness(payload_schema, {}, event)
    return capabilities, errors, commands, manifest, events, event_payload_schemas


def py_template(
    capabilities: list[str],
    errors: dict[str, int],
    commands: list[dict[str, Any]],
    manifest: dict[str, Any],
    events: list[str],
    event_payload_schemas: dict[str, Any],
) -> str:
    command_capabilities = {command["command_type"]: command["capability"] for command in commands}
    idempotent_commands = sorted(command["command_type"] for command in commands if command.get("idempotent"))
    command_payload_schemas = {command["command_type"]: command["payload_schema"] for command in commands}
    command_schema_definitions = collect_command_definitions()
    error_code_literal = "Literal[" + ", ".join(repr(code) for code in errors) + "]"
    return f'''# Generated by backend/scripts/generate_contracts.py. Do not edit by hand.
from __future__ import annotations

import re

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CONTRACT_VERSION: Literal["1.0"] = "1.0"
REVIEW_CAPABILITIES: tuple[str, ...] = {tuple(capabilities)!r}
EDIT_ENTRY_PROFILE: tuple[str, ...] = {tuple(load_yaml(CONTRACT_ROOT / "capabilities.yaml")["capabilities"]["edit"])!r}
REVIEW_ENTRY_PROFILE: tuple[str, ...] = {tuple(load_yaml(CONTRACT_ROOT / "capabilities.yaml")["capabilities"]["review"])!r}
ERROR_HTTP_STATUS: dict[str, int] = {errors!r}
COMMAND_CAPABILITIES: dict[str, str] = {command_capabilities!r}
IDEMPOTENT_COMMANDS: tuple[str, ...] = {tuple(idempotent_commands)!r}
DOMAIN_EVENT_TYPES: tuple[str, ...] = {tuple(events)!r}
MODULE_MANIFEST: dict[str, Any] = {manifest!r}
COMMAND_PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {command_payload_schemas!r}
COMMAND_SCHEMA_DEFINITIONS: dict[str, dict[str, Any]] = {command_schema_definitions!r}
DOMAIN_EVENT_PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {event_payload_schemas!r}


def _resolve_schema(schema: dict[str, Any]) -> dict[str, Any]:
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/definitions/"):
        return COMMAND_SCHEMA_DEFINITIONS[ref.removeprefix("#/definitions/")]
    return schema


def _validate_contract_schema(schema: dict[str, Any], value: Any, path: str) -> None:
    schema = _resolve_schema(schema)
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{{path}} must equal {{schema['const']!r}}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{{path}} must be one of {{schema['enum']!r}}")
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{{path}} must be an object")
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{{path}}.{{key}} is required")
        if schema.get("additionalProperties") is False:
            allowed = set(schema.get("properties", {{}}))
            extra = sorted(set(value) - allowed)
            if extra:
                raise ValueError(f"{{path}} has unknown fields {{extra!r}}")
        for key, child_schema in schema.get("properties", {{}}).items():
            if key in value and value[key] is not None:
                _validate_contract_schema(child_schema, value[key], f"{{path}}.{{key}}")
        return
    if schema_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{{path}} must be an array")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            raise ValueError(f"{{path}} has too many items")
        item_schema = schema.get("items", {{}})
        for index, item in enumerate(value):
            _validate_contract_schema(item_schema, item, f"{{path}}[{{index}}]")
        return
    if schema_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{{path}} must be a string")
        if len(value) < int(schema.get("minLength", 0)):
            raise ValueError(f"{{path}} is too short")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            raise ValueError(f"{{path}} is too long")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(f"{{path}} does not match required pattern")
        return
    if schema_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{{path}} must be an integer")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{{path}} must be >= {{schema['minimum']}}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{{path}} must be <= {{schema['maximum']}}")
        return
    if schema_type == "number":
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ValueError(f"{{path}} must be a number")
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{{path}} must be >= {{schema['minimum']}}")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{{path}} must be <= {{schema['maximum']}}")
        return
    if schema_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{{path}} must be a boolean")
        return


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ErrorBody(ContractModel):
    code: {error_code_literal}
    message: str
    http_status: int = Field(ge=400, le=599)
    details: dict[str, Any]
    request_id: str
    timestamp: str
    contract_version: Literal["1.0"] = "1.0"

    @model_validator(mode="after")
    def validate_error_status(self) -> "ErrorBody":
        expected = ERROR_HTTP_STATUS[self.code]
        if self.http_status != expected:
            raise ValueError(f"error.http_status must match ERROR_HTTP_STATUS for {{self.code}}")
        return self


class ErrorEnvelope(ContractModel):
    error: ErrorBody


class ResponseMeta(ContractModel):
    request_id: str
    contract_version: Literal["1.0"] = "1.0"
    total_count: int | None = None
    page: int | None = None
    page_size: int | None = None


class ListResponseMeta(ContractModel):
    request_id: str
    contract_version: Literal["1.0"] = "1.0"
    total_count: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)


class Envelope(ContractModel):
    data: Any
    meta: ResponseMeta


class CommandEnvelope(ContractModel):
    command_id: str = Field(min_length=1, max_length=256)
    command_type: str
    contract_version: Literal["1.0"] = "1.0"
    expected_aggregate_version: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload_schema(self) -> "CommandEnvelope":
        schema = COMMAND_PAYLOAD_SCHEMAS.get(self.command_type)
        if schema is None:
            raise ValueError(f"Unknown command_type {{self.command_type}}")
        _validate_contract_schema(schema, self.payload, "payload")
        return self


def validate_domain_event_payload(event_type: str, payload: dict[str, Any]) -> None:
    schema = DOMAIN_EVENT_PAYLOAD_SCHEMAS.get(event_type)
    if schema is None:
        raise ValueError(f"Unknown domain event type {{event_type}}")
    _validate_contract_schema(schema, payload, "payload")


class ReviewAnnotationShape(ContractModel):
    id: str
    tool_type: Literal["pen", "arrow", "rect", "circle", "text"]
    anchor_points: list[dict[str, float]] | None = None
    path_data: str | None = None
    text_content: str | None = None
    color: str
    line_width: int
    font_size: int | None = None
    z_index: int


class ReviewAnnotationSetInput(ContractModel):
    canvas_width: int
    canvas_height: int
    video_width: int
    video_height: int
    shapes: list[ReviewAnnotationShape]


class ReviewPlaybackTarget(ContractModel):
    project_ref_id: str
    review_item_id: str
    version_id: str
    issue_id: str
    revision_id: str
    annotation_set_id: str | None = None
    timestamp_ms: int
    frame_number: int


class OriginalMediaSnapshotDTO(ContractModel):
    original_file_id: str
    original_filename: str
    mime_type: str
    file_size: int
    sha256: str
    duration_ms: int
    width: int
    height: int
    fps_num: int
    fps_den: int
    media_probe_version: str


class ProjectDTO(ContractModel):
    project_ref_id: str
    project_code: str
    project_name: str
    description: str
    source: Literal["local", "host"]
    external_project_id: str | None = None
    lifecycle_status: Literal["active", "archived"]
    completion_status: Literal["empty", "in_progress", "completed"]
    deleted_at: str | None = None
    lock_version: int
    created_at: str
    updated_at: str


class ReviewVersionDTO(ContractModel):
    id: str
    project_ref_id: str
    review_item_id: str
    previous_version_id: str | None = None
    version_no: int
    version_label: str
    is_current: bool
    original_media: OriginalMediaSnapshotDTO
    playback_status: Literal["processing", "ready", "failed"]
    playback_asset_id: str | None = None
    thumbnail_asset_id: str | None = None
    version_note: str | None = None
    change_summary: str | None = None
    lock_version: int
    created_at: str


class ReviewIssueRevisionDTO(ContractModel):
    id: str
    project_ref_id: str
    review_item_id: str
    version_id: str
    issue_id: str
    revision_no: int
    content: str
    annotation_set_id: str | None = None
    created_at: str


class ReviewAnnotationSetDTO(ContractModel):
    id: str
    project_ref_id: str
    review_item_id: str
    version_id: str
    issue_id: str
    timestamp_ms: int
    frame_number: int
    canvas_width: int
    canvas_height: int
    video_width: int
    video_height: int
    shapes: list[ReviewAnnotationShape]
    created_at: str


class ReviewIssueDTO(ContractModel):
    id: str
    project_ref_id: str
    review_item_id: str
    version_id: str
    issue_no: int
    status: Literal["unresolved", "resolved"]
    current_revision_id: str
    timestamp_ms: int
    frame_number: int
    playback_target: ReviewPlaybackTarget
    current_revision: ReviewIssueRevisionDTO
    current_annotation_set: ReviewAnnotationSetDTO | None = None
    deleted_at: str | None = None
    lock_version: int
    created_at: str
    updated_at: str


class ReviewItemDTO(ContractModel):
    id: str
    project_ref_id: str
    item_code: str
    episode_no: int | None = None
    title: str
    workflow_status: Literal["pending_review", "in_review", "changes_requested", "finalized"]
    current_version_id: str
    current_version_no: int
    ui_status: str
    active_finalization_id: str | None = None
    unresolved_current_version_count: int
    resolved_current_version_count: int
    historical_version_count: int
    is_finalized: bool
    lock_version: int
    created_at: str
    updated_at: str


class ThreadMessageDTO(ContractModel):
    id: str
    project_ref_id: str
    review_item_id: str
    version_id: str
    issue_id: str
    content: str
    created_at: str


class FinalizationDTO(ContractModel):
    id: str
    project_ref_id: str
    review_item_id: str
    version_id: str
    version_no: int
    original_media: OriginalMediaSnapshotDTO
    status: Literal["active"]
    finalized_at: str


class PackageSnapshotItemDTO(ContractModel):
    review_item_id: str
    version_id: str
    finalization_id: str
    original_file_id: str
    original_filename: str
    sha256: str
    archive_name: str


class PackageSnapshotDTO(ContractModel):
    id: str
    project_ref_id: str
    status: Literal["preparing", "ready", "failed", "expired"]
    package_filename: str
    expires_at: str
    file_count: int
    total_bytes: int
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{{64}}$")
    download_token: str | None = None
    download_token_expires_at: str | None = None
    created_at: str
    updated_at: str
    failure_details: dict[str, Any] | None = None
    items: list[PackageSnapshotItemDTO]


class PackageDownloadSessionDTO(ContractModel):
    status: Literal["ready"]


class UploadInitRequest(ContractModel):
    original_filename: str = Field(min_length=1, max_length=512)
    mime_type: Literal["video/mp4", "video/quicktime", "application/octet-stream"]
    file_size: int = Field(ge=1)
    sha256: str = Field(pattern="^[0-9a-f]{{64}}$")
    duration_ms: int = Field(default=1000, ge=1)
    width: int = Field(default=1920, ge=1)
    height: int = Field(default=1080, ge=1)
    fps_num: int = Field(default=25, ge=1)
    fps_den: int = Field(default=1, ge=1)


class UploadSessionDTO(ContractModel):
    upload_id: str
    status: Literal["initiated", "receiving", "completed", "aborted"]
    original_filename: str
    mime_type: str
    declared_size: int
    received_size: int
    file_id: str | None = None
'''


def type_name_from_command(command_type: str) -> str:
    return f"{command_type}Payload"


def type_name_from_event(event_type: str) -> str:
    return EVENT_PAYLOAD_SCHEMA_NAMES[event_type].removesuffix("EventPayload") + "Payload"


def schema_to_ts_type(schema: dict[str, Any], definitions: dict[str, Any]) -> str:
    ref = schema.get("$ref")
    if isinstance(ref, str):
        if ref == "#/definitions/review_annotation_set_input":
            return "ReviewAnnotationSetInput"
        if ref.startswith("#/definitions/"):
            return schema_to_ts_type(definitions[ref.removeprefix("#/definitions/")], definitions)
    if "const" in schema:
        return json.dumps(schema["const"])
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return " | ".join(json.dumps(value) for value in enum)
    schema_type = schema.get("type")
    if schema_type == "string":
        return "string"
    if schema_type in {"integer", "number"}:
        return "number"
    if schema_type == "boolean":
        return "boolean"
    if schema_type == "array":
        return f"{schema_to_ts_type(schema.get('items', {}), definitions)}[]"
    if schema_type == "object":
        properties = schema.get("properties", {})
        if not properties and schema.get("additionalProperties") is False:
            return "Record<string, never>"
        if schema.get("additionalProperties") is True:
            return "Record<string, unknown>"
        required = set(schema.get("required", []))
        parts = []
        for name, child in properties.items():
            optional = "" if name in required else "?"
            parts.append(f"{json.dumps(name)}{optional}: {schema_to_ts_type(child, definitions)}")
        return "{ " + "; ".join(parts) + " }"
    return "unknown"


def schema_to_ts_interface(name: str, schema: dict[str, Any], definitions: dict[str, Any]) -> str:
    schema = normalize_contract_schema(schema, {}, definitions)
    if schema.get("type") != "object":
        return f"export type {name} = {schema_to_ts_type(schema, definitions)};"
    properties = schema.get("properties", {})
    if not properties and schema.get("additionalProperties") is False:
        return f"export type {name} = Record<string, never>;"
    required = set(schema.get("required", []))
    lines = [f"export interface {name} {{"]
    for property_name, property_schema in properties.items():
        optional = "" if property_name in required else "?"
        lines.append(f"  {property_name}{optional}: {schema_to_ts_type(property_schema, definitions)};")
    lines.append("}")
    return "\n".join(lines)


def payload_interfaces_ts(commands: list[dict[str, Any]]) -> str:
    definitions = collect_command_definitions()
    return "\n\n".join(
        schema_to_ts_interface(type_name_from_command(command["command_type"]), command["payload_schema"], definitions)
        for command in commands
    )


def event_payload_interfaces_ts(events: list[str], event_payload_schemas: dict[str, Any]) -> str:
    blocks = [
        schema_to_ts_interface(type_name_from_event(event), event_payload_schemas[event], {})
        for event in events
    ]
    union = " | ".join(type_name_from_event(event) for event in events)
    blocks.append(f"export type DomainEventPayload = {union};")
    return "\n\n".join(blocks)


def ts_template(capabilities: list[str], errors: dict[str, int], commands: list[dict[str, Any]], manifest: dict[str, Any], events: list[str], event_payload_schemas: dict[str, Any]) -> str:
    command_capabilities = {command["command_type"]: command["capability"] for command in commands}
    idempotent_commands = sorted(command["command_type"] for command in commands if command.get("idempotent"))
    edit_profile = load_yaml(CONTRACT_ROOT / "capabilities.yaml")["capabilities"]["edit"]
    review_profile = load_yaml(CONTRACT_ROOT / "capabilities.yaml")["capabilities"]["review"]
    cap_union = " | ".join(json.dumps(cap) for cap in capabilities)
    command_union = " | ".join(json.dumps(command["command_type"]) for command in commands)
    event_union = " | ".join(json.dumps(event) for event in events)
    error_status_union = " | ".join(str(status) for status in sorted(set(errors.values())))
    command_payload_interfaces = payload_interfaces_ts(commands)
    event_payload_interfaces = event_payload_interfaces_ts(events, event_payload_schemas)
    return f'''// Generated by backend/scripts/generate_contracts.py. Do not edit by hand.
export const CONTRACT_VERSION = "1.0" as const;
export const REVIEW_CAPABILITIES = {json.dumps(capabilities, ensure_ascii=False)} as const;
export type ReviewCapability = {cap_union};
export const EDIT_ENTRY_PROFILE = {json.dumps(edit_profile, ensure_ascii=False)} as const;
export const REVIEW_ENTRY_PROFILE = {json.dumps(review_profile, ensure_ascii=False)} as const;
export type CommandType = {command_union};
export const IDEMPOTENT_COMMANDS = {json.dumps(idempotent_commands, ensure_ascii=False)} as const;
export type DomainEventType = {event_union};
export const DOMAIN_EVENT_TYPES = {json.dumps(events, ensure_ascii=False)} as const;
export const DOMAIN_EVENT_PAYLOAD_SCHEMAS = {json.dumps(event_payload_schemas, ensure_ascii=False, indent=2)} as const;
export const ERROR_HTTP_STATUS = {json.dumps(errors, ensure_ascii=False, indent=2)} as const;
export type ErrorCode = keyof typeof ERROR_HTTP_STATUS;
export type ErrorHttpStatus = {error_status_union};
export const COMMAND_CAPABILITIES = {json.dumps(command_capabilities, ensure_ascii=False, indent=2)} as const;
export const MODULE_MANIFEST = {json.dumps(manifest, ensure_ascii=False, indent=2)} as const;

export interface ErrorBody {{
  code: ErrorCode;
  message: string;
  http_status: ErrorHttpStatus;
  details: Record<string, unknown>;
  request_id: string;
  timestamp: string;
  contract_version: "1.0";
}}

export interface ResponseMeta {{
  request_id: string;
  contract_version: "1.0";
  total_count?: number;
  page?: number;
  page_size?: number;
}}

export interface ListResponseMeta {{
  request_id: string;
  contract_version: "1.0";
  total_count: number;
  page: number;
  page_size: number;
}}

export interface Envelope<T> {{
  data: T;
  meta: ResponseMeta;
}}

export interface ErrorEnvelope {{
  error: ErrorBody;
}}

export interface CommandEnvelope<TPayload extends object> {{
  command_id: string;
  command_type: CommandType;
  contract_version: "1.0";
  expected_aggregate_version?: number;
  payload: TPayload;
}}

export interface ReviewAnnotationShape {{
  id: string;
  tool_type: "pen" | "arrow" | "rect" | "circle" | "text";
  anchor_points?: Array<{{ x: number; y: number }}>;
  path_data?: string;
  text_content?: string;
  color: string;
  line_width: number;
  font_size?: number;
  z_index: number;
}}

export interface ReviewAnnotationSetInput {{
  canvas_width: number;
  canvas_height: number;
  video_width: number;
  video_height: number;
  shapes: ReviewAnnotationShape[];
}}

export interface ReviewPlaybackTarget {{
  project_ref_id: string;
  review_item_id: string;
  version_id: string;
  issue_id: string;
  revision_id: string;
  annotation_set_id?: string;
  timestamp_ms: number;
  frame_number: number;
}}

export interface OriginalMediaSnapshotDTO {{
  original_file_id: string;
  original_filename: string;
  mime_type: string;
  file_size: number;
  sha256: string;
  duration_ms: number;
  width: number;
  height: number;
  fps_num: number;
  fps_den: number;
  media_probe_version: string;
}}

export interface ProjectDTO {{
  project_ref_id: string;
  project_code: string;
  project_name: string;
  description: string;
  source: "local" | "host";
  external_project_id?: string | null;
  lifecycle_status: "active" | "archived";
  completion_status: "empty" | "in_progress" | "completed";
  deleted_at?: string | null;
  lock_version: number;
  created_at: string;
  updated_at: string;
}}

export interface ReviewVersionDTO {{
  id: string;
  project_ref_id: string;
  review_item_id: string;
  previous_version_id?: string | null;
  version_no: number;
  version_label: string;
  is_current: boolean;
  original_media: OriginalMediaSnapshotDTO;
  playback_status: "processing" | "ready" | "failed";
  playback_asset_id?: string | null;
  thumbnail_asset_id?: string | null;
  version_note?: string | null;
  change_summary?: string | null;
  lock_version: number;
  created_at: string;
}}

export interface ReviewIssueRevisionDTO {{
  id: string;
  project_ref_id: string;
  review_item_id: string;
  version_id: string;
  issue_id: string;
  revision_no: number;
  content: string;
  annotation_set_id?: string | null;
  created_at: string;
}}

export interface ReviewAnnotationSetDTO {{
  id: string;
  project_ref_id: string;
  review_item_id: string;
  version_id: string;
  issue_id: string;
  timestamp_ms: number;
  frame_number: number;
  canvas_width: number;
  canvas_height: number;
  video_width: number;
  video_height: number;
  shapes: ReviewAnnotationShape[];
  created_at: string;
}}

export interface ReviewIssueDTO {{
  id: string;
  project_ref_id: string;
  review_item_id: string;
  version_id: string;
  issue_no: number;
  status: "unresolved" | "resolved";
  current_revision_id: string;
  timestamp_ms: number;
  frame_number: number;
  playback_target: ReviewPlaybackTarget;
  current_revision: ReviewIssueRevisionDTO;
  current_annotation_set?: ReviewAnnotationSetDTO | null;
  deleted_at?: string | null;
  lock_version: number;
  created_at: string;
  updated_at: string;
}}

export interface ReviewItemDTO {{
  id: string;
  project_ref_id: string;
  item_code: string;
  episode_no?: number | null;
  title: string;
  workflow_status: "pending_review" | "in_review" | "changes_requested" | "finalized";
  current_version_id: string;
  current_version_no: number;
  ui_status: string;
  active_finalization_id?: string | null;
  unresolved_current_version_count: number;
  resolved_current_version_count: number;
  historical_version_count: number;
  is_finalized: boolean;
  lock_version: number;
  created_at: string;
  updated_at: string;
}}

export interface ThreadMessageDTO {{
  id: string;
  project_ref_id: string;
  review_item_id: string;
  version_id: string;
  issue_id: string;
  content: string;
  created_at: string;
}}

export interface FinalizationDTO {{
  id: string;
  project_ref_id: string;
  review_item_id: string;
  version_id: string;
  version_no: number;
  original_media: OriginalMediaSnapshotDTO;
  status: "active";
  finalized_at: string;
}}

export interface PackageSnapshotItem {{
  review_item_id: string;
  version_id: string;
  finalization_id: string;
  original_file_id: string;
  original_filename: string;
  sha256: string;
  archive_name: string;
}}

export interface PackageSnapshotDTO {{
  id: string;
  project_ref_id: string;
  status: "preparing" | "ready" | "failed" | "expired";
  package_filename: string;
  expires_at: string;
  file_count: number;
  total_bytes: number;
  sha256?: string;
  download_token?: string;
  download_token_expires_at?: string;
  created_at: string;
  updated_at: string;
  failure_details?: Record<string, unknown>;
  items: PackageSnapshotItem[];
}}

export interface PackageDownloadSessionDTO {{
  status: "ready";
}}

export interface UploadInitRequest {{
  original_filename: string;
  mime_type: "video/mp4" | "video/quicktime" | "application/octet-stream";
  file_size: number;
  sha256: string;
  duration_ms?: number;
  width?: number;
  height?: number;
  fps_num?: number;
  fps_den?: number;
}}

export interface UploadSessionDTO {{
  upload_id: string;
  status: "initiated" | "receiving" | "completed" | "aborted";
  original_filename: string;
  mime_type: string;
  declared_size: number;
  received_size: number;
  file_id?: string | null;
}}

{command_payload_interfaces}

export type EmptyPayload = Record<string, never>;

{event_payload_interfaces}

export interface DomainEventEnvelope {{
  event_id: string;
  event_type: DomainEventType;
  event_version: number;
  occurred_at: string;
  aggregate_type: string;
  aggregate_id: string;
  aggregate_version: number;
  sequence: number;
  project_ref_id: string;
  review_item_id?: string | null;
  version_id?: string | null;
  issue_id?: string | null;
  finalization_id?: string | null;
  package_id?: string | null;
  correlation_id: string;
  causation_id?: string | null;
  metadata: Record<string, unknown>;
  payload: DomainEventPayload;
}}

export class FinalCutReviewHttpError extends Error {{
  readonly code: ErrorCode;
  readonly httpStatus: number;
  readonly details: Record<string, unknown>;
  readonly requestId: string;
  readonly timestamp: string;
  readonly contractVersion: string;

  constructor(error: ErrorBody, fallbackStatus: number) {{
    super(error.message);
    this.name = "FinalCutReviewHttpError";
    this.code = error.code;
    this.httpStatus = error.http_status ?? fallbackStatus;
    this.details = error.details;
    this.requestId = error.request_id;
    this.timestamp = error.timestamp;
    this.contractVersion = error.contract_version;
  }}
}}

export async function finalCutReviewRequest<T>(
  baseUrl: string,
  path: string,
  init: RequestInit = {{}},
): Promise<T> {{
  const response = await fetch(`${{baseUrl}}${{path}}`, {{
    ...init,
    headers: {{
      "Content-Type": "application/json",
      ...(init.headers ?? {{}}),
    }},
  }});
  const body = (await response.json()) as Envelope<T> | ErrorEnvelope;
  if (!response.ok) {{
    const error = (body as ErrorEnvelope).error;
    if (error) {{
      throw new FinalCutReviewHttpError(error, response.status);
    }}
    throw new Error(`HTTP ${{response.status}}`);
  }}
  const envelope = body as Envelope<T>;
  return envelope.data as T;
}}

export class FinalCutReviewClient {{
  constructor(private readonly baseUrl: string) {{}}

  request<T>(path: string, init: RequestInit = {{}}): Promise<T> {{
    return finalCutReviewRequest<T>(this.baseUrl, path, init);
  }}

  listProjects(init: RequestInit = {{}}): Promise<ProjectDTO[]> {{
    return this.request<ProjectDTO[]>("/api/v1/final-cut-review/projects", init);
  }}

  getProject(projectRefId: string, init: RequestInit = {{}}): Promise<ProjectDTO> {{
    return this.request<ProjectDTO>(`/api/v1/final-cut-review/projects/${{projectRefId}}`, init);
  }}

  command<TData, TPayload extends object>(
    path: string,
    envelope: CommandEnvelope<TPayload>,
    init: RequestInit = {{}},
  ): Promise<TData> {{
    return this.request<TData>(path, {{
      ...init,
      method: init.method ?? "POST",
      body: JSON.stringify(envelope),
    }});
  }}
}}
'''


def ts_templates(
    capabilities: list[str],
    errors: dict[str, int],
    commands: list[dict[str, Any]],
    manifest: dict[str, Any],
    events: list[str],
    event_payload_schemas: dict[str, Any],
) -> dict[str, str]:
    monolith = ts_template(capabilities, errors, commands, manifest, events, event_payload_schemas)
    metadata, remainder = monolith.split("export interface ErrorBody", 1)
    models, remainder = remainder.split("export interface FinalizeVersionPayload", 1)
    payloads, client = remainder.split("export class FinalCutReviewHttpError", 1)
    generated = "// Generated by backend/scripts/generate_contracts.py. Do not edit by hand.\n"
    outputs = {
        "backend-contract-metadata.ts": metadata,
        "backend-contract-models.ts": (
            generated
            + 'import type { CommandType, ErrorCode, ErrorHttpStatus } from "./backend-contract-metadata";\n\n'
            + "export interface ErrorBody"
            + models
        ),
        "backend-contract-payloads.ts": (
            generated
            + 'import type { DomainEventType } from "./backend-contract-metadata";\n'
            + "\n"
            + "export interface FinalizeVersionPayload"
            + payloads
        ),
        "backend-contract-client.ts": (
            generated
            + 'import type { ErrorCode } from "./backend-contract-metadata";\n'
            + 'import type { CommandEnvelope, Envelope, ErrorBody, ErrorEnvelope, ProjectDTO } from "./backend-contract-models";\n\n'
            + "export class FinalCutReviewHttpError"
            + client
        ),
        "backend-contract.ts": (
            generated
            + 'export * from "./backend-contract-metadata";\n'
            + 'export * from "./backend-contract-models";\n'
            + 'export * from "./backend-contract-payloads";\n'
            + 'export * from "./backend-contract-client";\n'
        ),
    }
    outputs = {name: content.rstrip() + "\n" for name, content in outputs.items()}
    oversized = {name: len(content.splitlines()) for name, content in outputs.items() if len(content.splitlines()) > 300}
    if oversized:
        raise ValueError(f"Generated TypeScript modules exceed 300 lines: {oversized!r}")
    return outputs


def write_outputs(py_out: Path, ts_out: Path) -> None:
    capabilities, errors, commands, manifest, events, event_payload_schemas = validate_contracts()
    py_out.parent.mkdir(parents=True, exist_ok=True)
    ts_out.parent.mkdir(parents=True, exist_ok=True)
    py_out.write_text(py_template(capabilities, errors, commands, manifest, events, event_payload_schemas), encoding="utf-8")
    for name, content in ts_templates(capabilities, errors, commands, manifest, events, event_payload_schemas).items():
        (ts_out.parent / name).write_text(content, encoding="utf-8")


def check_outputs() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        py_tmp = tmp_path / "generated.py"
        ts_tmp = tmp_path / "backend-contract.ts"
        write_outputs(py_tmp, ts_tmp)
        diffs: list[str] = []
        generated_pairs = [(py_tmp, PY_OUT)]
        generated_pairs.extend(
            (ts_tmp.parent / name, TS_OUT.parent / name)
            for name in ts_templates(*validate_contracts()).keys()
        )
        for expected, actual in generated_pairs:
            if not actual.exists() or actual.read_text(encoding="utf-8") != expected.read_text(encoding="utf-8"):
                diffs.append(str(actual.relative_to(ROOT)))
        if diffs:
            print("Contract generated files are stale: " + ", ".join(diffs), file=sys.stderr)
            return 1
    print("contract generation check ok")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        return check_outputs()
    write_outputs(PY_OUT, TS_OUT)
    print(f"generated {PY_OUT.relative_to(ROOT)}")
    for name in ts_templates(*validate_contracts()):
        print(f"generated {(TS_OUT.parent / name).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
