from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from backend.app.modules.final_cut_review.domain.errors import ReviewError
from backend.app.modules.review_contracts.generated import CONTRACT_VERSION, ERROR_HTTP_STATUS


def ok(data: Any, request_id: str) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": request_id, "contract_version": CONTRACT_VERSION}}


def list_ok(data: list[Any], request_id: str, page: int, page_size: int) -> dict[str, Any]:
    start = (page - 1) * page_size
    end = start + page_size
    return {
        "data": data[start:end],
        "meta": {
            "request_id": request_id,
            "contract_version": CONTRACT_VERSION,
            "total_count": len(data),
            "page": page,
            "page_size": page_size,
        },
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def error_content(code: str, message: str, http_status: int, request_id: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "http_status": http_status,
            "details": jsonable_encoder(details or {}),
            "request_id": request_id,
            "timestamp": _timestamp(),
            "contract_version": CONTRACT_VERSION,
        }
    }


def error_response(error: ReviewError, request_id: str) -> JSONResponse:
    status = ERROR_HTTP_STATUS.get(error.code, 500)
    return JSONResponse(
        status_code=status,
        content=error_content(error.code, error.message, status, request_id, error.details),
    )


def validation_error_response(error: Any, request_id: str) -> JSONResponse:
    sanitized_errors = []
    for item in error.errors():
        sanitized_errors.append(
            {
                key: value
                for key, value in item.items()
                if key in {"type", "loc", "msg", "url"}
            }
        )
    return JSONResponse(
        status_code=422,
        content=error_content("VALIDATION_ERROR", "请求字段非法", 422, request_id, {"errors": sanitized_errors}),
    )


def http_exception_response(error: HTTPException, request_id: str) -> JSONResponse:
    status = int(error.status_code)
    code = {
        400: "VALIDATION_ERROR",
        401: "PRINCIPAL_AUTHENTICATION_REQUIRED",
        403: "PRINCIPAL_PERMISSION_DENIED",
        404: "RESOURCE_NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "RESOURCE_STATE_CONFLICT",
        413: "FILE_TOO_LARGE",
        422: "VALIDATION_ERROR",
    }.get(status, "INTERNAL_SERVER_ERROR")
    message = str(error.detail) if error.detail else "请求失败"
    return JSONResponse(status_code=status, content=error_content(code, message, status, request_id))


def unhandled_exception_response(request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_content("INTERNAL_SERVER_ERROR", "服务器内部错误", 500, request_id),
    )


def set_no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
