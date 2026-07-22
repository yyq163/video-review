from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
from alembic import command as alembic_command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, create_engine, delete, inspect, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from backend.app.modules.final_cut_review.infra.database import Base, SessionLocal
from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
    IdempotencyRecordModel,
    FinalizationRecordModel,
    OutboxEventModel,
    ProjectRefModel,
    ReviewAnnotationSetModel,
    ReviewIssueModel,
    ReviewIssueRevisionModel,
    ReviewItemModel,
    ReviewVersionModel,
    UploadSessionModel,
)

from .conftest import TEST_SIGNING_SECRET, api_data, api_error, command, create_project, create_project_item, upload_init_request, upload_video
from .test_api_contract_runtime import annotation, create_issue, finalize


def test_settings_require_database_url_and_explicit_sqlite_test_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    settings_mod = __import__("backend.app.settings", fromlist=["get_settings"])

    monkeypatch.delenv("DATABASE_URL", raising=False)
    for component in ("DATABASE_HOST", "DATABASE_NAME", "DATABASE_USER", "DATABASE_PASSWORD"):
        monkeypatch.delenv(component, raising=False)
    monkeypatch.delenv("ALLOW_SQLITE_FOR_TESTS", raising=False)
    settings_mod.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="DATABASE_URL is required"):
        settings_mod.get_settings()

    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/fj-disallowed.db")
    settings_mod.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SQLite is only allowed"):
        settings_mod.get_settings()

    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://user:pass@127.0.0.1/db")
    settings_mod.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="Delivery runtimes must use PostgreSQL"):
        settings_mod.get_settings()

    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///tmp/fj-allowed.db")
    settings_mod.get_settings.cache_clear()
    try:
        assert settings_mod.get_settings().database_url.startswith("sqlite")
    finally:
        settings_mod.get_settings.cache_clear()


def test_upload_reservation_covers_parts_and_full_staging_peak(client: TestClient) -> None:
    response = upload_init_request(
        client,
        json={
            "original_filename": "quota-peak.mp4",
            "mime_type": "video/mp4",
            "file_size": 123,
            "sha256": "a" * 64,
        },
    )
    upload_id = api_data(response)["upload_id"]

    with SessionLocal() as session:
        upload = session.get(UploadSessionModel, upload_id)
        assert upload is not None
        assert upload.declared_size == 123
        assert upload.reserved_bytes == 246
        upload.reserved_bytes = upload.declared_size
        with pytest.raises((DBAPIError, IntegrityError)):
            session.commit()


def test_upload_session_ttl_exceeds_body_timeout_with_safety_margin() -> None:
    from backend.app.settings import Settings, UPLOAD_SESSION_TTL_SAFETY_MARGIN_SECONDS

    defaults = Settings()
    assert defaults.upload_session_ttl_seconds == 15 * 60
    assert defaults.max_inflight_upload_parts_per_principal == 16
    assert defaults.max_active_upload_sessions_per_principal == 16
    assert (
        defaults.upload_session_ttl_seconds
        > defaults.upload_part_read_timeout_seconds + UPLOAD_SESSION_TTL_SAFETY_MARGIN_SECONDS
    )
    with pytest.raises(ValueError, match="UPLOAD_SESSION_TTL_SECONDS must exceed"):
        Settings(
            upload_part_read_timeout_seconds=240,
            upload_session_ttl_seconds=300,
        )


def test_upload_session_admission_cannot_be_relaxed_by_environment() -> None:
    from backend.app.settings import Settings

    assert Settings().max_inflight_upload_parts_per_session == 1
    with pytest.raises(ValidationError, match="max_inflight_upload_parts_per_session"):
        Settings(max_inflight_upload_parts_per_session=2)


def test_runtime_replacement_of_managed_root_with_symlink_fails_closed(tmp_path: Path) -> None:
    from backend.app.safe_files import UnsafeFilePathError, pin_regular_file, write_private_file

    managed_root = tmp_path / "managed"
    managed_root.mkdir()
    original_root = tmp_path / "managed-original"
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    managed_root.rename(original_root)
    managed_root.symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(UnsafeFilePathError):
        write_private_file(managed_root, "files", "file_test", b"blocked")
    with pytest.raises(UnsafeFilePathError):
        with pin_regular_file(managed_root / "outside.txt", managed_root):
            pass

    assert list(outside_root.iterdir()) == []


def test_database_components_safely_encode_uri_reserved_password_characters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_mod = __import__("backend.app.settings", fromlist=["get_database_settings"])
    synthetic_password = "local-test:@/?#[]% value"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_HOST", "postgres")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "final_cut_review")
    monkeypatch.setenv("DATABASE_USER", "final_cut_review_app")
    monkeypatch.setenv("DATABASE_PASSWORD", synthetic_password)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))

    settings = settings_mod.get_database_settings()
    parsed = make_url(settings.database_url)

    assert parsed.drivername == "postgresql+psycopg"
    assert parsed.username == "final_cut_review_app"
    assert parsed.password == synthetic_password
    assert parsed.host == "postgres"
    assert parsed.database == "final_cut_review"


