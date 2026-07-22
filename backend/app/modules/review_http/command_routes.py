from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from backend.app.modules.final_cut_review.application.command_handlers import CommandBus
from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef, WriteGuardState
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra.database import SessionLocal
from backend.app.modules.final_cut_review.infra.database import RuntimeWriterFenceUnavailable
from backend.app.modules.final_cut_review.infra.repositories import SqlAlchemyReviewRepository
from backend.app.modules.review_access.policies import SHARED_CODE_MAX_LENGTH, ConfiguredWriteGuardAdapter, WriteGuardSessionSigner
from backend.app.modules.review_contracts.generated import COMMAND_CAPABILITIES, IDEMPOTENT_COMMANDS, CommandEnvelope, ErrorEnvelope
from backend.app.settings import get_settings

from .context_dependencies import build_context, get_command_bus, get_request_id, get_repository, session_dependency, trusted_request_scheme, write_guard_attempt_key
from .envelope import ok

LOGGER = logging.getLogger(__name__)
COMMAND_ROUTE_TYPES = {
    "edit_create_project": "CreateProject",
    "edit_update_project": "UpdateProject",
    "review_archive_project": "ArchiveProject",
    "review_restore_project": "RestoreProject",
    "review_soft_delete_project": "SoftDeleteProject",
    "edit_create_item": "CreateReviewItem",
    "edit_update_item": "UpdateReviewItem",
    "edit_delete_item": "DeleteReviewItem",
    "edit_upload_version": "UploadReviewVersion",
    "review_start": "StartReview",
    "review_create_issue": "CreateReviewIssue",
    "review_update_issue": "UpdateReviewIssue",
    "review_add_message": "AddReviewMessage",
    "review_resolve_issue": "ResolveReviewIssue",
    "review_reopen_issue": "ReopenReviewIssue",
    "review_soft_delete_issue": "SoftDeleteReviewIssue",
    "review_request_changes": "RequestChanges",
    "review_finalize": "FinalizeVersion",
    "review_prepare_package": "PrepareFinalizedPackage",
}


def request_validation_stage(exc: RequestValidationError) -> str:
    errors = exc.errors()
    if any("command_type" in str(error.get("msg", "")) for error in errors):
        return "command_type"
    if any("path" in error.get("loc", ()) for error in errors):
        return "path"
    if any("body" in error.get("loc", ()) for error in errors):
        return "payload"
    return "request_validation"


def persist_request_validation_audit(request: Request, command_type: str, entry_source: str, failure_stage: str) -> None:
    request_id = get_request_id(request, request.headers.get("x-request-id"))
    try:
        context = context_for(
            entry_source,
            request,
            request_id,
            request.cookies.get("fj_write_guard"),
            request.headers.get("x-write-guard-verified"),
        )
    except Exception:
        context = ExecutionContext(
            entry_source=entry_source,
            request_id=request_id,
            principal=PrincipalRef(),
            write_guard=WriteGuardState(mode="unknown", verified=False),
            remote_addr=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
    resource_type, resource_id = audit_resource(dict(request.path_params))
    with SessionLocal() as audit_anchor:
        SqlAlchemyReviewRepository(audit_anchor, get_settings()).persist_failed_operation_log(
            context,
            COMMAND_CAPABILITIES.get(command_type),
            "VALIDATION_ERROR",
            command_type=command_type,
            idempotency_key=request.headers.get("idempotency-key"),
            resource_type=resource_type,
            resource_id=resource_id,
            failure_stage=failure_stage,
        )


class AuditedCommandRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        route_handler = super().get_route_handler()
        command_type = COMMAND_ROUTE_TYPES.get(self.endpoint.__name__)
        entry_source = "review" if self.endpoint.__name__.startswith("review_") else "edit"

        async def audited_route_handler(request: Request) -> Response:
            try:
                return await route_handler(request)
            except RequestValidationError as exc:
                if command_type is not None:
                    try:
                        persist_request_validation_audit(request, command_type, entry_source, request_validation_stage(exc))
                    except Exception as audit_exc:
                        LOGGER.warning(
                            "request_validation_audit_write_failed",
                            extra={"exception_type": type(audit_exc).__name__},
                        )
                raise

        return audited_route_handler


router = APIRouter(prefix="/api/v1/final-cut-review", route_class=AuditedCommandRoute)
LOCK_REQUIRED_COMMANDS = {
    "UpdateProject",
    "ArchiveProject",
    "RestoreProject",
    "SoftDeleteProject",
    "UpdateReviewItem",
    "DeleteReviewItem",
    "UploadReviewVersion",
    "StartReview",
    "CreateReviewIssue",
    "UpdateReviewIssue",
    "ResolveReviewIssue",
    "ReopenReviewIssue",
    "SoftDeleteReviewIssue",
    "RequestChanges",
    "FinalizeVersion",
}


class SharedCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=SHARED_CODE_MAX_LENGTH)


