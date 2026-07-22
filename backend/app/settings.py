from __future__ import annotations

import os
import stat
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

MIN_SIGNING_SECRET_LENGTH = 32
FORBIDDEN_SIGNING_SECRETS = {
    "change-me-in-deploy",
    "change-this-secret",
    "change-this",
    "changeme",
    "secret",
    "password",
}
UPLOAD_SESSION_TTL_SAFETY_MARGIN_SECONDS = 60
DIRECTORY_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
SECRET_FILE_MAX_BYTES = 64 * 1024


def _read_secret_file(setting_name: str, file_path: str) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(file_path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > SECRET_FILE_MAX_BYTES:
            raise RuntimeError(f"{setting_name}_FILE must reference a non-empty bounded regular file")
        with os.fdopen(descriptor, "rb") as secret_file:
            descriptor = -1
            raw_value = secret_file.read(SECRET_FILE_MAX_BYTES + 1)
    except OSError as exc:
        raise RuntimeError(f"{setting_name}_FILE must reference a readable non-symlink regular file") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if len(raw_value) > SECRET_FILE_MAX_BYTES:
        raise RuntimeError(f"{setting_name}_FILE exceeds the maximum supported size")
    if raw_value.endswith(b"\n"):
        raw_value = raw_value[:-1]
        if raw_value.endswith(b"\r"):
            raw_value = raw_value[:-1]
    if not raw_value or b"\x00" in raw_value or b"\r" in raw_value or b"\n" in raw_value:
        raise RuntimeError(f"{setting_name}_FILE must contain exactly one non-empty secret value")
    try:
        return raw_value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"{setting_name}_FILE must contain valid UTF-8") from exc


def _resolve_secret_setting(setting_name: str, direct_value: str, file_path: str) -> str:
    if direct_value and file_path:
        raise RuntimeError(f"{setting_name} and {setting_name}_FILE are mutually exclusive")
    if file_path:
        return _read_secret_file(setting_name, file_path)
    return direct_value


def _open_data_root(absolute: Path, *, create: bool) -> tuple[int, tuple[tuple[int, int], ...]]:
    current_fd = os.open("/", DIRECTORY_FLAGS)
    identities: list[tuple[int, int]] = []
    try:
        for part in absolute.parts[1:]:
            try:
                next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=current_fd)
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(next_fd)
                raise NotADirectoryError(part)
            identities.append((metadata.st_dev, metadata.st_ino))
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, tuple(identities)
    except Exception:
        os.close(current_fd)
        raise


def _prepare_data_root(path: Path, setting_name: str) -> Path:
    absolute = Path(os.path.abspath(os.path.normpath(os.fspath(path.expanduser()))))
    first_fd = -1
    second_fd = -1
    try:
        first_fd, first_identities = _open_data_root(absolute, create=True)
        second_fd, second_identities = _open_data_root(absolute, create=False)
        if first_identities != second_identities:
            raise RuntimeError(f"{setting_name} changed while it was inspected")

        opened = os.fstat(second_fd)
        if not stat.S_ISDIR(opened.st_mode):
            raise RuntimeError(f"{setting_name} must be a directory")
        return absolute
    except OSError as exc:
        raise RuntimeError(f"{setting_name} must not contain symbolic links and must be accessible") from exc
    finally:
        if first_fd >= 0:
            os.close(first_fd)
        if second_fd >= 0:
            os.close(second_fd)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "fj-final-cut-review-backend"
    database_url: str = ""
    database_url_file: str = ""
    database_host: str = ""
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = ""
    database_user: str = ""
    database_password: SecretStr | None = None
    database_password_file: str = ""
    database_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    database_statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)
    database_migration_statement_timeout_ms: int = Field(default=300_000, ge=10_000, le=3_600_000)
    allow_sqlite_for_tests: bool = False
    storage_root: Path = Path("backend/.data/storage")
    package_root: Path = Path("backend/.data/packages")
    max_upload_bytes: int = 5 * 1024 * 1024 * 1024
    max_upload_part_bytes: int = 64 * 1024 * 1024
    max_upload_parts: int = Field(default=256, ge=1, le=256)
    upload_part_read_timeout_seconds: float = Field(default=120.0, gt=0, le=600)
    upload_part_io_workers: int = Field(default=4, ge=1, le=32)
    max_inflight_upload_parts_per_principal: int = Field(default=16, ge=1, le=64)
    max_inflight_upload_parts_per_session: int = Field(default=1, ge=1, le=1)
    max_inflight_upload_part_candidates: int = Field(default=64, ge=1, le=1024)
    max_active_upload_sessions_global: int = Field(default=128, ge=1, le=4096)
    max_active_upload_sessions_per_principal: int = Field(default=16, ge=1, le=256)
    max_reserved_upload_bytes_global: int = Field(default=100 * 1024 * 1024 * 1024, ge=1)
    max_reserved_upload_bytes_per_principal: int = Field(default=20 * 1024 * 1024 * 1024, ge=1)
    upload_storage_low_watermark_bytes: int = Field(default=1024 * 1024 * 1024, ge=0)
    upload_finalization_lease_seconds: int = Field(default=7200, ge=60, le=86400)
    upload_runtime_worker_count: int = Field(default=1, ge=1, le=1)
    upload_session_ttl_seconds: int = Field(default=15 * 60, ge=300, le=7 * 24 * 60 * 60)
    media_probe_command: str = Field(default="ffprobe", min_length=1, max_length=4096)
    media_probe_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    package_ttl_seconds: int = Field(default=24 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)
    max_package_files: int = Field(default=100, ge=1, le=100)
    max_package_bytes: int = Field(default=5 * 1024 * 1024 * 1024, ge=1, le=5 * 1024 * 1024 * 1024)
    max_pending_package_builds: int = Field(default=8, ge=1, le=16)
    max_package_storage_bytes: int = Field(default=20 * 1024 * 1024 * 1024, ge=1, le=100 * 1024 * 1024 * 1024)
    package_worker_max_attempts: int = Field(default=3, ge=1, le=10)
    package_worker_retry_delay_seconds: int = Field(default=30, ge=1, le=3600)
    package_download_token_ttl_seconds: int = Field(default=600, ge=60, le=600)
    package_download_session_ttl_seconds: int = Field(default=120, ge=30, le=300)
    package_download_lease_seconds: int = Field(default=7200, ge=300, le=86400)
    package_download_cooldown_seconds: int = Field(default=10, ge=1, le=300)
    write_guard_mode: str = Field(default="none", pattern="^(none|shared_code|reverse_proxy)$")
    write_guard_code: str | None = Field(default=None, max_length=256)
    write_guard_code_file: str = ""
    write_guard_session_secret: str = ""
    write_guard_session_secret_file: str = ""
    write_guard_session_ttl_seconds: int = 14400
    write_guard_max_failures: int = 5
    write_guard_failure_window_seconds: int = 300
    write_guard_lockout_seconds: int = 900
    reverse_proxy_write_header: str = "x-write-guard-verified"
    reverse_proxy_trusted_hosts: str = ""
    browser_allowed_origins: str = "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:5174,http://localhost:5174,http://127.0.0.1:8000,http://localhost:8000"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_upload_session_ttl(self) -> Self:
        minimum_ttl = self.upload_part_read_timeout_seconds + UPLOAD_SESSION_TTL_SAFETY_MARGIN_SECONDS
        if self.upload_session_ttl_seconds <= minimum_ttl:
            raise ValueError(
                "UPLOAD_SESSION_TTL_SECONDS must exceed UPLOAD_PART_READ_TIMEOUT_SECONDS plus the safety margin"
            )
        return self