def test_application_secrets_load_from_files_and_reject_ambiguous_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_mod = __import__("backend.app.settings", fromlist=["get_settings"])
    database_password = "local-file-test:@/?#[]% value"
    database_password_file = tmp_path / "database-password"
    database_password_file.write_text(f"{database_password}\n", encoding="utf-8")
    signing_secret = "file-backed-signing-secret-at-least-32-bytes"
    signing_secret_file = tmp_path / "write-guard-secret"
    signing_secret_file.write_text(signing_secret, encoding="utf-8")
    write_guard_code = "file-backed-write-guard-code"
    write_guard_code_file = tmp_path / "write-guard-code"
    write_guard_code_file.write_text(write_guard_code, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    for name in (
        "DATABASE_URL",
        "DATABASE_URL_FILE",
        "DATABASE_PASSWORD",
        "WRITE_GUARD_CODE",
        "WRITE_GUARD_SESSION_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DATABASE_HOST", "postgres")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "final_cut_review")
    monkeypatch.setenv("DATABASE_USER", "final_cut_review_app")
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", str(database_password_file))
    monkeypatch.setenv("WRITE_GUARD_MODE", "shared_code")
    monkeypatch.setenv("WRITE_GUARD_CODE_FILE", str(write_guard_code_file))
    monkeypatch.setenv("WRITE_GUARD_SESSION_SECRET_FILE", str(signing_secret_file))
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    settings_mod.get_settings.cache_clear()
    try:
        settings = settings_mod.get_settings()
        assert make_url(settings.database_url).password == database_password
        assert settings.write_guard_code == write_guard_code
        assert settings.write_guard_session_secret == signing_secret

        monkeypatch.setenv("DATABASE_PASSWORD", "ambiguous-direct-value")
        settings_mod.get_settings.cache_clear()
        with pytest.raises(RuntimeError, match="DATABASE_PASSWORD and DATABASE_PASSWORD_FILE are mutually exclusive"):
            settings_mod.get_settings()
    finally:
        settings_mod.get_settings.cache_clear()


def test_database_role_scripts_load_secret_files_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.scripts.bootstrap_database_roles import BootstrapConfig, read_secret_from_environment
    from backend.scripts.grant_runtime_role import RuntimeGrantConfig

    secret_values = {
        "POSTGRES_ADMIN_PASSWORD": "admin-file-secret",
        "POSTGRES_OWNER_PASSWORD": "owner-file-secret",
        "POSTGRES_APP_PASSWORD": "runtime-file-secret",
    }
    for name, value in secret_values.items():
        secret_path = tmp_path / name.lower()
        secret_path.write_text(f"{value}\n", encoding="utf-8")
        monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv(f"{name}_FILE", str(secret_path))
    monkeypatch.setenv("POSTGRES_ADMIN_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_ADMIN_PORT", "5432")
    monkeypatch.setenv("POSTGRES_ADMIN_DB", "postgres")
    monkeypatch.setenv("POSTGRES_ADMIN_USER", "postgres")
    monkeypatch.setenv("POSTGRES_OWNER_USER", "final_cut_review_owner")
    monkeypatch.setenv("POSTGRES_APP_DB", "final_cut_review")
    monkeypatch.setenv("POSTGRES_APP_USER", "final_cut_review_app")
    monkeypatch.setenv("POSTGRES_TEST_DB", "final_cut_review_test")

    bootstrap = BootstrapConfig.from_environment()
    assert bootstrap.admin_password == secret_values["POSTGRES_ADMIN_PASSWORD"]
    assert bootstrap.owner_password == secret_values["POSTGRES_OWNER_PASSWORD"]
    assert bootstrap.runtime_password == secret_values["POSTGRES_APP_PASSWORD"]

    monkeypatch.setenv("DATABASE_HOST", "postgres")
    monkeypatch.setenv("DATABASE_PORT", "5432")
    monkeypatch.setenv("DATABASE_NAME", "final_cut_review")
    monkeypatch.setenv("DATABASE_USER", "final_cut_review_owner")
    monkeypatch.delenv("DATABASE_PASSWORD", raising=False)
    monkeypatch.setenv("DATABASE_PASSWORD_FILE", os.environ["POSTGRES_OWNER_PASSWORD_FILE"])
    grant = RuntimeGrantConfig.from_environment()
    assert grant.owner_password == secret_values["POSTGRES_OWNER_PASSWORD"]

    monkeypatch.setenv("DATABASE_PASSWORD", "ambiguous-direct-value")
    with pytest.raises(ValueError, match="DATABASE_PASSWORD and DATABASE_PASSWORD_FILE are mutually exclusive"):
        RuntimeGrantConfig.from_environment()

    unsafe_target = tmp_path / "unsafe-target"
    unsafe_target.write_text("must-not-be-read", encoding="utf-8")
    unsafe_link = tmp_path / "unsafe-link"
    unsafe_link.symlink_to(unsafe_target)
    monkeypatch.delenv("UNSAFE_TEST_SECRET", raising=False)
    monkeypatch.setenv("UNSAFE_TEST_SECRET_FILE", str(unsafe_link))
    with pytest.raises(ValueError, match="non-symlink regular file"):
        read_secret_from_environment("UNSAFE_TEST_SECRET")


@pytest.mark.parametrize(
    ("content", "error_match"),
    [
        (b"", "non-empty bounded regular file"),
        (b"first-line\nsecond-line\n", "exactly one non-empty secret value"),
        (b"\xff", "valid UTF-8"),
    ],
)
def test_secret_file_readers_reject_unsafe_content(
    content: bytes,
    error_match: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app.settings import Settings, resolved_database_url
    from backend.scripts.bootstrap_database_roles import read_secret_from_environment

    secret_file = tmp_path / "unsafe-secret"
    secret_file.write_bytes(content)
    with pytest.raises(RuntimeError, match=error_match):
        resolved_database_url(Settings(database_url="", database_url_file=str(secret_file)))

    monkeypatch.delenv("UNSAFE_TEST_SECRET", raising=False)
    monkeypatch.setenv("UNSAFE_TEST_SECRET_FILE", str(secret_file))
    with pytest.raises(ValueError, match=error_match):
        read_secret_from_environment("UNSAFE_TEST_SECRET")


def test_database_only_settings_do_not_require_http_signing_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings_mod = __import__("backend.app.settings", fromlist=["get_database_settings"])

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'maintenance.db'}")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    monkeypatch.delenv("WRITE_GUARD_SESSION_SECRET", raising=False)

    settings = settings_mod.get_database_settings()

    assert settings.database_url.startswith("sqlite")
    assert settings.storage_root.is_absolute()
    assert settings.package_root.is_absolute()
    assert settings.storage_root == (tmp_path / "storage").resolve()
    assert settings.package_root == (tmp_path / "packages").resolve()
    assert settings.write_guard_session_secret == ""
    settings_mod.get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="WRITE_GUARD_SESSION_SECRET is required"):
            settings_mod.get_settings()
    finally:
        settings_mod.get_settings.cache_clear()


