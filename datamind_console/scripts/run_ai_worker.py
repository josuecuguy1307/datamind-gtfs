from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from datamind_console.ai_agent.worker import main


if __name__ == "__main__":
    main()
