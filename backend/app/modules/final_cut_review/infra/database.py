from __future__ import annotations

import hashlib
import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.app.settings import Settings, get_database_settings


class Base(DeclarativeBase):
    pass


RUNTIME_WRITER_LOCK_NAMESPACE = "fj-final-cut-review:database-writer:v1"
RUNTIME_STORAGE_CONTRACT_NAMESPACE = "fj-final-cut-review:managed-storage:v1"
RUNTIME_TRANSACTION_FENCE_NAMESPACE = "fj-final-cut-review:transaction-fence:v1"


def _advisory_lock_key(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], byteorder="big", signed=True)


def runtime_writer_lock_keys(database_name: str, settings: Settings) -> tuple[int, int, int]:
    root_contract = hashlib.sha256(f"{settings.storage_root}\0{settings.package_root}".encode()).hexdigest()
    writer_key = _advisory_lock_key(f"{database_name}\0{RUNTIME_WRITER_LOCK_NAMESPACE}")
    contract_key = _advisory_lock_key(
        f"{database_name}\0{RUNTIME_STORAGE_CONTRACT_NAMESPACE}\0{root_contract}"
    )
    fence_key = _advisory_lock_key(f"{database_name}\0{RUNTIME_TRANSACTION_FENCE_NAMESPACE}")
    return writer_key, contract_key, fence_key


def _advisory_lock_parts(lock_key: int) -> tuple[int, int]:
    unsigned = lock_key & ((1 << 64) - 1)
    return unsigned >> 32, unsigned & 0xFFFFFFFF


class RuntimeWriterFenceUnavailable(RuntimeError):
    pass


@dataclass(slots=True)
class RuntimeWriterLock:
    connection: Connection | None
    writer_key: int | None
    contract_key: int | None
    fence_key: int | None
    backend_pid: int | None
    sqlite_test_exemption: bool = False
    released: bool = False
    _connection_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def assert_held(self) -> None:
        with self._connection_lock:
            if self.released:
                raise RuntimeError("runtime writer lock has already been released")
            if self.sqlite_test_exemption:
                return
            if self.connection is None or self.connection.closed or self.backend_pid is None:
                raise RuntimeError("runtime writer lock connection is unavailable")
            current_pid = self.connection.scalar(text("SELECT pg_backend_pid()"))
            if current_pid is None or int(current_pid) != self.backend_pid:
                raise RuntimeError("runtime writer lock connection identity changed")
            for lock_key, label in ((self.writer_key, "writer"), (self.contract_key, "storage contract")):
                if lock_key is None:
                    raise RuntimeError(f"runtime {label} lock key is unavailable")
                class_id, object_id = _advisory_lock_parts(lock_key)
                held = self.connection.scalar(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                        "AND pid = pg_backend_pid() AND classid::bigint = :class_id "
                        "AND objid::bigint = :object_id AND objsubid = 1 "
                        "AND mode = 'ExclusiveLock' AND granted)"
                    ),
                    {"class_id": class_id, "object_id": object_id},
                )
                if held is not True:
                    raise RuntimeError(f"runtime {label} advisory lock is no longer held")

    def assert_transaction_held(self, session: Session) -> None:
        if self.released:
            raise RuntimeWriterFenceUnavailable("runtime writer lock has already been released")
        if self.sqlite_test_exemption:
            return
        if self.fence_key is None:
            raise RuntimeWriterFenceUnavailable("runtime transaction fence is unavailable")
        try:
            session.execute(
                text("SELECT pg_advisory_xact_lock_shared(:lock_key)"),
                {"lock_key": self.fence_key},
            )
            self.assert_held()
        except Exception as exc:
            raise RuntimeWriterFenceUnavailable("runtime writer transaction fence is unavailable") from exc

    def release(self) -> None:
        with self._connection_lock:
            if self.released:
                return
            self.released = True
            if self.sqlite_test_exemption:
                return
            connection = self.connection
            if connection is None or self.writer_key is None or self.contract_key is None:
                raise RuntimeError("runtime writer lock release state is incomplete")
            release_error: Exception | None = None
            try:
                current_pid = connection.scalar(text("SELECT pg_backend_pid()"))
                if current_pid is None or int(current_pid) != self.backend_pid:
                    raise RuntimeError("runtime writer lock must be released by its acquiring connection")
                contract_released = connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self.contract_key},
                )
                writer_released = connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": self.writer_key},
                )
                if contract_released is not True or writer_released is not True:
                    raise RuntimeError("runtime writer lock release was not confirmed")
            except Exception as exc:
                release_error = exc
            finally:
                connection.close()
                self.connection = None
            if release_error is not None:
                raise release_error


