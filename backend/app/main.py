from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.logging_config import configure_logging
from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.final_cut_review.infra.database import RuntimeWriterFenceUnavailable, bind_runtime_writer_lock
from backend.app.modules.review_access.policies import PrincipalContextSigner, SHARED_CODE_REQUEST_MAX_BYTES
from backend.app.modules.review_http.command_routes import router as command_router
from backend.app.modules.review_http.context_dependencies import is_verified_reverse_proxy_write
from backend.app.modules.review_http.envelope import error_content, error_response, http_exception_response, unhandled_exception_response, validation_error_response
from backend.app.modules.review_http.query_routes import router as query_router
from backend.app.modules.review_http.upload_routes import router as upload_router
from backend.app.safe_files import UnsafeFilePathError, contained_path, pin_regular_file
from backend.app.settings import get_settings


settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[2]

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SAFE_FETCH_SITES = {"", "none", "same-origin", "same-site"}
ALLOWED_CORS_METHODS = "GET,POST,PUT,PATCH,OPTIONS"
ALLOWED_CORS_HEADERS = ",".join(
    [
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "If-Match",
        "Range",
        "X-Allowed-Project-Refs",
        "X-Capability",
        "X-Principal-Context",
        "X-Principal-Id",
        "X-Principal-Kind",
        "X-Package-Download-Token",
        "X-Request-ID",
    ]
)
SHARED_CODE_SESSION_PATH = "/api/v1/final-cut-review/write-guard/session"


def safe_request_id(raw_request_id: str | None) -> str:
    if raw_request_id is None:
        return uuid.uuid4().hex
    try:
        return uuid.UUID(raw_request_id.strip()).hex
    except (ValueError, AttributeError) as exc:
        raise ValueError("X-Request-ID must be a UUID") from exc


def apply_security_headers(response: Response, request_id: str) -> Response:
    response.headers["X-Request-ID"] = request_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
    return response


async def enforce_shared_code_request_limit(request: Request, request_id: str) -> Response | None:
    if request.method != "POST" or request.url.path != SHARED_CODE_SESSION_PATH:
        return None
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = SHARED_CODE_REQUEST_MAX_BYTES + 1
        if declared_length < 0 or declared_length > SHARED_CODE_REQUEST_MAX_BYTES:
            return JSONResponse(
                status_code=413,
                content=error_content("FILE_TOO_LARGE", "请求体过大", 413, request_id),
            )
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > SHARED_CODE_REQUEST_MAX_BYTES:
            return JSONResponse(
                status_code=413,
                content=error_content("FILE_TOO_LARGE", "请求体过大", 413, request_id),
            )
        body.extend(chunk)
    request._body = bytes(body)
    return None


def enforce_runtime_writer_lock(app: FastAPI, request: Request, request_id: str) -> Response | None:
    if request.method not in MUTATING_METHODS:
        return None
    runtime_writer_lock = getattr(app.state, "runtime_writer_lock", None)
    try:
        if runtime_writer_lock is None:
            raise RuntimeError("runtime writer lock is unavailable")
        runtime_writer_lock.assert_held()
    except (RuntimeError, SQLAlchemyError):
        logger.error("mutating request rejected because the runtime writer lock is not held")
        return JSONResponse(
            status_code=503,
            content=error_content("STORAGE_UNAVAILABLE", "数据库写入租约不可用", 503, request_id),
        )
    return None


def configured_browser_origins() -> set[str]:
    return {origin.strip().rstrip("/") for origin in settings.browser_allowed_origins.split(",") if origin.strip()}


def is_allowed_browser_origin(origin: str | None) -> bool:
    if not origin:
        return False
    normalized_origin = origin.strip().rstrip("/")
    return normalized_origin in configured_browser_origins()


def is_trusted_reverse_proxy_write(request: Request) -> bool:
    runtime_settings = get_settings()
    return runtime_settings.write_guard_mode == "reverse_proxy" and is_verified_reverse_proxy_write(request, runtime_settings)


def is_signed_service_write(request: Request) -> bool:
    """Allow originless non-browser clients only with a valid service principal."""
    principal = PrincipalContextSigner(get_settings()).verify(request.headers.get("x-principal-context"))
    return principal.kind == "service" and bool(principal.id)


def apply_cors_headers(response: Response, request: Request) -> Response:
    origin = request.headers.get("origin")
    if is_allowed_browser_origin(origin) and origin:
        response.headers["Access-Control-Allow-Origin"] = origin.strip().rstrip("/")
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = ALLOWED_CORS_METHODS
        response.headers["Access-Control-Allow-Headers"] = ALLOWED_CORS_HEADERS
        response.headers["Access-Control-Expose-Headers"] = "Content-Disposition,Content-Range,X-Request-ID"
        response.headers.add_vary_header("Origin")
    return response


