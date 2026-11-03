"""类型化错误：服务层只抛这些，HTTP 层统一翻译成规范 JSON。

客户端永远看不到堆栈、SQL、文件路径。
"""

from __future__ import annotations


class AppError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        # detail 只进日志，不进响应
        self.detail = detail


class ValidationError(AppError):
    code = "invalid_request"
    http_status = 400


class UnauthorizedError(AppError):
    code = "unauthorized"
    http_status = 401


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404


class ConflictError(AppError):
    code = "conflict"
    http_status = 409


class NoActiveBatchError(ConflictError):
    code = "no_active_batch"

    def __init__(self) -> None:
        super().__init__("没有正在进行的批次。先在界面上点『开始本批』，再让手机拉任务。")


class BatchLimitError(ConflictError):
    code = "batch_limit_exceeded"


class InternalError(AppError):
    code = "internal_error"
    http_status = 500