@dataclass(slots=True)
class RuntimeParticipantLease:
    connection: Connection | None
    writer_key: int | None
    contract_key: int | None
    fence_key: int | None
    backend_pid: int | None
    owner_backend_pid: int | None
    database_name: str | None
    sqlite_test_exemption: bool = False
    released: bool = False
    _connection_lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def assert_held(self) -> None:
        with self._connection_lock:
            if self.released:
                raise RuntimeWriterFenceUnavailable("runtime participant lease has already been released")
            if self.sqlite_test_exemption:
                return
            if self.connection is None or self.connection.closed or self.backend_pid is None:
                raise RuntimeWriterFenceUnavailable("runtime participant lease connection is unavailable")
            if self.fence_key is None:
                raise RuntimeWriterFenceUnavailable("runtime participant transaction fence is unavailable")
            current_pid = self.connection.scalar(text("SELECT pg_backend_pid()"))
            if current_pid is None or int(current_pid) != self.backend_pid:
                raise RuntimeWriterFenceUnavailable("runtime participant lease connection identity changed")
            class_id, object_id = _advisory_lock_parts(self.fence_key)
            held = self.connection.scalar(
                text(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid() AND classid::bigint = :class_id "
                    "AND objid::bigint = :object_id AND objsubid = 1 "
                    "AND mode = 'ShareLock' AND granted)"
                ),
                {"class_id": class_id, "object_id": object_id},
            )
            if held is not True:
                raise RuntimeWriterFenceUnavailable("runtime participant transaction fence is no longer held")

    def assert_transaction_held(self, session: Session) -> None:
        if self.released:
            raise RuntimeWriterFenceUnavailable("runtime participant lease has already been released")
        bind = session.get_bind()
        if self.sqlite_test_exemption:
            if bind.dialect.name != "sqlite":
                raise RuntimeWriterFenceUnavailable("SQLite participant exemption cannot fence a non-SQLite session")
            return
        if bind.dialect.name != "postgresql" or self.fence_key is None or self.database_name is None:
            raise RuntimeWriterFenceUnavailable("runtime participant transaction fence is unavailable")
        try:
            transaction_fenced = session.scalar(
                text("SELECT pg_try_advisory_xact_lock_shared(:lock_key)"),
                {"lock_key": self.fence_key},
            )
            if transaction_fenced is not True:
                raise RuntimeWriterFenceUnavailable("runtime participant transaction fence could not be acquired")
            session_database = session.scalar(text("SELECT current_database()"))
            if session_database != self.database_name:
                raise RuntimeWriterFenceUnavailable("runtime participant session targets a different database")
            self.assert_held()
        except Exception as exc:
            if isinstance(exc, RuntimeWriterFenceUnavailable):
                raise
            raise RuntimeWriterFenceUnavailable("runtime participant transaction fence is unavailable") from exc

    def release(self) -> None:
        with self._connection_lock:
            if self.released:
                return
            self.released = True
            if self.sqlite_test_exemption:
                return
            connection = self.connection
            if connection is None or self.fence_key is None or self.backend_pid is None:
                raise RuntimeError("runtime participant lease release state is incomplete")
            release_error: Exception | None = None
            try:
                current_pid = connection.scalar(text("SELECT pg_backend_pid()"))
                if current_pid is None or int(current_pid) != self.backend_pid:
                    raise RuntimeError("runtime participant lease must be released by its acquiring connection")
                released = connection.scalar(
                    text("SELECT pg_advisory_unlock_shared(:lock_key)"),
                    {"lock_key": self.fence_key},
                )
                if released is not True:
                    raise RuntimeError("runtime participant lease release was not confirmed")
            except Exception as exc:
                release_error = exc
            finally:
                connection.close()
                self.connection = None
            if release_error is not None:
                raise release_error


