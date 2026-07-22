from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrincipalRef:
    kind: str = "anonymous"
    id: str | None = None
    project_ref_ids: tuple[str, ...] = ()

    def can_access_project(self, project_ref_id: str) -> bool:
        return "*" in self.project_ref_ids or project_ref_id in self.project_ref_ids


@dataclass(frozen=True)
class WriteGuardState:
    mode: str
    verified: bool = False


@dataclass(frozen=True)
class ExecutionContext:
    entry_source: str
    request_id: str
    principal: PrincipalRef
    write_guard: WriteGuardState
    remote_addr: str | None = None
    user_agent: str | None = None
