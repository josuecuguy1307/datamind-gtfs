# phase4_semantics/ingest/docs/upload.py

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Optional OpenAI import (only needed if --openai is used)
try:
    from openai import OpenAI  # pip install openai
except Exception:
    OpenAI = None  # type: ignore


# ----------------------------
# Config (folders)
# ----------------------------

DEFAULT_DOCS_ROOT = Path(os.environ.get("PHASE4_DOCS_ROOT", "./phase4_docs")).resolve()
RAW_DIRNAME = "raw"
META_DIRNAME = "meta"


# ----------------------------
# Helpers
# ----------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext in (".xlsx", ".xls"):
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if ext == ".csv":
        return "text/csv"
    if ext in (".json",):
        return "application/json"
    return "application/octet-stream"


def _safe_filename(name: str) -> str:
    # keep it simple but safe
    name = name.strip().replace(" ", "_")
    bad = ['"', "'", "\\", "/", ":", "*", "?", "<", ">", "|"]
    for b in bad:
        name = name.replace(b, "_")
    return name


def _ensure_dirs(root: Path) -> Tuple[Path, Path]:
    raw_dir = root / RAW_DIRNAME
    meta_dir = root / META_DIRNAME
    raw_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    return raw_dir, meta_dir


# ----------------------------
# Data model
# ----------------------------

@dataclass
class UploadedDoc:
    doc_id: str
    stored_path: str
    meta_path: str
    sha256: str
    mime_type: str
    openai_file_id: Optional[str] = None


# ----------------------------
# Core upload
# ----------------------------

def upload_document(
    input_path: str,
    docs_root: Path = DEFAULT_DOCS_ROOT,
    title: Optional[str] = None,
    source_url: Optional[str] = None,
    notes: Optional[str] = None,
    upload_to_openai: bool = False,
    openai_purpose: str = "user_data",
) -> UploadedDoc:
    """
    1) Copy input PDF/Excel into phase4_docs/raw/
    2) Generate a doc_id (UUID)
    3) Write phase4_docs/meta/<doc_id>.json
    4) Optionally upload file to OpenAI Files API and store file_id
    """
    src = Path(input_path).expanduser().resolve()
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(f"File not found: {src}")

    raw_dir, meta_dir = _ensure_dirs(docs_root)

    doc_id = str(uuid.uuid4())
    original_name = src.name
    safe_name = _safe_filename(original_name)

    stored_filename = f"{doc_id}__{safe_name}"
    stored_path = raw_dir / stored_filename

    # Copy original bytes to our raw store
    shutil.copy2(src, stored_path)

    sha256 = _sha256_file(stored_path)
    mime_type = _guess_mime(stored_path)

    meta: Dict[str, Any] = {
        "doc_id": doc_id,
        "created_at": _utc_now_iso(),
        "title": title or "",
        "source_type": "docs_upload",
        "source_url": source_url or "",
        "original_filename": original_name,
        "stored_path": str(stored_path),
        "sha256": sha256,
        "mime_type": mime_type,
        "notes": notes or "",
        "openai": {
            "uploaded": False,
            "purpose": openai_purpose,
            "file_id": None,
        },
    }

    openai_file_id: Optional[str] = None
    if upload_to_openai:
        if OpenAI is None:
            raise RuntimeError(
                "OpenAI SDK not available. Install with: pip install openai"
            )

        # Upload file for later usage as Responses input_file
        # (recommended purpose is user_data)  :contentReference[oaicite:1]{index=1}
        client = OpenAI()

        with stored_path.open("rb") as f:
            uploaded = client.files.create(
                file=f,
                purpose=openai_purpose,
            )

        openai_file_id = getattr(uploaded, "id", None) or uploaded.get("id")  # type: ignore
        meta["openai"]["uploaded"] = True
        meta["openai"]["file_id"] = openai_file_id

    meta_path = meta_dir / f"{doc_id}.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    return UploadedDoc(
        doc_id=doc_id,
        stored_path=str(stored_path),
        meta_path=str(meta_path),
        sha256=sha256,
        mime_type=mime_type,
        openai_file_id=openai_file_id,
    )


# ----------------------------
# CLI
# ----------------------------

def main():
    p = argparse.ArgumentParser(
        description="Phase 4 Docs Upload: store PDF/Excel + optional OpenAI file upload"
    )
    p.add_argument("path", help="Path to PDF/Excel/CSV")
    p.add_argument("--root", default=str(DEFAULT_DOCS_ROOT), help="Docs root folder")
    p.add_argument("--title", default=None, help="Optional document title")
    p.add_argument("--source-url", default=None, help="Optional source URL (ArcGIS/web)")
    p.add_argument("--notes", default=None, help="Optional notes")

    p.add_argument(
        "--openai",
        action="store_true",
        help="Upload to OpenAI Files API (requires OPENAI_API_KEY)",
    )
    p.add_argument(
        "--purpose",
        default="user_data",
        help='OpenAI Files API purpose (recommended: "user_data")',
    )

    args = p.parse_args()

    out = upload_document(
        input_path=args.path,
        docs_root=Path(args.root).expanduser().resolve(),
        title=args.title,
        source_url=args.source_url,
        notes=args.notes,
        upload_to_openai=args.openai,
        openai_purpose=args.purpose,
    )

    print("\n✅ Upload complete")
    print(f"doc_id        = {out.doc_id}")
    print(f"stored_path   = {out.stored_path}")
    print(f"meta_path     = {out.meta_path}")
    print(f"sha256        = {out.sha256}")
    print(f"mime_type     = {out.mime_type}")
    print(f"openai_file_id= {out.openai_file_id or ''}")
    print("")


if __name__ == "__main__":
    main()
