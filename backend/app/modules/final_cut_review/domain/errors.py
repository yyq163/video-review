from __future__ import annotations


class ReviewError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def not_found(message: str = "资源不存在") -> ReviewError:
    return ReviewError("RESOURCE_NOT_FOUND", message)


def state_conflict(message: str = "当前状态不允许执行此操作") -> ReviewError:
    return ReviewError("RESOURCE_STATE_CONFLICT", message)