def acquire_runtime_writer_lock(runtime_engine: Engine, settings: Settings) -> RuntimeWriterLock:
    drivername = runtime_engine.dialect.name
    if drivername == "sqlite":
        if not settings.allow_sqlite_for_tests:
            raise RuntimeError("SQLite runtime writer lock exemption requires ALLOW_SQLITE_FOR_TESTS=true")
        return RuntimeWriterLock(None, None, None, None, None, sqlite_test_exemption=True)
    if drivername != "postgresql":
        raise RuntimeError("runtime writer lock requires PostgreSQL")

    connection = runtime_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    writer_key: int | None = None
    contract_key: int | None = None
    fence_key: int | None = None
    writer_acquired = False
    contract_acquired = False
    fence_acquired = False
    try:
        database_name = connection.scalar(text("SELECT current_database()"))
        if not isinstance(database_name, str) or not database_name:
            raise RuntimeError("runtime writer lock could not identify the database")
        writer_key, contract_key, fence_key = runtime_writer_lock_keys(database_name, settings)
        fence_acquired = (
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": fence_key},
            )
            is True
        )
        if not fence_acquired:
            raise RuntimeError("in-flight database writes prevent runtime writer lock acquisition")
        writer_acquired = (
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": writer_key},
            )
            is True
        )
        if not writer_acquired:
            raise RuntimeError("another backend runtime already owns this database writer lock")
        contract_acquired = (
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_key)"),
                {"lock_key": contract_key},
            )
            is True
        )
        if not contract_acquired:
            raise RuntimeError("managed storage contract is already owned by another runtime")
        backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
        if backend_pid is None:
            raise RuntimeError("runtime writer lock connection has no backend identity")
        fence_released = connection.scalar(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": fence_key},
        )
        if fence_released is not True:
            raise RuntimeError("runtime transaction fence release was not confirmed")
        fence_acquired = False
        return RuntimeWriterLock(connection, writer_key, contract_key, fence_key, int(backend_pid))
    except Exception:
        if contract_acquired and contract_key is not None:
            try:
                connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": contract_key},
                )
            except Exception:
                pass
        if writer_acquired and writer_key is not None:
            try:
                connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": writer_key},
                )
            except Exception:
                pass
        if fence_acquired and fence_key is not None:
            try:
                connection.scalar(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": fence_key},
                )
            except Exception:
                pass
        connection.close()
        raise


def acquire_runtime_participant_lease(runtime_engine: Engine, settings: Settings) -> RuntimeParticipantLease:
    drivername = runtime_engine.dialect.name
    if drivername == "sqlite":
        if not settings.allow_sqlite_for_tests:
            raise RuntimeError("SQLite runtime participant exemption requires ALLOW_SQLITE_FOR_TESTS=true")
        return RuntimeParticipantLease(None, None, None, None, None, None, None, sqlite_test_exemption=True)
    if drivername != "postgresql":
        raise RuntimeError("runtime participant lease requires PostgreSQL")

    connection = runtime_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    fence_key: int | None = None
    fence_acquired = False
    try:
        database_name = connection.scalar(text("SELECT current_database()"))
        if not isinstance(database_name, str) or not database_name:
            raise RuntimeWriterFenceUnavailable("runtime participant could not identify the database")
        writer_key, contract_key, fence_key = runtime_writer_lock_keys(database_name, settings)
        fence_acquired = (
            connection.scalar(
                text("SELECT pg_try_advisory_lock_shared(:lock_key)"),
                {"lock_key": fence_key},
            )
            is True
        )
        if not fence_acquired:
            raise RuntimeWriterFenceUnavailable("runtime writer acquisition is already in progress")
        backend_pid = connection.scalar(text("SELECT pg_backend_pid()"))
        if backend_pid is None:
            raise RuntimeWriterFenceUnavailable("runtime participant connection has no backend identity")

        writer_class_id, writer_object_id = _advisory_lock_parts(writer_key)
        contract_class_id, contract_object_id = _advisory_lock_parts(contract_key)
        owner_backend_pids = list(
            connection.execute(
                text(
                    "SELECT DISTINCT writer.pid FROM pg_locks AS writer "
                    "JOIN pg_locks AS contract ON contract.pid = writer.pid "
                    "WHERE writer.locktype = 'advisory' AND contract.locktype = 'advisory' "
                    "AND writer.database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
                    "AND contract.database = writer.database "
                    "AND writer.classid::bigint = :writer_class_id "
                    "AND writer.objid::bigint = :writer_object_id AND writer.objsubid = 1 "
                    "AND contract.classid::bigint = :contract_class_id "
                    "AND contract.objid::bigint = :contract_object_id AND contract.objsubid = 1 "
                    "AND writer.mode = 'ExclusiveLock' AND contract.mode = 'ExclusiveLock' "
                    "AND writer.granted AND contract.granted"
                ),
                {
                    "writer_class_id": writer_class_id,
                    "writer_object_id": writer_object_id,
                    "contract_class_id": contract_class_id,
                    "contract_object_id": contract_object_id,
                },
            ).scalars()
        )
        if len(owner_backend_pids) != 1 or int(owner_backend_pids[0]) == int(backend_pid):
            raise RuntimeWriterFenceUnavailable(
                "no active backend runtime owns the current database writer and managed storage contract"
            )
        lease = RuntimeParticipantLease(
            connection=connection,
            writer_key=writer_key,
            contract_key=contract_key,
            fence_key=fence_key,
            backend_pid=int(backend_pid),
            owner_backend_pid=int(owner_backend_pids[0]),
            database_name=database_name,
        )
        lease.assert_held()
        return lease
    except Exception:
        if fence_acquired and fence_key is not None:
            try:
                connection.scalar(
                    text("SELECT pg_advisory_unlock_shared(:lock_key)"),
                    {"lock_key": fence_key},
                )
            except Exception:
                pass
        connection.close()
        raise


