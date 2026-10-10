"""The embedding dimension in settings matches every pgvector column in the SQL."""
import importlib
import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"


def test_default_embed_dim_matches_sql_vector_columns(monkeypatch) -> None:
    monkeypatch.delenv("EMBED_DIM", raising=False)
    import src.settings as settings

    settings = importlib.reload(settings)
    dims = {
        int(d)
        for f in SQL_DIR.glob("*.sql")
        for d in re.findall(r"\bvector\((\d+)\)", f.read_text(encoding="utf-8"))
    }
    assert dims, "no vector(N) column found in phase2_semantics/sql"
    assert dims == {settings.EMBED_DIM}
