from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from secrets import compare_digest
from threading import Lock

from itsdangerous import BadSignature, URLSafeTimedSerializer

from backend.app.modules.final_cut_review.application.context import ExecutionContext, PrincipalRef
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.review_contracts.generated import EDIT_ENTRY_PROFILE, REVIEW_ENTRY_PROFILE
from backend.app.settings import Settings

SHARED_CODE_MAX_LENGTH = 256
SHARED_CODE_REQUEST_MAX_BYTES = 4096


class StaticEntryPolicyAdapter:
    def __init__(self) -> None:
        self._profiles = {
            "edit": set(EDIT_ENTRY_PROFILE),
            "review": set(REVIEW_ENTRY_PROFILE),
            "embedded": set(),
            "unspecified": set(EDIT_ENTRY_PROFILE) | set(REVIEW_ENTRY_PROFILE),
        }

    def allows(self, entry_source: str, capability: str) -> bool:
        return capability in self._profiles.get(entry_source, set())


class NoAccountAuthorizationAdapter:
    def authorize(self, context: ExecutionContext, capability: str, resource: dict[str, str | None]) -> None:
        del capability
        if not context.principal.id:
            raise ReviewError("PRINCIPAL_AUTHENTICATION_REQUIRED", "缺少可信 principal 上下文")
        project_ref_id = resource.get("project_ref_id")
        if project_ref_id and not context.principal.can_access_project(project_ref_id):
            raise ReviewError("PRINCIPAL_PERMISSION_DENIED", "当前 principal 无权访问该项目")


class PrincipalContextSigner:
    salt = "fj-final-cut-review-principal-context"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._serializer = URLSafeTimedSerializer(settings.write_guard_session_secret, salt=self.salt)

    def issue(self, principal_id: str, project_ref_ids: tuple[str, ...], principal_kind: str = "user") -> str:
        return self._serializer.dumps({"kind": principal_kind, "id": principal_id, "project_ref_ids": list(project_ref_ids)})

    def verify(self, token: str | None) -> PrincipalRef:
        if not token:
            return PrincipalRef()
        try:
            data = self._serializer.loads(token, max_age=self._settings.write_guard_session_ttl_seconds)
        except BadSignature:
            return PrincipalRef()
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            return PrincipalRef()
        project_ref_ids = data.get("project_ref_ids", [])
        if not isinstance(project_ref_ids, list) or not all(isinstance(item, str) for item in project_ref_ids):
            return PrincipalRef()
        kind_value = data.get("kind")
        kind = kind_value if isinstance(kind_value, str) else "user"
        return PrincipalRef(kind=kind, id=data["id"], project_ref_ids=tuple(project_ref_ids))


class WriteGuardSessionSigner:
    salt = "fj-final-cut-review-write-guard"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._serializer = URLSafeTimedSerializer(settings.write_guard_session_secret, salt=self.salt)

    def issue(self) -> str:
        return self._serializer.dumps({"ok": True})

    def verify(self, token: str | None) -> bool:
        if not isinstance(token, str) or not token:
            return False
        try:
            data = self._serializer.loads(token, max_age=self._settings.write_guard_session_ttl_seconds)
        except BadSignature:
            return False
        return data == {"ok": True}


class PackageDownloadTokenSigner:
    salt = "fj-final-cut-review-package-download"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._serializer = URLSafeTimedSerializer(settings.write_guard_session_secret, salt=self.salt)

    def issue(self, project_ref_id: str, package_id: str) -> str:
        return self._serializer.dumps({"project_ref_id": project_ref_id, "package_id": package_id})

    def verify(self, token: str | None, project_ref_id: str, package_id: str) -> bool:
        if not token:
            return False
        try:
            data = self._serializer.loads(token, max_age=self._settings.package_download_token_ttl_seconds)
        except BadSignature:
            return False
        return data == {"project_ref_id": project_ref_id, "package_id": package_id}


@dataclass
class _SharedCodeAttempt:
    failures: int = 0
    first_failed_at: datetime | None = None
    locked_until: datetime | None = None


class ConfiguredWriteGuardAdapter:
    _attempts: dict[str, _SharedCodeAttempt] = {}
    _attempts_lock = Lock()
    _max_attempt_entries = 4096

    def __init__(self, settings: Settings, signer: WriteGuardSessionSigner) -> None:
        self._settings = settings
        self._signer = signer

    def assert_write_allowed(self, context: ExecutionContext) -> None:
        if self._settings.write_guard_mode == "none":
            return
        if context.write_guard.verified:
            return
        raise ReviewError("WRITE_GUARD_REQUIRED", "需要写保护验证")

    @classmethod
    def reset_attempts_for_tests(cls) -> None:
        with cls._attempts_lock:
            cls._attempts.clear()

    def _prune_expired_attempts(self, now: datetime) -> None:
        failure_window = timedelta(seconds=self._settings.write_guard_failure_window_seconds)
        expired_keys = [
            key
            for key, attempt in self._attempts.items()
            if (
                attempt.locked_until is not None
                and attempt.locked_until <= now
            )
            or (
                attempt.locked_until is None
                and attempt.first_failed_at is not None
                and now - attempt.first_failed_at > failure_window
            )
        ]
        for key in expired_keys:
            self._attempts.pop(key, None)

    def verify_shared_code(self, submitted_code: str, attempt_key: str) -> str:
        if self._settings.write_guard_mode != "shared_code":
            raise ReviewError("PORT_OPERATION_NOT_SUPPORTED", "当前未启用 shared_code 写保护")
        if not submitted_code or len(submitted_code) > SHARED_CODE_MAX_LENGTH:
            raise ReviewError("WRITE_GUARD_INVALID", "写保护验证失败")
        now = datetime.now(timezone.utc)
        with self._attempts_lock:
            self._prune_expired_attempts(now)
            attempt = self._attempts.get(attempt_key)
            if attempt and attempt.locked_until and attempt.locked_until > now:
                raise ReviewError("WRITE_GUARD_INVALID", "写保护验证失败次数过多，请稍后重试")
            configured_code = self._settings.write_guard_code
            if configured_code and compare_digest(
                submitted_code.encode("utf-8"),
                configured_code.encode("utf-8"),
            ):
                self._attempts.pop(attempt_key, None)
                return self._signer.issue()
            if attempt is None:
                if len(self._attempts) >= self._max_attempt_entries:
                    raise ReviewError("WRITE_GUARD_INVALID", "写保护验证失败次数过多，请稍后重试")
                attempt = _SharedCodeAttempt()
                self._attempts[attempt_key] = attempt
            if attempt.failures == 0:
                attempt.first_failed_at = now
            attempt.failures += 1
            if attempt.failures >= self._settings.write_guard_max_failures:
                attempt.locked_until = now + timedelta(seconds=self._settings.write_guard_lockout_seconds)
            raise ReviewError("WRITE_GUARD_INVALID", "写保护验证失败")


__all__ = [
    "ConfiguredWriteGuardAdapter",
    "NoAccountAuthorizationAdapter",
    "PackageDownloadTokenSigner",
    "PrincipalContextSigner",
    "StaticEntryPolicyAdapter",
    "WriteGuardSessionSigner",
]
