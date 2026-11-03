"""结构化日志：一行一个 JSON，带 request_id，方便 grep 和事后对账。

不记录 token、不记录完整响应体（可能有账号信息）。
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for key in ("job_id", "batch_id", "waybill", "status", "source", "path", "ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(JsonFormatter())
    root.addHandler(stream)

    if log_dir:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_dir / "scan.log", encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