@pytest.mark.parametrize("setting_name", ["STORAGE_ROOT", "PACKAGE_ROOT"])
def test_database_settings_reject_symlinked_data_roots(
    setting_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_mod = __import__("backend.app.settings", fromlist=["get_database_settings"])
    real_root = tmp_path / f"real-{setting_name.lower()}"
    real_root.mkdir()
    linked_root = tmp_path / f"linked-{setting_name.lower()}"
    linked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'symlink-root.db'}")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    monkeypatch.setenv(setting_name, str(linked_root))

    with pytest.raises(RuntimeError, match=f"{setting_name} must not contain symbolic links"):
        settings_mod.get_database_settings()


@pytest.mark.parametrize("setting_name", ["STORAGE_ROOT", "PACKAGE_ROOT"])
def test_database_settings_reject_intermediate_symlink_without_creating_outside(
    setting_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings_mod = __import__("backend.app.settings", fromlist=["get_database_settings"])
    outside = tmp_path / "outside"
    outside.mkdir()
    safe_parent = tmp_path / "safe"
    safe_parent.mkdir()
    (safe_parent / "linked").symlink_to(outside, target_is_directory=True)
    target = safe_parent / "linked" / "must-not-be-created"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'intermediate-symlink.db'}")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    monkeypatch.setenv(setting_name, str(target))

    with pytest.raises(RuntimeError, match=f"{setting_name} must not contain symbolic links"):
        settings_mod.get_database_settings()

    assert list(outside.iterdir()) == []


def test_runtime_writer_lock_keys_reject_different_roots_on_same_database(tmp_path: Path) -> None:
    from backend.app.modules.final_cut_review.infra.database import runtime_writer_lock_keys
    from backend.app.settings import Settings

    first = Settings(
        database_url="postgresql+psycopg://localhost/final_cut_review",
        storage_root=tmp_path / "first-storage",
        package_root=tmp_path / "first-packages",
    )
    second = Settings(
        database_url="postgresql+psycopg://localhost/final_cut_review",
        storage_root=tmp_path / "second-storage",
        package_root=tmp_path / "second-packages",
    )

    first_writer, first_contract, first_fence = runtime_writer_lock_keys("final_cut_review", first)
    second_writer, second_contract, second_fence = runtime_writer_lock_keys("final_cut_review", second)

    assert first_writer == second_writer
    assert first_contract != second_contract
    assert first_fence == second_fence


def test_runtime_writer_lock_allows_only_explicit_sqlite_test_exemption(tmp_path: Path) -> None:
    from backend.app.modules.final_cut_review.infra.database import acquire_runtime_writer_lock
    from backend.app.settings import Settings

    sqlite_engine = create_engine(f"sqlite:///{tmp_path / 'writer-lock.db'}")
    try:
        disallowed = Settings(
            database_url=f"sqlite:///{tmp_path / 'writer-lock.db'}",
            allow_sqlite_for_tests=False,
        )
        with pytest.raises(RuntimeError, match="ALLOW_SQLITE_FOR_TESTS=true"):
            acquire_runtime_writer_lock(sqlite_engine, disallowed)

        allowed = Settings(
            database_url=f"sqlite:///{tmp_path / 'writer-lock.db'}",
            allow_sqlite_for_tests=True,
        )
        lock = acquire_runtime_writer_lock(sqlite_engine, allowed)
        lock.assert_held()
        lock.release()
        with pytest.raises(RuntimeError, match="already been released"):
            lock.assert_held()
    finally:
        sqlite_engine.dispose()


def test_runtime_participant_allows_only_explicit_sqlite_test_exemption_and_fences_worker_commit(
    tmp_path: Path,
) -> None:
    from backend.app.modules.final_cut_review.infra.database import (
        RUNTIME_PARTICIPANT_REQUIRED_SESSION_KEY,
        RuntimeParticipantLease,
        RuntimeWriterFenceUnavailable,
        acquire_runtime_participant_lease,
        require_runtime_participant_session,
        runtime_participant_lease,
    )
    from backend.app.settings import Settings

    database_path = tmp_path / "participant.db"
    sqlite_engine = create_engine(f"sqlite:///{database_path}")
    session_factory = sessionmaker(bind=sqlite_engine, future=True)
    disallowed = Settings(database_url=f"sqlite:///{database_path}", allow_sqlite_for_tests=False)
    allowed = Settings(database_url=f"sqlite:///{database_path}", allow_sqlite_for_tests=True)
    try:
        with pytest.raises(RuntimeError, match="ALLOW_SQLITE_FOR_TESTS=true"):
            acquire_runtime_participant_lease(sqlite_engine, disallowed)

        with runtime_participant_lease(sqlite_engine, allowed) as participant:
            assert isinstance(participant, RuntimeParticipantLease)
            with runtime_participant_lease(sqlite_engine, allowed) as nested:
                assert nested is participant
            with session_factory() as worker_session:
                require_runtime_participant_session(worker_session)
                worker_session.execute(text("SELECT 1"))
                worker_session.commit()

        with session_factory() as unbound_worker_session:
            unbound_worker_session.info[RUNTIME_PARTICIPANT_REQUIRED_SESSION_KEY] = True
            unbound_worker_session.execute(text("SELECT 1"))
            with pytest.raises(RuntimeWriterFenceUnavailable, match="not bound before commit"):
                unbound_worker_session.commit()
    finally:
        sqlite_engine.dispose()