def parse_if_match(if_match: str | None) -> int | None:
    if not if_match:
        return None
    value = if_match.strip().strip('"')
    try:
        return int(value)
    except ValueError as exc:
        raise ReviewError("VALIDATION_ERROR", "If-Match 必须是整数 lock_version") from exc


def expected_lock_for(command_type: str, envelope: CommandEnvelope, if_match: str | None) -> int | None:
    expected = parse_if_match(if_match)
    if command_type in LOCK_REQUIRED_COMMANDS and expected is None:
        raise ReviewError("VALIDATION_ERROR", "该命令必须提供 If-Match")
    return expected


def normalize_idempotency_key(idempotency_key: str | None) -> str | None:
    if idempotency_key is None:
        raise ReviewError("VALIDATION_ERROR", "该命令必须提供 Idempotency-Key")
    if len(idempotency_key) < 1 or len(idempotency_key) > 256:
        raise ReviewError("VALIDATION_ERROR", "Idempotency-Key 长度必须在 1 到 256 之间")
    return idempotency_key


def rollback_command(repo: SqlAlchemyReviewRepository) -> None:
    repo.discard_post_commit_file_deletions()
    repo.session.rollback()


def audit_resource(path_payload: dict[str, Any]) -> tuple[str, str | None]:
    for key, resource_type in (
        ("issue_id", "review_issue"),
        ("version_id", "review_version"),
        ("review_item_id", "review_item"),
        ("project_ref_id", "project"),
    ):
        value = path_payload.get(key)
        if value is not None:
            return resource_type, str(value)
    return "request", None


def context_for(entry_source: str, request: Request, request_id: str, fj_write_guard: str | None, x_write_guard_verified: str | None) -> ExecutionContext:
    return build_context(entry_source, request, request_id, fj_write_guard, x_write_guard_verified)


