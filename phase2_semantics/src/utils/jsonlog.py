from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional


# ============================================================
# JSON formatter
# ============================================================

class JsonFormatter(logging.Formatter):
    """
    Minimal JSON log formatter.
    Safe for pipelines, CLI, and later ingestion into OpenSearch.
    """

    RESERVED = {
        "name", "msg", "args", "levelname", "levelno",
        "pathname", "filename", "module", "exc_info",
        "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread",
        "threadName", "processName", "process",
    }

    def format(self, record: logging.LogRecord) -> str:
        log: Dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Capture user-provided extras safely
        for key, value in record.__dict__.items():
            if key not in self.RESERVED and key not in log:
                log[key] = value

        return json.dumps(log, ensure_ascii=False)


# ============================================================
# Logger factory (NEVER returns None)
# ============================================================

def get_logger(name: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    """
    Always returns a usable logger.

    - Idempotent (won't add duplicate handlers)
    - JSON formatted
    - Safe for multiprocessing / reruns
    """

    logger = logging.getLogger(name or "app")

    if logger.handlers:
        return logger

    logger.setLevel(level)

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    logger.addHandler(handler)
    logger.propagate = False

    return logger
