# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.

import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from aiodocker import DockerError
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import uvicorn

from auth.router import router as auth_router
from profiles.router import router as configs_router
from users.router import router as users_router
from subscriptions.router import router as subscriptions_router
from olcrtc.router import router as containers_router
from routing.router import router as routing_router
from routing.service import Routing
from settings.router import router as settings_router
from settings.service import SettingsService
from rw.reconcile import ReconcileManager
from xraycore.sdk import XrayCore
from config import settings
from database import create_tables
from traffic import TrafficManager
from rw_sync import SyncManager
from docker_client import init_docker, close_docker


async def lifespan(app: FastAPI):
    await create_tables()
    await init_docker()
    # Parse OLCWAVE_NODES_JSON fail-fast: invalid JSON, a non-object
    # payload, a node without docker_url or duplicate tags abort startup -
    # a malformed node config must never silently degrade to local-only.
    # Empty env = single-node mode. Then pre-open per-node daemon clients
    # (local Unix sockets only, no remote TCP).
    from olcrtc import nodes
    nodes.reset_cache()
    for node_name in nodes.names():
        nodes.client(node_name)
    await SettingsService.load()

    try:
        routing = await Routing.get()
    except HTTPException:
        routing = False
    if routing:
        await XrayCore.run(routing)

    if settings.RW_ENABLED:
        # startup reconciliation: evaluate restored containers before serving
        try:
            summary = await ReconcileManager.reconcile_once()
            if summary["checked"]:
                logging.getLogger(__name__).info("startup reconciliation: %s", summary)
        except Exception:
            logging.getLogger(__name__).exception("startup reconciliation failed")
        SyncManager.start()
        ReconcileManager.start()

    traffic_task = asyncio.create_task(TrafficManager.run())
    yield

    traffic_task.cancel()
    try:
        await traffic_task
    except asyncio.CancelledError:
        pass

    if settings.RW_ENABLED:
        await ReconcileManager.stop()
        await SyncManager.stop()
    try:
        await XrayCore.stop()
    except DockerError:
        pass
    await close_docker()


app = FastAPI(lifespan=lifespan, openapi_url="", docs_url="", redoc_url="")  # pyright: ignore[reportArgumentType]

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(configs_router)
app.include_router(users_router)
app.include_router(subscriptions_router)
app.include_router(containers_router)
app.include_router(settings_router)
app.include_router(routing_router)


@app.get("/health")
async def healthcheck():
    return "ok"


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0")