def run_command(
    *,
    fixed_command_type: str,
    envelope: CommandEnvelope,
    path_payload: dict[str, Any],
    context: ExecutionContext,
    bus: CommandBus,
    repo: SqlAlchemyReviewRepository,
    request_id: str,
    idempotency_key: str | None,
    if_match: str | None,
    fastapi_response: Response | None = None,
    success_status: int = 200,
) -> dict[str, Any]:
    capability = COMMAND_CAPABILITIES.get(fixed_command_type)
    resource_type, resource_id = audit_resource(path_payload)
    audit_stage = "command_type"
    normalized_idempotency_key = idempotency_key
    commit_attempted = False
    try:
        if envelope.command_type != fixed_command_type:
            raise ReviewError("VALIDATION_ERROR", "command_type 与路由不匹配")
        audit_stage = "idempotency_key"
        if fixed_command_type in IDEMPOTENT_COMMANDS:
            normalized_idempotency_key = normalize_idempotency_key(idempotency_key)
            if normalized_idempotency_key != envelope.command_id:
                raise ReviewError("VALIDATION_ERROR", "Idempotency-Key 必须与 command_id 一致")
        else:
            normalized_idempotency_key = None
        payload = envelope.payload
        audit_stage = "payload"
        if any(value is None for value in payload.values()):
            raise ReviewError("VALIDATION_ERROR", "payload 字段不接受 null，请省略可选字段")
        audit_stage = "path_payload"
        for key, value in path_payload.items():
            if payload.get(key) != value:
                raise ReviewError("VALIDATION_ERROR", f"payload.{key} 必须与路径一致")
        audit_stage = "lock"
        expected_lock = expected_lock_for(fixed_command_type, envelope, if_match)
        audit_stage = "execution"
        result = bus.execute(
            fixed_command_type,
            payload,
            context,
            expected_lock,
            normalized_idempotency_key or envelope.command_id,
        )
        audit_stage = "operation_log"
        repo.add_operation_log(
            context,
            capability,
            "ok",
            command_type=fixed_command_type,
            idempotency_key=normalized_idempotency_key,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        audit_stage = "commit"
        commit_attempted = True
        repo.session.commit()
        audit_stage = "post_commit"
        repo.run_post_commit_file_deletions()
        if fastapi_response is not None:
            fastapi_response.status_code = success_status
        return ok(result, request_id)
    except StaleDataError as exc:
        rollback_command(repo)
        error = ReviewError("OPTIMISTIC_LOCK_CONFLICT", "乐观锁冲突")
        repo.persist_failed_operation_log(
            context,
            capability,
            error.code,
            command_type=fixed_command_type,
            idempotency_key=normalized_idempotency_key,
            resource_type=resource_type,
            resource_id=resource_id,
            failure_stage=audit_stage,
        )
        raise error from exc
    except IntegrityError as exc:
        rollback_command(repo)
        error = ReviewError("RESOURCE_STATE_CONFLICT", "数据库约束拒绝该操作")
        repo.persist_failed_operation_log(
            context,
            capability,
            error.code,
            command_type=fixed_command_type,
            idempotency_key=normalized_idempotency_key,
            resource_type=resource_type,
            resource_id=resource_id,
            failure_stage=audit_stage,
        )
        raise error from exc
    except RuntimeWriterFenceUnavailable as exc:
        repo.preserve_post_commit_file_deletions()
        repo.session.rollback()
        raise ReviewError("STORAGE_UNAVAILABLE", "数据库写入租约不可用") from exc
    except Exception as exc:
        if commit_attempted:
            repo.preserve_post_commit_file_deletions()
            repo.session.rollback()
            repo.persist_uncertain_operation_log(
                context,
                capability,
                command_type=fixed_command_type,
                idempotency_key=normalized_idempotency_key,
                resource_type=resource_type,
                resource_id=resource_id,
                failure_stage=audit_stage,
            )
        else:
            rollback_command(repo)
            error_code = exc.code if isinstance(exc, ReviewError) else "INTERNAL_SERVER_ERROR"
            repo.persist_failed_operation_log(
                context,
                capability,
                error_code,
                command_type=fixed_command_type,
                idempotency_key=normalized_idempotency_key,
                resource_type=resource_type,
                resource_id=resource_id,
                failure_stage=audit_stage,
            )
        raise


@router.post(
    "/write-guard/session",
    responses={
        413: {
            "description": "Write guard request body exceeds the bounded verification payload.",
            "model": ErrorEnvelope,
        }
    },
)
def verify_write_guard(
    payload: SharedCodeRequest,
    request: Request,
    response: Response,
    request_id: str = Depends(get_request_id),
) -> dict[str, Any]:
    settings = get_settings()
    signer = WriteGuardSessionSigner(settings)
    adapter = ConfiguredWriteGuardAdapter(settings, signer)
    fingerprint = write_guard_attempt_key(request)
    token = adapter.verify_shared_code(payload.code, fingerprint)
    secure_cookie = trusted_request_scheme(request, settings) == "https"
    response.set_cookie(
        "fj_write_guard",
        token,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
        max_age=settings.write_guard_session_ttl_seconds,
    )
    return ok({"verified": True, "mode": "shared_code"}, request_id)


def _deps(
    request: Request,
    session: Session = Depends(session_dependency),
    request_id: str = Depends(get_request_id),
    fj_write_guard: str | None = Cookie(default=None),
    x_write_guard_verified: str | None = Header(default=None),
) -> tuple[Request, Session, str, str | None, str | None]:
    return request, session, request_id, fj_write_guard, x_write_guard_verified


def _execute(
    entry_source: str,
    command_type: str,
    envelope: CommandEnvelope,
    path_payload: dict[str, Any],
    deps: tuple[Request, Session, str, str | None, str | None],
    idempotency_key: str | None,
    if_match: str | None,
    response: Response | None = None,
    success_status: int = 200,
) -> dict[str, Any]:
    request, session, request_id, cookie, proxy_header = deps
    context = context_for(entry_source, request, request_id, cookie, proxy_header)
    bus = get_command_bus(session)
    repo = get_repository(session)
    return run_command(
        fixed_command_type=command_type,
        envelope=envelope,
        path_payload=path_payload,
        context=context,
        bus=bus,
        repo=repo,
        request_id=request_id,
        idempotency_key=idempotency_key,
        if_match=if_match,
        fastapi_response=response,
        success_status=success_status,
    )


@router.post("/edit/projects")
def edit_create_project(response: Response, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    return _execute("edit", "CreateProject", envelope, {}, deps, idempotency_key, None, response=response, success_status=201)


@router.patch("/edit/projects/{project_ref_id}")
def edit_update_project(project_ref_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("edit", "UpdateProject", envelope, {"project_ref_id": project_ref_id}, deps, None, if_match)


@router.post("/review/projects/{project_ref_id}/archive")
def review_archive_project(project_ref_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "ArchiveProject", envelope, {"project_ref_id": project_ref_id}, deps, None, if_match)


@router.post("/review/projects/{project_ref_id}/restore")
def review_restore_project(project_ref_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "RestoreProject", envelope, {"project_ref_id": project_ref_id}, deps, None, if_match)


@router.post("/review/projects/{project_ref_id}/soft-delete")
def review_soft_delete_project(project_ref_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "SoftDeleteProject", envelope, {"project_ref_id": project_ref_id}, deps, None, if_match)


@router.post("/edit/projects/{project_ref_id}/items")
def edit_create_item(project_ref_id: str, response: Response, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    return _execute("edit", "CreateReviewItem", envelope, {"project_ref_id": project_ref_id}, deps, idempotency_key, None, response=response, success_status=201)


@router.patch("/edit/projects/{project_ref_id}/items/{review_item_id}")
def edit_update_item(project_ref_id: str, review_item_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("edit", "UpdateReviewItem", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id}, deps, None, if_match)


@router.post("/edit/projects/{project_ref_id}/items/{review_item_id}/delete")
def edit_delete_item(project_ref_id: str, review_item_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("edit", "DeleteReviewItem", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id}, deps, None, if_match)


@router.post("/edit/projects/{project_ref_id}/items/{review_item_id}/versions")
def edit_upload_version(project_ref_id: str, review_item_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("edit", "UploadReviewVersion", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id}, deps, idempotency_key, if_match)


@router.post("/review/projects/{project_ref_id}/items/{review_item_id}/start")
def review_start(project_ref_id: str, review_item_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "StartReview", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id}, deps, None, if_match)


@router.post("/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues")
def review_create_issue(project_ref_id: str, review_item_id: str, version_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "CreateReviewIssue", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id, "version_id": version_id}, deps, idempotency_key, if_match)


@router.patch("/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}")
def review_update_issue(project_ref_id: str, review_item_id: str, version_id: str, issue_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "UpdateReviewIssue", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id, "version_id": version_id, "issue_id": issue_id}, deps, None, if_match)


@router.post("/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/messages")
def review_add_message(project_ref_id: str, review_item_id: str, version_id: str, issue_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> dict[str, Any]:
    return _execute("review", "AddReviewMessage", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id, "version_id": version_id, "issue_id": issue_id}, deps, idempotency_key, None)


@router.post("/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/resolve")
def review_resolve_issue(project_ref_id: str, review_item_id: str, version_id: str, issue_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "ResolveReviewIssue", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id, "version_id": version_id, "issue_id": issue_id}, deps, None, if_match)