def test_worker_entries_fail_before_database_or_physical_cleanup_when_participant_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.app import maintenance_cleanup, package_builds
    from backend.app.modules.final_cut_review.infra import database as database_module
    from backend.app.modules.final_cut_review.infra.database import RuntimeWriterFenceUnavailable

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'denied-worker.db'}")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))

    class DeniedParticipant:
        def __enter__(self) -> None:
            raise RuntimeWriterFenceUnavailable("participant denied")

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(database_module, "runtime_participant_lease", lambda *_args: DeniedParticipant())

    def unexpected_database_session() -> None:
        raise AssertionError("worker opened a database session without a participant")

    def unexpected_physical_cleanup(*_args: object) -> Path:
        raise AssertionError("maintenance touched managed storage without a participant")

    monkeypatch.setattr(package_builds, "SessionLocal", unexpected_database_session)
    monkeypatch.setattr(maintenance_cleanup, "ensure_private_directory", unexpected_physical_cleanup)

    with pytest.raises(RuntimeWriterFenceUnavailable, match="participant denied"):
        package_builds.process_pending_packages()
    with pytest.raises(RuntimeWriterFenceUnavailable, match="participant denied"):
        maintenance_cleanup.cleanup_temporary_files()


def test_postgresql_runtime_participant_rejects_missing_active_backend(tmp_path: Path) -> None:
    if os.environ.get("RUN_POSTGRES_CONSTRAINT_TESTS") != "1":
        pytest.skip("PostgreSQL runtime participant gate requires RUN_POSTGRES_CONSTRAINT_TESTS=1")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or not make_url(database_url).drivername.startswith("postgresql"):
        pytest.fail("PostgreSQL DATABASE_URL is required for the runtime participant gate")

    from backend.app.modules.final_cut_review.infra.database import (
        RuntimeWriterFenceUnavailable,
        acquire_runtime_participant_lease,
    )
    from backend.app.settings import Settings

    postgres_engine = create_engine(database_url, pool_pre_ping=True)
    settings = Settings(
        database_url=database_url,
        storage_root=tmp_path / "missing-backend-storage",
        package_root=tmp_path / "missing-backend-packages",
    )
    try:
        with pytest.raises(RuntimeWriterFenceUnavailable, match="no active backend runtime owns"):
            acquire_runtime_participant_lease(postgres_engine, settings)
    finally:
        postgres_engine.dispose()


def test_postgresql_runtime_participant_allows_same_root_worker_commit(tmp_path: Path) -> None:
    if os.environ.get("RUN_POSTGRES_CONSTRAINT_TESTS") != "1":
        pytest.skip("PostgreSQL runtime participant gate requires RUN_POSTGRES_CONSTRAINT_TESTS=1")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or not make_url(database_url).drivername.startswith("postgresql"):
        pytest.fail("PostgreSQL DATABASE_URL is required for the runtime participant gate")

    from backend.app.modules.final_cut_review.infra.database import (
        RuntimeParticipantLease,
        acquire_runtime_writer_lock,
        require_runtime_participant_session,
        runtime_participant_lease,
    )
    from backend.app.settings import Settings

    postgres_engine = create_engine(database_url, pool_pre_ping=True)
    settings = Settings(
        database_url=database_url,
        storage_root=tmp_path / "participant-storage",
        package_root=tmp_path / "participant-packages",
    )
    writer_lock = acquire_runtime_writer_lock(postgres_engine, settings)
    session_factory = sessionmaker(bind=postgres_engine, future=True)
    try:
        with runtime_participant_lease(postgres_engine, settings) as participant:
            assert isinstance(participant, RuntimeParticipantLease)
            assert participant.owner_backend_pid == writer_lock.backend_pid
            with session_factory() as worker_session:
                require_runtime_participant_session(worker_session)
                worker_session.execute(text("SELECT 1"))
                worker_session.commit()
    finally:
        writer_lock.release()
        postgres_engine.dispose()


def test_postgresql_runtime_participant_blocks_different_root_takeover_until_release(tmp_path: Path) -> None:
    if os.environ.get("RUN_POSTGRES_CONSTRAINT_TESTS") != "1":
        pytest.skip("PostgreSQL runtime participant gate requires RUN_POSTGRES_CONSTRAINT_TESTS=1")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or not make_url(database_url).drivername.startswith("postgresql"):
        pytest.fail("PostgreSQL DATABASE_URL is required for the runtime participant gate")

    from backend.app.modules.final_cut_review.infra.database import (
        acquire_runtime_participant_lease,
        acquire_runtime_writer_lock,
    )
    from backend.app.settings import Settings

    postgres_engine = create_engine(database_url, pool_pre_ping=True)
    first = Settings(
        database_url=database_url,
        storage_root=tmp_path / "first-participant-storage",
        package_root=tmp_path / "first-participant-packages",
    )
    second = Settings(
        database_url=database_url,
        storage_root=tmp_path / "second-participant-storage",
        package_root=tmp_path / "second-participant-packages",
    )
    writer_lock = acquire_runtime_writer_lock(postgres_engine, first)
    participant = None
    replacement = None
    try:
        participant = acquire_runtime_participant_lease(postgres_engine, first)
        writer_lock.release()
        with pytest.raises(RuntimeError, match="in-flight database writes prevent"):
            acquire_runtime_writer_lock(postgres_engine, second)
        participant.release()
        participant = None
        replacement = acquire_runtime_writer_lock(postgres_engine, second)
        replacement.assert_held()
    finally:
        if replacement is not None:
            replacement.release()
        if participant is not None:
            participant.release()
        if not writer_lock.released:
            writer_lock.release()
        postgres_engine.dispose()


