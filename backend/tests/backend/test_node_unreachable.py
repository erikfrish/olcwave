"""A node that cannot be listed must never make its containers look missing.

Regression: a transient SSH relay reset on one node hid that node's
containers from the subscription request, which then "recreated" them with
fresh rooms/keys and broke every client still holding the old config.
"""
import json

import pytest
import yaml

from test_rw_authorization import (
    allow, fake_access, fake_docker, no_db, ok_traffic, panel_env,
)

NODES = {
    "vm1": {"docker_url": "unix:///run/olcwave-nodes/vm1.sock", "tags": ["vm1"]},
    "vm3": {"docker_url": "unix:///run/olcwave-nodes/vm3.sock", "tags": ["vm3"]},
}


@pytest.fixture
def two_nodes(monkeypatch, fake_docker):
    from config import settings
    from olcrtc import nodes
    from olcrtc import sdk as olcrtc_sdk

    monkeypatch.setattr(settings, "OLCWAVE_LOCAL_NODE", False)
    monkeypatch.setattr(settings, "OLCWAVE_NODES_JSON", json.dumps(NODES))
    nodes.reset_cache()
    down: set[str] = set()

    async def all_containers(include_stopped=False, node="local"):
        if node in down:
            raise ConnectionResetError("Connection reset by peer")
        return [
            c for c in fake_docker.containers
            if c._info["Config"]["Labels"].get("olcwave.config_tag")
            in NODES[node]["tags"]
        ]

    monkeypatch.setattr(olcrtc_sdk.OlcRTC, "all", staticmethod(all_containers))
    yield down
    nodes.reset_cache()


@pytest.mark.asyncio
async def test_unreachable_node_is_not_reprovisioned(
    two_nodes, fake_access, fake_docker, no_db, panel_env,
):
    from profiles.schemas import ProfileSchema
    from subscriptions.service import Subscriptions

    for tag in ("vm1", "vm3"):
        panel_env.profiles.append(ProfileSchema(
            name=tag, tag=tag, profile=yaml.safe_dump({
                "auth": {"provider": "jitsi"},
                "room": {"id": f"https://meet.example.org/{tag}"},
                "net": {"transport": "datachannel"},
            }),
        ))
    user = "alice"
    panel_env.traffic[user] = ok_traffic(user)
    fake_access.decision = allow(user)

    first = await Subscriptions.get(user)
    assert first.status_code == 200
    issued = dict(panel_env.configs)
    assert len(fake_docker.containers) == 2

    two_nodes.add("vm3")
    flapped = await Subscriptions.get(user)
    assert flapped.status_code == 503, "partial view must not be served as complete"
    assert len(fake_docker.containers) == 2, "hidden vm3 container was recreated"
    assert not any(c.removed for c in fake_docker.containers)
    assert panel_env.configs == issued

    two_nodes.clear()
    recovered = await Subscriptions.get(user)
    assert recovered.status_code == 200
    assert recovered.body == first.body, "keys must survive the node flap"