@router.post("/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/reopen")
def review_reopen_issue(project_ref_id: str, review_item_id: str, version_id: str, issue_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "ReopenReviewIssue", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id, "version_id": version_id, "issue_id": issue_id}, deps, None, if_match)


@router.post("/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/issues/{issue_id}/soft-delete")
def review_soft_delete_issue(project_ref_id: str, review_item_id: str, version_id: str, issue_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "SoftDeleteReviewIssue", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id, "version_id": version_id, "issue_id": issue_id}, deps, None, if_match)


@router.post("/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/request-changes")
def review_request_changes(project_ref_id: str, review_item_id: str, version_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "RequestChanges", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id, "version_id": version_id}, deps, idempotency_key, if_match)


@router.post("/review/projects/{project_ref_id}/items/{review_item_id}/versions/{version_id}/finalize")
def review_finalize(project_ref_id: str, review_item_id: str, version_id: str, envelope: CommandEnvelope, deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"), if_match: str = Header(..., alias="If-Match", pattern='^"?[0-9]+"?$')) -> dict[str, Any]:
    return _execute("review", "FinalizeVersion", envelope, {"project_ref_id": project_ref_id, "review_item_id": review_item_id, "version_id": version_id}, deps, idempotency_key, if_match)


@router.post("/review/projects/{project_ref_id}/finalized-originals/packages")
def review_prepare_package(
    project_ref_id: str,
    response: Response,
    envelope: CommandEnvelope,
    deps: tuple[Request, Session, str, str | None, str | None] = Depends(_deps),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    result = _execute(
        "review",
        "PrepareFinalizedPackage",
        envelope,
        {"project_ref_id": project_ref_id},
        deps,
        idempotency_key,
        None,
        response=response,
        success_status=202,
    )
    return result
