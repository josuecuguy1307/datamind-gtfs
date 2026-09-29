from __future__ import annotations

import logging
import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator, Optional


def get_logger(name: str = "openmaps_extractor") -> logging.Logger:
    """
    Usage:
      log = get_logger(__name__)
      log.info("message")
    """
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv(
        "LOG_FORMAT",
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    logger = logging.getLogger(name)

    # Prevent duplicate handlers if called multiple times
    if not logger.handlers:
        logger.setLevel(level)
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(fmt))
        logger.addHandler(handler)
        logger.propagate = False

    # Keep level in sync even if env var changes
    logger.setLevel(level)
    return logger


@contextmanager
def log_stage(logger: logging.Logger, stage: str, extra: Optional[str] = None) -> Iterator[None]:
    """
    Wrap a pipeline stage to auto-log start/end + duration.

    Example:
      with log_stage(log, "extract", f"action={action_id}"):
          ...
    """
    msg = f"START {stage}" + (f" | {extra}" if extra else "")
    logger.info(msg)
    t0 = time.time()
    try:
        yield
        dt_ms = int((time.time() - t0) * 1000)
        logger.info(f"END   {stage} | {dt_ms}ms")
    except Exception as e:
        dt_ms = int((time.time() - t0) * 1000)
        logger.exception(f"FAIL  {stage} | {dt_ms}ms | {type(e).__name__}: {e}")
        raise
