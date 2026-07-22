from __future__ import annotations

import hashlib
import json
from typing import Any

from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.review_contracts.generated import COMMAND_CAPABILITIES, IDEMPOTENT_COMMANDS

from .context import ExecutionContext
from .ports import EntryPolicyPort, PrincipalAuthorizationPort, ReviewRepositoryPort, WriteGuardPort


def canonical_request_hash(command_type: str, payload: dict[str, Any]) -> str:
    body = {"command_type": command_type, "payload": payload}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def principal_fingerprint(context: ExecutionContext) -> str:
    principal = context.principal
    body = {
        "kind": principal.kind,
        "id": principal.id,
        "project_ref_ids": sorted(principal.project_ref_ids),
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class CommandBus:
    def __init__(
        self,
        repository: ReviewRepositoryPort,
        entry_policy: EntryPolicyPort,
        write_guard: WriteGuardPort,
        principal_authorization: PrincipalAuthorizationPort,
    ) -> None:
        self.repository = repository
        self.entry_policy = entry_policy
        self.write_guard = write_guard
        self.principal_authorization = principal_authorization

    def execute(
        self,
        command_type: str,
        payload: dict[str, Any],
        context: ExecutionContext,
        expected_version: int | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        capability = COMMAND_CAPABILITIES.get(command_type)
        if not capability:
            raise ReviewError("VALIDATION_ERROR", f"未知命令 {command_type}")
        self.write_guard.assert_write_allowed(context)
        if not self.entry_policy.allows(context.entry_source, capability):
            raise ReviewError("ENTRY_CAPABILITY_DENIED", "当前入口不能执行该能力")
        self.principal_authorization.authorize(context, capability, self._resource(payload))

        request_hash = canonical_request_hash(command_type, payload)
        reserved_key: str | None = None
        if command_type in IDEMPOTENT_COMMANDS:
            if not idempotency_key:
                raise ReviewError("VALIDATION_ERROR", "缺少 Idempotency-Key")
            replay = self.repository.reserve_idempotency(idempotency_key, command_type, request_hash, principal_fingerprint(context))
            if replay is not None:
                return replay
            reserved_key = idempotency_key
        response = self.repository.execute(command_type, payload, context, expected_version)
        if reserved_key:
            self.repository.complete_idempotency(reserved_key, response)
        return response

    @staticmethod
    def _resource(payload: dict[str, Any]) -> dict[str, str | None]:
        return {
            "project_ref_id": payload.get("project_ref_id"),
            "review_item_id": payload.get("review_item_id"),
            "version_id": payload.get("version_id"),
            "issue_id": payload.get("issue_id"),
            "finalization_id": payload.get("finalization_id"),
            "package_id": payload.get("package_id"),
        }
