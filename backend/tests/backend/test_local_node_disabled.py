# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
"""OLCWAVE_LOCAL_NODE=false: the panel host has no Docker daemon.

Pinned behavior:
- the node registry exposes only configured remote nodes
- nothing can route to or open a client for 'local' (no fallback drain)
- the mode is rejected without OLCWAVE_NODES_JSON (no node at all)
- no local aiodocker client is ever created
- panel Xray routing reports not-running and refuses mutations with 409

Run:
    cd backend && uv run pytest tests/backend/test_local_node_disabled.py -q
"""

import json

import aiodocker
import pytest
from fastapi import HTTPException

import docker_client
from config import settings
from olcrtc import nodes
from xraycore.sdk import XrayCore

NODES = {
    "vm1": {"docker_url": "unix:///run/olcwave-nodes/vm1.sock", "tags": ["vm1"]},
    "vm3": {"docker_url": "unix:///run/olcwave-nodes/vm3.sock", "tags": ["vm3"]},
}


@pytest.fixture
def remote_only(monkeypatch):
    monkeypatch.setattr(settings, "OLCWAVE_LOCAL_NODE", False)
    monkeypatch.setattr(settings, "OLCWAVE_NODES_JSON", json.dumps(NODES))

    def forbidden(*args, **kwargs):
        if not kwargs.get("url"):
            raise AssertionError("local docker client must not be created")
        return object()

    monkeypatch.setattr(aiodocker, "Docker", forbidden)
    monkeypatch.setattr(docker_client, "docker", None)
    nodes.reset_cache()
    yield
    nodes.reset_cache()


def test_registry_lists_only_remote_nodes(remote_only):
    assert nodes.names() == ["vm1", "vm3"]
    assert set(nodes.clients()) == {"vm1", "vm3"}


def test_local_is_never_routable(remote_only):
    with pytest.raises(nodes.UnknownNodeError):
        nodes.resolve_node(node=nodes.LOCAL_NODE)
    with pytest.raises(nodes.UnknownNodeError):
        nodes.resolve_node()
    with pytest.raises(nodes.UnknownNodeError):
        nodes.node_for_tag("undeclared")
    with pytest.raises(nodes.UnknownNodeError):
        nodes.client(nodes.LOCAL_NODE)
    assert nodes.resolve_node(config_tag="vm3") == "vm3"


def test_requires_configured_nodes(remote_only, monkeypatch):
    monkeypatch.setattr(settings, "OLCWAVE_NODES_JSON", "")
    nodes.reset_cache()
    with pytest.raises(ValueError, match="OLCWAVE_NODES_JSON"):
        nodes.names()


async def test_init_and_close_skip_local_daemon(remote_only):
    await docker_client.init_docker()
    assert docker_client.docker is None
    await docker_client.close_docker()


async def test_routing_unavailable(remote_only):
    assert await XrayCore.is_running() is False
    with pytest.raises(HTTPException) as exc:
        XrayCore.ensure_available()
    assert exc.value.status_code == 409