def apply_browser_response_headers(response: Response, request: Request, request_id: str) -> Response:
    return apply_security_headers(apply_cors_headers(response, request), request_id)


def browser_boundary_error(request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content=error_content("PRINCIPAL_PERMISSION_DENIED", "浏览器跨站写请求被拒绝", 403, request_id),
    )


def reject_browser_write_boundary(request: Request, request_id: str) -> JSONResponse | None:
    if request.method not in MUTATING_METHODS:
        return None
    fetch_site = request.headers.get("sec-fetch-site", "").lower()
    if fetch_site not in SAFE_FETCH_SITES:
        return browser_boundary_error(request_id)
    origin = request.headers.get("origin")
    if origin:
        if not is_allowed_browser_origin(origin):
            return browser_boundary_error(request_id)
        return None
    if not is_trusted_reverse_proxy_write(request) and not is_signed_service_write(request):
        return browser_boundary_error(request_id)
    return None


def alembic_head_revision() -> str:
    alembic_cfg = Config(str(ROOT_DIR / "backend/alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(ROOT_DIR / "backend/alembic"))
    head = ScriptDirectory.from_config(alembic_cfg).get_current_head()
    if not head:
        raise RuntimeError("Alembic head revision is missing")
    return head


def database_runtime_readiness(runtime_writer_lock: object | None = None) -> dict[str, str]:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal

    if runtime_writer_lock is not None:
        assert_held = getattr(runtime_writer_lock, "assert_held", None)
        if not callable(assert_held):
            raise RuntimeError("runtime writer lock state is invalid")
        assert_held()
    head = alembic_head_revision()
    with SessionLocal() as session:
        session.execute(text("SELECT 1")).scalar_one()
        database_engine = session.get_bind().dialect.name
        current = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    if current != head:
        raise RuntimeError("Alembic current revision does not match head")
    return {
        "database": "ok",
        "database_engine": database_engine,
        "alembic_current": current,
        "alembic_head": head,
    }


def database_readiness(
    runtime_writer_lock: object | None = None,
    runtime_settings: object | None = None,
) -> dict[str, str]:
    from backend.app.modules.final_cut_review.infra.database import SessionLocal
    from backend.app.modules.final_cut_review.infra.sqlalchemy_models import (
        FileObjectModel,
        FinalCutPackageSnapshotModel,
        utcnow,
    )

    managed_settings = runtime_settings or settings
    storage_root = getattr(managed_settings, "storage_root", None)
    package_root = getattr(managed_settings, "package_root", None)
    if not isinstance(storage_root, Path) or not isinstance(package_root, Path):
        raise RuntimeError("runtime managed storage settings are invalid")
    runtime = database_runtime_readiness(runtime_writer_lock)
    with SessionLocal() as session:
        files = session.execute(select(FileObjectModel.id, FileObjectModel.storage_path)).all()
        packages = session.execute(
            select(FinalCutPackageSnapshotModel.id, FinalCutPackageSnapshotModel.storage_path).where(
                FinalCutPackageSnapshotModel.status == "ready",
                FinalCutPackageSnapshotModel.expires_at >= utcnow(),
            )
        ).all()
    try:
        for file_id, storage_path in files:
            _assert_managed_regular_file(storage_path, storage_root, Path("files") / file_id)
        for package_id, storage_path in packages:
            _assert_managed_regular_file(storage_path, package_root, Path(f"{package_id}.zip"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("persistent file association is unavailable") from exc
    return {
        **runtime,
        "storage": "ok",
        "file_associations": str(len(files)),
        "package_associations": str(len(packages)),
    }


def _assert_managed_regular_file(path_value: str, root: Path, expected_path: Path) -> None:
    actual = contained_path(path_value, root)
    expected = contained_path(expected_path, root)
    if actual != expected:
        raise UnsafeFilePathError("stored path does not match managed storage contract")
    with pin_regular_file(actual, root) as pinned:
        if pinned is None or not pinned.exists:
            raise FileNotFoundError(actual)


def create_app() -> FastAPI:
    app_settings = get_settings()

    @asynccontextmanager
    async def lifespan(lifespan_app: FastAPI) -> AsyncIterator[None]:
        from backend.app.modules.final_cut_review.infra.database import acquire_runtime_writer_lock, engine

        runtime_writer_lock = acquire_runtime_writer_lock(engine, app_settings)
        try:
            database_readiness(runtime_writer_lock, app_settings)
            lifespan_app.state.runtime_writer_lock = runtime_writer_lock
            logger.info("backend started; schema governance is Alembic-only")
            yield
        finally:
            try:
                runtime_writer_lock.release()
            except Exception as exc:
                logger.warning(
                    "runtime writer lock release failed during shutdown",
                    extra={"error_type": type(exc).__name__},
                )
            finally:
                lifespan_app.state.runtime_writer_lock = None

    app = FastAPI(title="Final Cut Review Backend", version="1.3.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_trace(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        try:
            request_id = safe_request_id(request.headers.get("x-request-id"))
        except ValueError:
            request_id = uuid.uuid4().hex
            validation_response = JSONResponse(
                status_code=422,
                content=error_content("VALIDATION_ERROR", "X-Request-ID 必须是 UUID", 422, request_id),
            )
            return apply_browser_response_headers(validation_response, request, request_id)
        request.state.request_id = request_id
        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            if not is_allowed_browser_origin(request.headers.get("origin")):
                return apply_browser_response_headers(browser_boundary_error(request_id), request, request_id)
            return apply_browser_response_headers(Response(status_code=204), request, request_id)
        if boundary_response := reject_browser_write_boundary(request, request_id):
            return apply_browser_response_headers(boundary_response, request, request_id)
        if writer_lock_response := enforce_runtime_writer_lock(app, request, request_id):
            return apply_browser_response_headers(writer_lock_response, request, request_id)
        if size_response := await enforce_shared_code_request_limit(request, request_id):
            return apply_browser_response_headers(size_response, request, request_id)
        runtime_writer_lock = getattr(app.state, "runtime_writer_lock", None)
        if runtime_writer_lock is None:
            return apply_browser_response_headers(
                JSONResponse(
                    status_code=503,
                    content=error_content("STORAGE_UNAVAILABLE", "数据库写入租约不可用", 503, request_id),
                ),
                request,
                request_id,
            )
        with bind_runtime_writer_lock(runtime_writer_lock):
            response = await call_next(request)
        return apply_browser_response_headers(response, request, request_id)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "contract_version": "1.0"}

    @app.get("/runtimez")
    def runtimez() -> dict[str, str]:
        try:
            runtime_writer_lock = getattr(app.state, "runtime_writer_lock", None)
            if runtime_writer_lock is None:
                raise RuntimeError("runtime writer lock is not active")
            return {
                "status": "ready",
                "contract_version": "1.0",
                **database_runtime_readiness(runtime_writer_lock),
            }
        except (RuntimeError, SQLAlchemyError) as exc:
            logger.warning("backend runtime check failed: %s", exc.__class__.__name__)
            raise HTTPException(status_code=503, detail="runtime readiness failed") from exc

    @app.get("/readyz")
    def readyz() -> dict[str, str]:
        try:
            runtime_writer_lock = getattr(app.state, "runtime_writer_lock", None)
            if runtime_writer_lock is None:
                raise RuntimeError("runtime writer lock is not active")
            return {
                "status": "ready",
                "contract_version": "1.0",
                **database_readiness(runtime_writer_lock, app_settings),
            }
        except (RuntimeError, SQLAlchemyError) as exc:
            logger.warning("backend readiness check failed: %s", exc.__class__.__name__)
            raise HTTPException(status_code=503, detail="runtime readiness failed") from exc

    @app.exception_handler(ReviewError)
    async def review_error_handler(request: Request, exc: ReviewError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", request.headers.get("x-request-id", uuid.uuid4().hex))
        return error_response(exc, request_id)

    @app.exception_handler(RuntimeWriterFenceUnavailable)
    async def runtime_writer_fence_handler(request: Request, exc: RuntimeWriterFenceUnavailable) -> JSONResponse:
        del exc
        request_id = getattr(request.state, "request_id", request.headers.get("x-request-id", uuid.uuid4().hex))
        return JSONResponse(
            status_code=503,
            content=error_content("STORAGE_UNAVAILABLE", "数据库写入租约不可用", 503, request_id),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", request.headers.get("x-request-id", uuid.uuid4().hex))
        return validation_error_response(exc, request_id)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = getattr(request.state, "request_id", request.headers.get("x-request-id", uuid.uuid4().hex))
        return http_exception_response(exc, request_id)

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = getattr(request.state, "request_id", request.headers.get("x-request-id", uuid.uuid4().hex))
        return http_exception_response(exc, request_id)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        request_id = getattr(request.state, "request_id", request.headers.get("x-request-id", uuid.uuid4().hex))
        logger.error(
            "unhandled backend exception",
            extra={"request_id": request_id, "error_type": type(exc).__name__},
        )
        return apply_browser_response_headers(unhandled_exception_response(request_id), request, request_id)

    app.include_router(query_router)
    app.include_router(command_router)
    app.include_router(upload_router)
    return app


app = create_app()