def resolved_database_url(settings: Settings) -> str:
    database_url = _resolve_secret_setting("DATABASE_URL", settings.database_url, settings.database_url_file)
    if database_url:
        return database_url
    direct_password = settings.database_password.get_secret_value() if settings.database_password else ""
    password = _resolve_secret_setting("DATABASE_PASSWORD", direct_password, settings.database_password_file)
    if not all((settings.database_host, settings.database_name, settings.database_user, password)):
        raise RuntimeError(
            "DATABASE_URL is required unless complete DATABASE_HOST, DATABASE_NAME, DATABASE_USER, and DATABASE_PASSWORD components are configured"
        )
    return URL.create(
        "postgresql+psycopg",
        username=settings.database_user,
        password=password,
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
    ).render_as_string(hide_password=False)


def get_database_settings() -> Settings:
    settings = Settings()
    settings.database_url = resolved_database_url(settings)
    drivername = make_url(settings.database_url).drivername
    if drivername.startswith("sqlite") and not settings.allow_sqlite_for_tests:
        raise RuntimeError("SQLite is only allowed when ALLOW_SQLITE_FOR_TESTS=true is explicitly set")
    if not drivername.startswith(("postgresql", "sqlite")):
        raise RuntimeError("Delivery runtimes must use PostgreSQL; SQLite is only allowed for explicit tests")
    settings.storage_root = _prepare_data_root(settings.storage_root, "STORAGE_ROOT")
    settings.package_root = _prepare_data_root(settings.package_root, "PACKAGE_ROOT")
    if drivername.startswith("sqlite") and settings.database_url.startswith("sqlite:///"):
        db_path = Path(settings.database_url.removeprefix("sqlite:///"))
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = get_database_settings()
    settings.write_guard_code = _resolve_secret_setting(
        "WRITE_GUARD_CODE",
        settings.write_guard_code or "",
        settings.write_guard_code_file,
    )
    if settings.write_guard_code and len(settings.write_guard_code) > 256:
        raise RuntimeError("WRITE_GUARD_CODE exceeds the maximum supported length")
    settings.write_guard_session_secret = _resolve_secret_setting(
        "WRITE_GUARD_SESSION_SECRET",
        settings.write_guard_session_secret,
        settings.write_guard_session_secret_file,
    )
    if not settings.write_guard_session_secret:
        raise RuntimeError("WRITE_GUARD_SESSION_SECRET is required")
    if (
        settings.write_guard_session_secret in FORBIDDEN_SIGNING_SECRETS
        or len(settings.write_guard_session_secret) < MIN_SIGNING_SECRET_LENGTH
    ):
        raise RuntimeError("WRITE_GUARD_SESSION_SECRET must be a non-placeholder signing secret")
    if settings.write_guard_mode == "shared_code" and not settings.write_guard_code:
        raise RuntimeError("WRITE_GUARD_CODE is required when WRITE_GUARD_MODE=shared_code")
    return settings
