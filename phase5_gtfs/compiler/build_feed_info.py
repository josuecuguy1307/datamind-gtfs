"""Generate feed_info.txt for GTFS feed."""
from __future__ import annotations

import csv
import os
from datetime import date, timedelta
from typing import Any, Dict, Optional


def build_feed_info(
    output_dir: str,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Write feed_info.txt to *output_dir*.

    GTFS spec fields:
      feed_publisher_name (required)
      feed_publisher_url  (required)
      feed_lang           (required)
      default_lang        (optional)
      feed_start_date     (recommended)
      feed_end_date       (recommended)
      feed_version        (recommended)
      feed_contact_email  (optional)
      feed_contact_url    (optional)

    Returns the path to the written file.
    """
    config = config or {}
    today = date.today()

    row = {
        "feed_publisher_name": config.get("publisher_name", "ReiseData / DataMind"),
        "feed_publisher_url": config.get("publisher_url", "https://example.com"),
        "feed_lang": config.get("feed_lang", "es"),
        "default_lang": config.get("default_lang", "es"),
        "feed_start_date": config.get(
            "feed_start_date", today.strftime("%Y%m%d")
        ),
        "feed_end_date": config.get(
            "feed_end_date", (today + timedelta(days=90)).strftime("%Y%m%d")
        ),
        "feed_version": config.get("feed_version", today.strftime("%Y%m%d")),
        "feed_contact_email": config.get("contact_email", "data@example.com"),
        "feed_contact_url": config.get("contact_url", "https://example.com"),
    }

    path = os.path.join(output_dir, "feed_info.txt")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row.keys()))
        writer.writeheader()
        writer.writerow(row)

    return path
