from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from pathlib import PurePosixPath

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from backend.app.safe_files import (
    UnsafeFilePathError,
    ensure_private_directory,
    pin_managed_root,
)
from backend.app.settings import Settings


_SQLITE_LOCK_DIRECTORY = ".storage-mutation-locks"
_SQLITE_SESSION_LOCKS_KEY = "fcr_storage_target_mutation_locks"
_LOCK_NAMESPACE = b"fcr-storage-target-mutation-v1\0"


def _canonical_target(logical_target: str) -> str:
    relative = PurePosixPath(logical_target)
    if (
        relative.is_absolute()
        or len(relative.parts) != 2
        or relative.parts[0] not in {"files", "packages"}
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise UnsafeFilePathError("storage mutation target is not canonical")
    return f"{relative.parts[0]}/{relative.parts[1]}"


def _target_digest(settings: Settings, canonical_target: str) -> bytes:
    storage_contract = (
        f"{os.path.abspath(settings.storage_root)}\0"
        f"{os.path.abspath(settings.package_root)}"
    ).encode()
    return hashlib.sha256(
        _LOCK_NAMESPACE + storage_contract + b"\0" + canonical_target.encode("utf-8"),
    ).digest()


def _release_sqlite_target_locks(
    session: Session,
    transaction: object,
) -> None:
    if getattr(transaction, "parent", None) is not None:
        return
    held = session.info.pop(_SQLITE_SESSION_LOCKS_KEY, None)
    if not isinstance(held, dict):
        return
    for descriptor in reversed(tuple(held.values())):
        if not isinstance(descriptor, int):
            continue
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass


event.listen(Session, "after_transaction_end", _release_sqlite_target_locks)


def _open_sqlite_target_lock(
    lock_root_fd: int,
    filename: str,
) -> tuple[int, bool]:
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    created = False
    try:
        descriptor = os.open(
            filename,
            flags | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=lock_root_fd,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(filename, flags, dir_fd=lock_root_fd)
    metadata = os.fstat(descriptor)
    root_metadata = os.fstat(lock_root_fd)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != root_metadata.st_dev
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise UnsafeFilePathError("storage mutation lock file is unsafe")
    return descriptor, created


def _acquire_sqlite_target_locks(
    session: Session,
    settings: Settings,
    targets: tuple[str, ...],
) -> None:
    session.connection()
    held = session.info.setdefault(_SQLITE_SESSION_LOCKS_KEY, {})
    if not isinstance(held, dict):
        raise RuntimeError("storage mutation lock session state is invalid")
    lock_root = ensure_private_directory(
        settings.storage_root,
        _SQLITE_LOCK_DIRECTORY,
    )
    newly_acquired: list[tuple[str, int]] = []
    try:
        with pin_managed_root(lock_root) as (_root, lock_root_fd):
            lock_root_metadata = os.fstat(lock_root_fd)
            if stat.S_IMODE(lock_root_metadata.st_mode) & 0o077:
                raise UnsafeFilePathError(
                    "storage mutation lock directory permissions are unsafe",
                )
            for target in targets:
                if target in held:
                    continue
                filename = f"{_target_digest(settings, target).hex()}.lock"
                descriptor, created = _open_sqlite_target_lock(
                    lock_root_fd,
                    filename,
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                except BaseException:
                    os.close(descriptor)
                    raise
                if created:
                    os.fsync(lock_root_fd)
                held[target] = descriptor
                newly_acquired.append((target, descriptor))
    except BaseException:
        for target, descriptor in reversed(newly_acquired):
            held.pop(target, None)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        raise


def acquire_storage_target_mutation_locks(
    session: Session,
    settings: Settings,
    logical_targets: set[str] | tuple[str, ...] | list[str],
) -> tuple[str, ...]:
    """Serialize canonical storage mutations until the outer transaction ends."""

    targets = tuple(sorted({_canonical_target(target) for target in logical_targets}))
    if not targets:
        return ()
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        for target in targets:
            lock_key = int.from_bytes(
                _target_digest(settings, target)[:8],
                byteorder="big",
                signed=True,
            )
            session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
        return targets
    if dialect == "sqlite":
        if not settings.allow_sqlite_for_tests:
            raise RuntimeError(
                "SQLite storage mutation locks require ALLOW_SQLITE_FOR_TESTS=true",
            )
        _acquire_sqlite_target_locks(session, settings, targets)
        return targets
    raise RuntimeError("storage mutation locks require PostgreSQL or test SQLite")