def test_postgresql_runtime_writer_lock_rejects_second_root(tmp_path: Path) -> None:
    if os.environ.get("RUN_POSTGRES_CONSTRAINT_TESTS") != "1":
        pytest.skip("PostgreSQL runtime writer lock gate requires RUN_POSTGRES_CONSTRAINT_TESTS=1")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or not make_url(database_url).drivername.startswith("postgresql"):
        pytest.fail("PostgreSQL DATABASE_URL is required for the runtime writer lock gate")

    from backend.app.modules.final_cut_review.infra.database import acquire_runtime_writer_lock
    from backend.app.settings import Settings

    postgres_engine = create_engine(database_url, pool_pre_ping=True)
    first = Settings(
        database_url=database_url,
        storage_root=tmp_path / "first-storage",
        package_root=tmp_path / "first-packages",
    )
    second = Settings(
        database_url=database_url,
        storage_root=tmp_path / "second-storage",
        package_root=tmp_path / "second-packages",
    )
    lock = acquire_runtime_writer_lock(postgres_engine, first)
    try:
        lock.assert_held()
        with pytest.raises(RuntimeError, match="another backend runtime already owns"):
            acquire_runtime_writer_lock(postgres_engine, second)
    finally:
        lock.release()
        postgres_engine.dispose()


def test_postgresql_runtime_writer_fence_blocks_takeover_until_transaction_finishes(tmp_path: Path) -> None:
    if os.environ.get("RUN_POSTGRES_CONSTRAINT_TESTS") != "1":
        pytest.skip("PostgreSQL runtime writer fence gate requires RUN_POSTGRES_CONSTRAINT_TESTS=1")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url or not make_url(database_url).drivername.startswith("postgresql"):
        pytest.fail("PostgreSQL DATABASE_URL is required for the runtime writer fence gate")

    from backend.app.modules.final_cut_review.infra.database import acquire_runtime_writer_lock
    from backend.app.settings import Settings

    postgres_engine = create_engine(database_url, pool_pre_ping=True)
    settings = Settings(
        database_url=database_url,
        storage_root=tmp_path / "fenced-storage",
        package_root=tmp_path / "fenced-packages",
    )
    lock = acquire_runtime_writer_lock(postgres_engine, settings)
    session_factory = sessionmaker(bind=postgres_engine, future=True)
    replacement = None
    try:
        with session_factory() as transaction:
            lock.assert_transaction_held(transaction)
            lock.release()
            with pytest.raises(RuntimeError, match="in-flight database writes prevent"):
                acquire_runtime_writer_lock(postgres_engine, settings)
            transaction.commit()
        replacement = acquire_runtime_writer_lock(postgres_engine, settings)
        replacement.assert_held()
    finally:
        if replacement is not None:
            replacement.release()
        elif not lock.released:
            lock.release()
        postgres_engine.dispose()


def test_database_connect_args_bound_application_and_alembic_connections() -> None:
    from backend.app.modules.final_cut_review.infra.database import database_connect_args
    from backend.app.settings import Settings

    postgres = Settings(
        database_url="postgresql+psycopg://localhost/test",
        database_connect_timeout_seconds=7,
        database_statement_timeout_ms=12_345,
        database_migration_statement_timeout_ms=456_789,
    )
    sqlite = Settings(database_url="sqlite:///tmp/test.db", allow_sqlite_for_tests=True)

    assert database_connect_args(postgres) == {
        "connect_timeout": 7,
        "options": "-c statement_timeout=12345",
    }
    assert database_connect_args(sqlite) == {"check_same_thread": False}
    assert postgres.database_migration_statement_timeout_ms == 456_789


def _alembic_config(root: Path) -> Config:
    cfg = Config(str(root / "backend/alembic.ini"))
    cfg.set_main_option("script_location", str(root / "backend/alembic"))
    return cfg


def _set_migration_env(db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))
    monkeypatch.setenv("WRITE_GUARD_SESSION_SECRET", TEST_SIGNING_SECRET)
    settings_mod = __import__("backend.app.settings", fromlist=["get_settings"])
    settings_mod.get_settings.cache_clear()


def _metadata_diffs(db_path: Path) -> list[Any]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            migration_context = MigrationContext.configure(connection)
            return compare_metadata(migration_context, Base.metadata)
    finally:
        engine.dispose()


def _legacy_server_defaults(db_path: Path) -> dict[str, dict[str, Any]]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        return {
            table: {
                column["name"]: column.get("default")
                for column in inspector.get_columns(table)
                if column["name"] in {"owner_principal_id", "owner_principal_kind", "principal_hash"}
            }
            for table in ("file_objects", "upload_sessions", "idempotency_records")
        }
    finally:
        engine.dispose()


def _assert_no_legacy_server_defaults(db_path: Path) -> None:
    assert _legacy_server_defaults(db_path) == {
        "file_objects": {"owner_principal_id": None, "owner_principal_kind": None},
        "upload_sessions": {"owner_principal_id": None, "owner_principal_kind": None},
        "idempotency_records": {"principal_hash": None},
    }


def _principal_hash_default(db_path: Path) -> Any:
    return _legacy_server_defaults(db_path)["idempotency_records"]["principal_hash"]


def test_alembic_accepts_database_url_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "file-backed-migration.db"
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text(f"sqlite:///{db_path}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL_FILE", str(database_url_file))
    monkeypatch.setenv("ALLOW_SQLITE_FOR_TESTS", "true")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "storage"))
    monkeypatch.setenv("PACKAGE_ROOT", str(tmp_path / "packages"))

    alembic_command.upgrade(_alembic_config(root), "head")

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "alembic_version" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_alembic_upgrade_and_downgrade_are_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "migration.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    cfg = _alembic_config(root)
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "20260619_0001")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert "outbox_consumer_receipts" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "base")


