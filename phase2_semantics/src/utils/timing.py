from contextlib import contextmanager
import time
import logging

logger = logging.getLogger(__name__)

@contextmanager
def timed(label: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        logger.info(
            "timing",
            extra={"label": label, "elapsed_ms": round(elapsed * 1000, 2)}
        )
