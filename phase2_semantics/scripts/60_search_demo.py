"""
Phase 2 – Semantic Geocoder
Step 60: Semantic search demo using PostgreSQL pgvector.

Usage:
  python scripts/60_search_demo.py "terminal quitumbe"
"""

from __future__ import annotations

import sys
from typing import Any, Dict, List

from src.pipeline.search.semantic_search import semantic_search
from src.settings import SEARCH_TOP_K
from src.utils.jsonlog import get_logger


logger = get_logger("phase2.search_demo")


def print_results(hits: List[Dict[str, Any]]) -> None:
    if not hits:
        print("No results.")
        return

    print("\nResults:\n" + "-" * 60)
    for i, h in enumerate(hits, start=1):
        p = h.get("payload") or {}
        print(
            f"{i:>2}. score={float(h.get('score') or 0.0):.4f} | "
            f"alias='{p.get('alias_text')}' | "
            f"place_id={p.get('place_id')} | "
            f"lang={p.get('lang')} | "
            f"kind={p.get('kind')} | "
            f"sem={float(p.get('semantic_score') or 0.0):.3f} "
            f"trgm={float(p.get('trgm_score') or 0.0):.3f} "
            f"fts={float(p.get('fts_score') or 0.0):.3f}"
        )


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: python scripts/60_search_demo.py "your query here"')
        sys.exit(1)

    query_text = sys.argv[1]
    logger.info("Running search demo", extra={"query": query_text, "backend": "pgvector"})
    hits = semantic_search(query_text, k=int(SEARCH_TOP_K))
    print_results(hits)


if __name__ == "__main__":
    main()
