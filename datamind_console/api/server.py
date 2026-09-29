
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from datamind_console.api_chatgpt.routes.ai_bot_api import router as ai_bot_api_router
from datamind_console.api_chatgpt.routes.copilot_api import router as copilot_api_router
from datamind_console.api.geo_api_router import router as geo_api_router
from datamind_console.api.gtfs_exports_router import router as gtfs_exports_router
from datamind_console.common.config import CFG


app = FastAPI(title="DataMind Geo API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CFG.cors_allow_origins) or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(geo_api_router)
app.include_router(gtfs_exports_router)
app.include_router(ai_bot_api_router)
app.include_router(copilot_api_router)


@app.get("/health")
def health():
    return {"ok": True, "service": "datamind_geo_api", "version": "Geo API v1"}
