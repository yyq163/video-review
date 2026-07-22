from __future__ import annotations

import uuid
from collections.abc import Generator

from fastapi import Header, Request
from sqlalchemy.orm import Session

from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.application.command_handlers import CommandBus
from backend.app.modules.final_cut_review.infra.database import get_session
from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
from backend.app.modules.review_access.policies import (
    ConfiguredWriteGuardAdapter,
    NoAccountAuthorizationAdapter,
    PrincipalContextSigner,
    StaticEntryPolicyAdapter,
    WriteGuardSessionSigner,
)
from backend.app.settings import Settings, get_settings


def get_request_id(request: Request, x_request_id: str | None = Header(default=None)) -> str:
    request_id = getattr(request.state, "request_id", None) or x_request_id
    if request_id:
        request.state.request_id = request_id
        return str(request_id)
    # The request tracing middleware owns generation for normal app traffic.
    # This fallback keeps direct dependency tests deterministic without adding
    # a second request-id source in production.
    generated = uuid.uuid4().hex
    request.state.request_id = generated
    return generated


def _split_csv_header(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def is_trusted_proxy_request(request: Request, settings: Settings) -> bool:
    remote = request.client.host if request.client else None
    trusted_hosts = set(_split_csv_header(settings.reverse_proxy_trusted_hosts))
    return remote in trusted_hosts


def trusted_client_host(request: Request, settings: Settings) -> str:
    remote = request.client.host if request.client else "unknown"
    if not is_trusted_proxy_request(request, settings):
        return remote
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
    return forwarded_for or remote


def write_guard_attempt_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def trusted_request_scheme(request: Request, settings: Settings) -> str:
    if not is_trusted_proxy_request(request, settings):
        return request.url.scheme
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    if forwarded_proto not in {"http", "https"}:
        raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "可信代理必须提供有效请求协议")
    return forwarded_proto


def _principal_from_headers(request: Request, settings: Settings) -> PrincipalRef:
    token = request.headers.get("x-principal-context")
    if not token:
        return PrincipalRef()
    return PrincipalContextSigner(settings).verify(token)


def is_verified_reverse_proxy_write(request: Request, settings: Settings) -> bool:
    header_name = settings.reverse_proxy_write_header.lower()
    return is_trusted_proxy_request(request, settings) and request.headers.get(header_name) == "true"


def build_context(
    entry_source: str,
    request: Request,
    request_id: str,
    fj_write_guard: str | None = None,
    x_write_guard_verified: str | None = None,
) -> ExecutionContext:
    settings = get_settings()
    signer = WriteGuardSessionSigner(settings)
    verified = settings.write_guard_mode == "none"
    if settings.write_guard_mode == "shared_code":
        verified = signer.verify(fj_write_guard)
    if settings.write_guard_mode == "reverse_proxy":
        del x_write_guard_verified
        verified = is_verified_reverse_proxy_write(request, settings)
    return ExecutionContext(
        entry_source=entry_source,
        request_id=request_id,
        principal=_principal_from_headers(request, settings),
        write_guard=WriteGuardState(mode=settings.write_guard_mode, verified=verified),
        remote_addr=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def get_repository(session: Session) -> SqlAlchemyReviewRepository:
    return SqlAlchemyReviewRepository(session, get_settings())


def get_command_bus(session: Session) -> CommandBus:
    settings = get_settings()
    repo = SqlAlchemyReviewRepository(session, settings)
    signer = WriteGuardSessionSigner(settings)
    return CommandBus(
        repository=repo,
        entry_policy=StaticEntryPolicyAdapter(),
        write_guard=ConfiguredWriteGuardAdapter(settings, signer),
        principal_authorization=NoAccountAuthorizationAdapter(),
    )


def session_dependency() -> Generator[Session, None, None]:
    yield from get_session()
