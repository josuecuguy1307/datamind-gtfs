from __future__ import annotations

import uuid


def new_uuid() -> str:
    return str(uuid.uuid4())