RuntimeFenceBinding = RuntimeWriterLock | RuntimeParticipantLease
RUNTIME_PARTICIPANT_REQUIRED_SESSION_KEY = "runtime_participant_required"


_REQUEST_RUNTIME_WRITER_LOCK: ContextVar[RuntimeFenceBinding | None] = ContextVar(
    "request_runtime_writer_lock",
    default=None,
)


@contextmanager
def bind_runtime_writer_lock(lock: RuntimeWriterLock) -> Generator[None, None, None]:
    token = _REQUEST_RUNTIME_WRITER_LOCK.set(lock)
    try:
        yield
    finally:
        _REQUEST_RUNTIME_WRITER_LOCK.reset(token)


@contextmanager
def runtime_participant_lease(
    runtime_engine: Engine,
    settings: Settings,
) -> Generator[RuntimeFenceBinding, None, None]:
    existing = _REQUEST_RUNTIME_WRITER_LOCK.get()
    if existing is not None:
        existing.assert_held()
        yield existing
        return

    lease = acquire_runtime_participant_lease(runtime_engine, settings)
    token = _REQUEST_RUNTIME_WRITER_LOCK.set(lease)
    try:
        yield lease
    finally:
        _REQUEST_RUNTIME_WRITER_LOCK.reset(token)
        lease.release()


def current_runtime_writer_lock() -> RuntimeWriterLock:
    lock = _REQUEST_RUNTIME_WRITER_LOCK.get()
    if lock is None or isinstance(lock, RuntimeParticipantLease):
        raise RuntimeWriterFenceUnavailable("runtime writer lock is not bound to the current operation")
    return lock


def require_runtime_participant_session(session: Session) -> None:
    session.info[RUNTIME_PARTICIPANT_REQUIRED_SESSION_KEY] = True
    binding = _REQUEST_RUNTIME_WRITER_LOCK.get()
    if binding is None:
        raise RuntimeWriterFenceUnavailable("runtime participant lease is not bound to the current operation")
    binding.assert_held()


@event.listens_for(Session, "before_commit")
def fence_request_transaction(session: Session) -> None:
    lock = _REQUEST_RUNTIME_WRITER_LOCK.get()
    if lock is None:
        if session.info.get(RUNTIME_PARTICIPANT_REQUIRED_SESSION_KEY) is True:
            raise RuntimeWriterFenceUnavailable("runtime participant lease is not bound before commit")
        return
    lock.assert_transaction_held(session)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection: object, connection_record: object) -> None:
    del connection_record
    if "sqlite" not in type(dbapi_connection).__module__:
        return
    cursor = getattr(dbapi_connection, "cursor", lambda: None)()
    if cursor is not None:
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()


def database_connect_args(settings: Settings) -> dict[str, object]:
    if settings.database_url.startswith("sqlite"):
        return {"check_same_thread": False}
    return {
        "connect_timeout": settings.database_connect_timeout_seconds,
        "options": f"-c statement_timeout={settings.database_statement_timeout_ms}",
    }


def make_engine() -> Engine:
    settings = get_database_settings()
    return create_engine(settings.database_url, future=True, pool_pre_ping=True, connect_args=database_connect_args(settings))


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
