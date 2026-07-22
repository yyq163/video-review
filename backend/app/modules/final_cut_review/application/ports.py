from __future__ import annotations

from typing import Any, Protocol

from .context import ExecutionContext


class EntryPolicyPort(Protocol):
    def allows(self, entry_source: str, capability: str) -> bool: ...


class WriteGuardPort(Protocol):
    def assert_write_allowed(self, context: ExecutionContext) -> None: ...


class PrincipalAuthorizationPort(Protocol):
    def authorize(self, context: ExecutionContext, capability: str, resource: dict[str, str | None]) -> None: ...


class ReviewRepositoryPort(Protocol):
    def execute(self, command_type: str, payload: dict[str, Any], context: ExecutionContext, expected_version: int | None) -> dict[str, Any]: ...
    def get_idempotency(self, key: str) -> tuple[str, dict[str, Any]] | None: ...
    def reserve_idempotency(self, key: str, command_type: str, request_hash: str, principal_hash: str) -> dict[str, Any] | None: ...
    def complete_idempotency(self, key: str, response: dict[str, Any]) -> None: ...