def test_upload_quota_migration_backfills_peak_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "upload-quota-backfill.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)
    cfg = _alembic_config(root)
    alembic_command.upgrade(cfg, "20260713_0011")
    upload_id = "upl_legacy_quota_backfill"
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO upload_sessions (
                        id, original_filename, mime_type, declared_size, expected_sha256,
                        status, temp_path, owner_principal_id, owner_principal_kind,
                        received_parts, file_id, duration_ms, width, height, fps_num, fps_den
                    ) VALUES (
                        :upload_id, 'legacy.mp4', 'video/mp4', 321, :sha256,
                        'aborted', :temp_path, 'legacy-owner', 'system',
                        '{}', NULL, 1000, 1920, 1080, 25, 1
                    )
                    """
                ),
                {
                    "upload_id": upload_id,
                    "sha256": "a" * 64,
                    "temp_path": str(tmp_path / "storage" / "uploads" / f"{upload_id}.parts"),
                },
            )
    finally:
        engine.dispose()

    alembic_command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as connection:
            reserved_bytes = connection.scalar(
                text("SELECT reserved_bytes FROM upload_sessions WHERE id = :upload_id"),
                {"upload_id": upload_id},
            )
        assert reserved_bytes == 642
    finally:
        engine.dispose()


def test_alembic_metadata_matches_after_previous_head_upgrade_and_fresh_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(root)

    previous_db = tmp_path / "previous-head.db"
    _set_migration_env(previous_db, tmp_path, monkeypatch)
    alembic_command.upgrade(cfg, "20260621_0003")
    assert _principal_hash_default(previous_db) is not None
    alembic_command.upgrade(cfg, "head")
    assert _metadata_diffs(previous_db) == []
    _assert_no_legacy_server_defaults(previous_db)

    fresh_db = tmp_path / "fresh-head.db"
    _set_migration_env(fresh_db, tmp_path, monkeypatch)
    alembic_command.upgrade(cfg, "head")
    assert _metadata_diffs(fresh_db) == []
    _assert_no_legacy_server_defaults(fresh_db)


def test_alembic_head_to_previous_head_restores_principal_hash_default_then_reupgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).resolve().parents[2]
    cfg = _alembic_config(root)
    db_path = tmp_path / "downgrade-reupgrade.db"
    _set_migration_env(db_path, tmp_path, monkeypatch)

    alembic_command.upgrade(cfg, "head")
    assert _principal_hash_default(db_path) is None
    alembic_command.downgrade(cfg, "20260621_0003")
    assert _principal_hash_default(db_path) is not None
    alembic_command.upgrade(cfg, "head")
    assert _metadata_diffs(db_path) == []
    _assert_no_legacy_server_defaults(db_path)


def test_sqlalchemy_metadata_declares_migration_check_constraints() -> None:
    root = Path(__file__).resolve().parents[2]
    migration = (root / "backend/alembic/versions/20260619_0001_initial_final_cut_review.py").read_text()
    migration_check_names = set(re.findall(r'name="(ck_[^"]+)"', migration))
    metadata_check_names = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name is not None
    }

    assert sorted(migration_check_names - metadata_check_names) == []


def _expect_db_rejects(fn: Any) -> None:
    with pytest.raises((DBAPIError, IntegrityError)):
        fn()


def test_database_constraints_reject_revision_mutation_cross_project_freeze_and_duplicates(client: TestClient) -> None:
    project, item = create_project_item(client)
    issue = create_issue(client, project["project_ref_id"], item, ann=annotation("db"))
    session = SessionLocal()
    try:
        revision_id = issue["current_revision_id"]

        def mutate_revision() -> None:
            session.execute(update(ReviewIssueRevisionModel).where(ReviewIssueRevisionModel.id == revision_id).values(content="tampered"))
            session.commit()

        _expect_db_rejects(mutate_revision)
        session.rollback()

        def delete_revision() -> None:
            session.execute(delete(ReviewIssueRevisionModel).where(ReviewIssueRevisionModel.id == revision_id))
            session.commit()

        _expect_db_rejects(delete_revision)
        session.rollback()

        annotation_id = issue["playback_target"]["annotation_set_id"]
        assert annotation_id is not None

        def mutate_annotation() -> None:
            session.execute(update(ReviewAnnotationSetModel).where(ReviewAnnotationSetModel.id == annotation_id).values(shapes=[]))
            session.commit()

        _expect_db_rejects(mutate_annotation)
        session.rollback()

        def delete_annotation() -> None:
            session.execute(delete(ReviewAnnotationSetModel).where(ReviewAnnotationSetModel.id == annotation_id))
            session.commit()

        _expect_db_rejects(delete_annotation)
        session.rollback()

        other = create_project(client, "PDBX")

        def cross_project_issue() -> None:
            issue_id = "iss_cross"
            revision_id = "rev_cross"
            session.add(
                ReviewIssueModel(
                    id=issue_id,
                    project_ref_id=other["project_ref_id"],
                    review_item_id=item["id"],
                    version_id=item["current_version_id"],
                    issue_no=999,
                    status="unresolved",
                    current_revision_id=revision_id,
                    timestamp_ms=1,
                    frame_number=1,
                )
            )
            session.add(
                ReviewIssueRevisionModel(
                    id=revision_id,
                    project_ref_id=other["project_ref_id"],
                    review_item_id=item["id"],
                    version_id=item["current_version_id"],
                    issue_id=issue_id,
                    revision_no=1,
                    content="cross project",
                )
            )
            session.commit()

        _expect_db_rejects(cross_project_issue)
        session.rollback()

        def invalid_current_revision_insert() -> None:
            session.add(
                ReviewIssueModel(
                    id="iss_bad_current_revision",
                    project_ref_id=project["project_ref_id"],
                    review_item_id=item["id"],
                    version_id=item["current_version_id"],
                    issue_no=1001,
                    status="unresolved",
                    current_revision_id="rev_missing",
                    timestamp_ms=1,
                    frame_number=1,
                )
            )
            session.commit()

        _expect_db_rejects(invalid_current_revision_insert)
        session.rollback()

        def null_current_revision_insert() -> None:
            session.add(
                ReviewIssueModel(
                    id="iss_null_current_revision",
                    project_ref_id=project["project_ref_id"],
                    review_item_id=item["id"],
                    version_id=item["current_version_id"],
                    issue_no=1002,
                    status="unresolved",
                    current_revision_id=None,  # type: ignore[arg-type]
                    timestamp_ms=1,
                    frame_number=1,
                )
            )
            session.commit()

        _expect_db_rejects(null_current_revision_insert)
        session.rollback()

        current_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{project['project_ref_id']}/items/{item['id']}"))
        other_issue = create_issue(client, project["project_ref_id"], current_item, content="other issue")

        def cross_issue_current_revision() -> None:
            session.execute(
                update(ReviewIssueModel)
                .where(ReviewIssueModel.id == issue["id"])
                .values(current_revision_id=other_issue["current_revision_id"])
            )
            session.commit()

        _expect_db_rejects(cross_issue_current_revision)
        session.rollback()

        event = session.scalars(select(OutboxEventModel)).first()
        assert event is not None

        def duplicate_outbox_event() -> None:
            session.add(
                OutboxEventModel(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    event_version=1,
                    aggregate_type="project",
                    aggregate_id=project["project_ref_id"],
                    aggregate_version=1,
                    sequence=1,
                    project_ref_id=project["project_ref_id"],
                    correlation_id="dup",
                    metadata_json={},
                    payload={},
                )
            )
            session.commit()

        _expect_db_rejects(duplicate_outbox_event)
        session.rollback()

        session.add(
            IdempotencyRecordModel(
                key="idem-db",
                command_type="CreateProject",
                request_hash="a" * 64,
                principal_hash="a" * 64,
                response_json={},
                status_code=200,
            )
        )
        session.commit()

        def duplicate_idempotency_key() -> None:
            session.add(
                IdempotencyRecordModel(
                    key="idem-db",
                    command_type="CreateProject",
                    request_hash="b" * 64,
                    principal_hash="b" * 64,
                    response_json={},
                    status_code=200,
                )
            )
            session.commit()

        _expect_db_rejects(duplicate_idempotency_key)
        session.rollback()

        freeze_project = create_project(client, "PFREEZE")
        freeze_item = create_project_item_for_project(client, freeze_project["project_ref_id"])
        finalized = finalize(client, freeze_project["project_ref_id"], freeze_item)
        assert finalized["status"] == "active"

        def finalized_without_active_finalization() -> None:
            session.execute(
                update(ReviewItemModel)
                .where(ReviewItemModel.id == freeze_item["id"])
                .values(active_finalization_id=None)
            )
            session.commit()

        _expect_db_rejects(finalized_without_active_finalization)
        session.rollback()

        def mutate_finalization() -> None:
            session.execute(update(FinalizationRecordModel).where(FinalizationRecordModel.id == finalized["id"]).values(original_filename="tampered.mp4"))
            session.commit()

        _expect_db_rejects(mutate_finalization)
        session.rollback()

        def insert_after_finalization() -> None:
            session.add(
                ReviewIssueModel(
                    id="iss_after_final",
                    project_ref_id=freeze_project["project_ref_id"],
                    review_item_id=freeze_item["id"],
                    version_id=freeze_item["current_version_id"],
                    issue_no=1000,
                    status="unresolved",
                    timestamp_ms=1,
                    frame_number=1,
                )
            )
            session.commit()

        _expect_db_rejects(insert_after_finalization)
        session.rollback()

        drift_project = create_project(client, "PDRIFT")
        drift_item = create_project_item_for_project(client, drift_project["project_ref_id"])
        old_version_id = drift_item["current_version_id"]
        create_issue(client, drift_project["project_ref_id"], drift_item, content="needs v2")
        drift_item = api_data(client.get(f"/api/v1/final-cut-review/projects/{drift_project['project_ref_id']}/items/{drift_item['id']}"))
        upload = command("UploadReviewVersion", {"project_ref_id": drift_project["project_ref_id"], "review_item_id": drift_item["id"], "original_file_id": upload_video(client, filename="drift-v2.mp4", seed=b"d"), "change_summary": "v2"})
        uploaded = client.post(
            f"/api/v1/final-cut-review/edit/projects/{drift_project['project_ref_id']}/items/{drift_item['id']}/versions",
            json=upload,
            headers={"Idempotency-Key": upload["command_id"], "If-Match": str(drift_item["lock_version"])},
        )
        assert uploaded.status_code == 200, uploaded.text
        old_version = session.get(ReviewVersionModel, old_version_id)
        assert old_version is not None

        def current_version_must_point_to_current_true() -> None:
            session.execute(
                update(ReviewItemModel)
                .where(ReviewItemModel.id == drift_item["id"])
                .values(current_version_id=old_version_id)
            )
            session.commit()

        _expect_db_rejects(current_version_must_point_to_current_true)
        session.rollback()

        def null_sentinel_cannot_bypass_current_version_constraint() -> None:
            session.execute(
                update(ReviewItemModel)
                .where(ReviewItemModel.id == drift_item["id"])
                .values(current_version_id=old_version_id, current_version_is_current=None)
            )
            session.commit()

        _expect_db_rejects(null_sentinel_cannot_bypass_current_version_constraint)
        session.rollback()

        stale_finalization = FinalizationRecordModel(
            id="fin_old_version",
            project_ref_id=old_version.project_ref_id,
            review_item_id=old_version.review_item_id,
            version_id=old_version.id,
            version_no=old_version.version_no,
            original_file_id=old_version.original_file_id,
            original_filename=old_version.original_filename,
            mime_type=old_version.mime_type,
            file_size=old_version.file_size,
            sha256=old_version.sha256,
            duration_ms=old_version.duration_ms,
            width=old_version.width,
            height=old_version.height,
            fps_num=old_version.fps_num,
            fps_den=old_version.fps_den,
            media_probe_version=old_version.media_probe_version,
            status="active",
        )
        session.add(stale_finalization)
        session.commit()

        def active_finalization_must_match_current_version() -> None:
            session.execute(
                update(ReviewItemModel)
                .where(ReviewItemModel.id == drift_item["id"])
                .values(workflow_status="finalized", active_finalization_id=stale_finalization.id)
            )
            session.commit()

        _expect_db_rejects(active_finalization_must_match_current_version)
        session.rollback()

        def finalized_status_requires_current_version_id() -> None:
            session.execute(
                update(ReviewItemModel)
                .where(ReviewItemModel.id == drift_item["id"])
                .values(workflow_status="finalized", current_version_id=None, active_finalization_id=stale_finalization.id)
            )
            session.commit()

        _expect_db_rejects(finalized_status_requires_current_version_id)
        session.rollback()

        def delete_referenced_project() -> None:
            session.execute(delete(ProjectRefModel).where(ProjectRefModel.id == project["project_ref_id"]))
            session.commit()

        _expect_db_rejects(delete_referenced_project)
    finally:
        session.rollback()
        session.close()


def test_runtime_rejects_missing_optimistic_lock_archived_project_and_historical_message(client: TestClient) -> None:
    project, item = create_project_item(client)
    missing_issue_lock = command("CreateReviewIssue", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "version_id": item["current_version_id"], "content": "missing lock", "timestamp_ms": 100, "frame_number": 3})
    missing_issue_response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=missing_issue_lock,
        headers={"Idempotency-Key": missing_issue_lock["command_id"]},
    )
    assert missing_issue_response.status_code == 422
    assert api_error(missing_issue_response)["code"] == "VALIDATION_ERROR"

    missing_lock = command("FinalizeVersion", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "version_id": item["current_version_id"], "confirmed": True})
    response = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/finalize",
        json=missing_lock,
        headers={"Idempotency-Key": missing_lock["command_id"]},
    )
    assert response.status_code == 422
    assert api_error(response)["code"] == "VALIDATION_ERROR"

    archive = command("ArchiveProject", {"project_ref_id": project["project_ref_id"]})
    archived = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/archive",
        json=archive,
        headers={"If-Match": str(project["lock_version"])},
    )
    assert archived.status_code == 200, archived.text
    issue = command("CreateReviewIssue", {"project_ref_id": project["project_ref_id"], "review_item_id": item["id"], "version_id": item["current_version_id"], "content": "blocked", "timestamp_ms": 100, "frame_number": 3})
    denied = client.post(
        f"/api/v1/final-cut-review/review/projects/{project['project_ref_id']}/items/{item['id']}/versions/{item['current_version_id']}/issues",
        json=issue,
        headers={"Idempotency-Key": issue["command_id"], "If-Match": str(item["lock_version"])},
    )
    assert denied.status_code == 409
    assert api_error(denied)["code"] == "RESOURCE_STATE_CONFLICT"

    active = create_project(client, "PHIST")
    active_item = create_project_item_for_project(client, active["project_ref_id"])
    created_issue = create_issue(client, active["project_ref_id"], active_item)
    active_current = api_data(client.get(f"/api/v1/final-cut-review/projects/{active['project_ref_id']}/items/{active_item['id']}"))
    upload = command("UploadReviewVersion", {"project_ref_id": active["project_ref_id"], "review_item_id": active_item["id"], "original_file_id": upload_video(client, filename="hist-v2.mp4", seed=b"h"), "change_summary": "v2"})
    missing_upload_lock = client.post(
        f"/api/v1/final-cut-review/edit/projects/{active['project_ref_id']}/items/{active_item['id']}/versions",
        json=upload,
        headers={"Idempotency-Key": upload["command_id"]},
    )
    assert missing_upload_lock.status_code == 422
    uploaded = client.post(
        f"/api/v1/final-cut-review/edit/projects/{active['project_ref_id']}/items/{active_item['id']}/versions",
        json=upload,
        headers={"Idempotency-Key": upload["command_id"], "If-Match": str(active_current["lock_version"])},
    )
    assert uploaded.status_code == 200, uploaded.text
    message = command("AddReviewMessage", {"project_ref_id": active["project_ref_id"], "review_item_id": active_item["id"], "version_id": active_item["current_version_id"], "issue_id": created_issue["id"], "content": "late historical reply"})
    blocked = client.post(
        f"/api/v1/final-cut-review/review/projects/{active['project_ref_id']}/items/{active_item['id']}/versions/{active_item['current_version_id']}/issues/{created_issue['id']}/messages",
        json=message,
        headers={"Idempotency-Key": message["command_id"]},
    )
    assert blocked.status_code == 409
    assert api_error(blocked)["code"] == "VERSION_NOT_CURRENT"


def create_project_item_for_project(client: TestClient, project_ref_id: str) -> dict[str, Any]:
    body = command("CreateReviewItem", {"project_ref_id": project_ref_id, "item_code": "HIST001", "title": "Historical", "original_file_id": upload_video(client, filename="hist-v1.mp4", seed=b"g")})
    response = client.post(f"/api/v1/final-cut-review/edit/projects/{project_ref_id}/items", json=body, headers={"Idempotency-Key": body["command_id"]})
    assert response.status_code == 201, response.text
    return api_data(response)
