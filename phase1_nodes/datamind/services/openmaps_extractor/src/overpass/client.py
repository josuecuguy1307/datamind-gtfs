import time
import requests
from phase1_nodes.datamind.services.openmaps_extractor.src.settings import (
    OVERPASS_URL,
)

import os
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "180"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "6"))



def run_overpass(query_text: str) -> tuple[dict, int, int, int]:
    """
    returns: (json, http_status, response_bytes, runtime_ms)
    """
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        t0 = time.time()
        try:
            r = requests.post(
                OVERPASS_URL,
                data=query_text.encode("utf-8"),
                headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
                timeout=REQUEST_TIMEOUT,
            )
            runtime_ms = int((time.time() - t0) * 1000)
            response_bytes = len(r.content)
            r.raise_for_status()
            return (r.json(), r.status_code, response_bytes, runtime_ms)
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(1.5 * attempt)
            else:
                raise RuntimeError(f"Overpass failed after {MAX_RETRIES} retries: {last_err}") from last_err
