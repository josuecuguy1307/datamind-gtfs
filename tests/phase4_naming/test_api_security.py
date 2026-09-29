"""Seguridad de la API de fase 4: CORS local y X-API-Key en los endpoints que escriben."""
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER = Path(__file__).resolve().parents[2] / "phase4_naming/phase4_semantics/api/server.py"

WRITE_ENDPOINTS = [
    ("/ranker/train", {}),
    ("/ranker/infer/x", {}),
    ("/seed/overpass/x", {}),
    ("/evidence/create", {"json": {}}),
    ("/ranker/label", {"json": {}}),
]


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("PHASE4_API_KEY", "clave-de-prueba")
    monkeypatch.delenv("PHASE4_CORS_ORIGINS", raising=False)
    spec = importlib.util.spec_from_file_location("phase4_server_under_test", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return TestClient(mod.app, raise_server_exceptions=False)


@pytest.mark.parametrize("path,kw", WRITE_ENDPOINTS)
def test_post_sin_clave_es_401(client, path, kw):
    assert client.post(path, **kw).status_code == 401


@pytest.mark.parametrize("path,kw", WRITE_ENDPOINTS)
def test_post_con_clave_mala_es_401(client, path, kw):
    assert client.post(path, headers={"X-API-Key": "otra"}, **kw).status_code == 401


@pytest.mark.parametrize("path,kw", WRITE_ENDPOINTS)
def test_post_con_clave_buena_pasa_la_autenticacion(client, path, kw):
    # puede fallar después (validación / sin BD), pero jamás por auth
    r = client.post(path, headers={"X-API-Key": "clave-de-prueba"}, **kw)
    assert r.status_code not in (401, 503)


def test_falla_cerrado_sin_clave_configurada(client, monkeypatch):
    monkeypatch.delenv("PHASE4_API_KEY")
    r = client.post("/ranker/train", headers={"X-API-Key": "clave-de-prueba"})
    assert r.status_code == 503


def test_cors_no_refleja_origenes_ajenos(client):
    pre = lambda origin: client.options(
        "/ranker/train",
        headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
    )
    assert pre("https://evil.example").headers.get("access-control-allow-origin") is None
    assert pre("http://localhost:8501").headers.get("access-control-allow-origin") == "http://localhost:8501"
    assert "access-control-allow-credentials" not in pre("http://localhost:8501").headers
