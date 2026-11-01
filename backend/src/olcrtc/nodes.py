# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
#
# Multi-node Docker daemon registry for the OLCWave panel.
#
# The panel talks to its own Docker daemon through docker_client (the LOCAL
# node - it also hosts the panel API/db/Caddy). Additional execution nodes
# are reached through dedicated per-node daemons exposed to the panel ONLY
# as local Unix sockets (no remote TCP, no public Docker ports):
#
#   OLCWAVE_NODES_JSON = {
#     "vm1": {"docker_url": "unix:///run/olcwave-nodes/vm1.sock",
#             "tags": ["vm1"], "image": "olcrtc"},
#     "vm2": {...}, "vm3": {...}
#   }
#
# The "local" node is implicitly registered (docker_url=None -> aiodocker
# default resolution, same socket docker_client uses), so a build without
# OLCWAVE_NODES_JSON keeps the previous single-node behavior.
# OLCWAVE_LOCAL_NODE=false removes it entirely (panel hosts without a Docker
# daemon, e.g. Kubernetes): every container must then route to a configured
# remote node, and anything addressed to 'local' raises UnknownNodeError.
#
# Node selection for a NEW container is EXACT config_tag -> tags matching
# (each Jitsi profile runs on its one designated node; the tag is also the
# actual egress host name). Lookups of EXISTING containers route by
# node+name, or by unambiguous configured tag when the name is not fully
# qualified. There is deliberately NO fallback to the local node when a
# remote node fails: an outage must surface, not silently drain elsewhere.

import json
import logging

import aiodocker
import aiohttp

from config import settings

logger = logging.getLogger(__name__)

LOCAL_NODE = "local"

_clients: dict[str, aiodocker.Docker] = {}
_local_docker: aiodocker.Docker | None = None
_nodes: dict[str, dict] | None = None


class UnknownNodeError(KeyError):
    """Raised when a method is called with a node name that is not configured."""


def local_enabled() -> bool:
    """Whether the panel host's own Docker daemon is an execution node."""
    return settings.OLCWAVE_LOCAL_NODE


def _load_nodes() -> dict[str, dict]:
    """Parse OLCWAVE_NODES_JSON (cached until reset_cache()).

    RAISES on invalid JSON, a non-object payload, a node without
    docker_url, non-string tags, or a tag declared by more than one node:
    a malformed node config must never silently degrade to local-only
    operation (no fake fallback). Returns {} only when the env is
    empty/unset - that is the legitimate single-node mode, which in turn
    requires the local node.
    """
    global _nodes
    if _nodes is not None:
        return _nodes
    _nodes = {}
    raw = (settings.OLCWAVE_NODES_JSON or "").strip()
    if not raw:
        if not local_enabled():
            raise ValueError(
                "OLCWAVE_LOCAL_NODE=false requires a non-empty OLCWAVE_NODES_JSON"
            )
        return _nodes
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"OLCWAVE_NODES_JSON is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise ValueError(
            "OLCWAVE_NODES_JSON must be a non-empty JSON object "
            "{name: {docker_url, tags, image}}"
        )
    seen_tags: dict[str, str] = {}
    for name, spec in parsed.items():
        if not isinstance(spec, dict) or not spec.get("docker_url"):
            raise ValueError(f"OLCWAVE_NODES_JSON[{name}]: missing docker_url")
        tags = spec.get("tags") or []
        if not isinstance(tags, list) or not all(isinstance(t, str) and t for t in tags):
            raise ValueError(
                f"OLCWAVE_NODES_JSON[{name}]: tags must be a list of non-empty strings"
            )
        for t in tags:
            if t in seen_tags and seen_tags[t] != name:
                raise ValueError(
                    f"OLCWAVE_NODES_JSON: tag '{t}' is declared by both "
                    f"'{seen_tags[t]}' and '{name}' - each profile tag must "
                    "map to exactly one node"
                )
            seen_tags[t] = name
        _nodes[name] = {
            "docker_url": spec["docker_url"],
            "tags": list(tags),
            "image": spec.get("image") or "olcrtc",
        }
    return _nodes


def reset_cache() -> None:
    """Drop parsed-config and client caches (used by tests and re-init)."""
    global _nodes, _local_docker
    _nodes = None
    _local_docker = None
    _clients.clear()


def names() -> list[str]:
    """All known node names: 'local' first (if enabled), then configured."""
    configured = list(_load_nodes().keys())
    return [LOCAL_NODE] + configured if local_enabled() else configured


def tags_for(node: str) -> list[str]:
    """Tags configured for the node (exact strings; empty for 'local')."""
    if node == LOCAL_NODE:
        return []
    return list(_load_nodes().get(node, {}).get("tags", []))


def image_for(node: str) -> str:
    """Image used for new containers on the node (default 'olcrtc')."""
    return _load_nodes().get(node, {}).get("image", "olcrtc")


def node_for_tag(config_tag: str) -> str:
    """Exact configured-tag match to a node.

    Multi-node mode (env configured): every profile tag MUST be declared
    by exactly one node - an undeclared tag is a configuration error and
    raises (no silent drain to local). Single-node mode: always 'local'.
    """
    table = _load_nodes()
    for name, spec in table.items():
        if config_tag in spec["tags"]:
            return name
    if table:
        raise UnknownNodeError(
            f"config tag '{config_tag}' is not declared in OLCWAVE_NODES_JSON "
            f"(known tags: {sorted(t for s in table.values() for t in s['tags'])})"
        )
    return LOCAL_NODE


def resolve_node(node: str | None = None, config_tag: str | None = None) -> str:
    """Node for a NEW container: explicit node wins, else tag routing."""
    if node:
        if node not in names():
            raise UnknownNodeError(node)
        return node
    if config_tag:
        return node_for_tag(config_tag)
    if not local_enabled():
        raise UnknownNodeError("no config tag given and the local node is disabled")
    return LOCAL_NODE


# Every remote node socket is an SSH relay: each NEW connection is a fresh SSH
# handshake (`ssh ... docker system dial-stdio`). sshd throttles concurrent
# unauthenticated handshakes (MaxStartups, default 10:30:100), so an unbounded
# pool (aiohttp default 100) gets bursts of parallel calls reset during key
# exchange. A small pool of kept-alive connections reuses established SSH
# sessions and keeps the handshake rate far below that limit.
REMOTE_POOL_LIMIT = 4
REMOTE_KEEPALIVE_SECONDS = 120.0
UNIX_PREFIX = "unix://"


def _remote_client(docker_url: str) -> aiodocker.Docker:
    if not docker_url.startswith(UNIX_PREFIX):
        return aiodocker.Docker(url=docker_url)
    connector = aiohttp.UnixConnector(
        docker_url[len(UNIX_PREFIX):],
        limit=REMOTE_POOL_LIMIT,
        keepalive_timeout=REMOTE_KEEPALIVE_SECONDS,
    )
    # With a caller-supplied connector aiodocker uses `url` verbatim for
    # request URLs, so pass the dummy host it would use itself.
    return aiodocker.Docker(url=UNIX_PREFIX + "localhost", connector=connector)


def local_docker() -> aiodocker.Docker:
    """The LOCAL daemon client (the instance docker_client manages)."""
    global _local_docker
    if not local_enabled():
        raise UnknownNodeError(LOCAL_NODE)
    if _local_docker is None:
        import docker_client
        if docker_client.docker is not None:
            _local_docker = docker_client.docker
        else:
            _local_docker = aiodocker.Docker()
    return _local_docker


def client(node: str = LOCAL_NODE) -> aiodocker.Docker:
    """aiodocker client bound to the node's daemon Unix socket.

    Clients are created lazily and cached; they are all closed by
    close_all() on API shutdown (main lifespan), so sockets never leak.
    """
    if node == LOCAL_NODE:
        return local_docker()
    spec = _load_nodes().get(node)
    if spec is None:
        raise UnknownNodeError(node)
    if node not in _clients:
        logger.info("connecting to node daemon %s at %s", node, spec["docker_url"])
        _clients[node] = _remote_client(spec["docker_url"])
    return _clients[node]


def clients() -> dict[str, aiodocker.Docker]:
    """Client per node, local first (if enabled), all nodes included."""
    return {name: client(name) for name in names()}


async def close_all() -> None:
    """Close every node client (local one included) - API shutdown path."""
    global _local_docker
    for name, cl in _clients.items():
        try:
            await cl.close()
            # _remote_client passes its own connector: aiodocker does not
            # close a caller-owned one (closing twice is a no-op).
            await cl.connector.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("failed to close node client %s", name)
    _clients.clear()
    if _local_docker is not None:
        try:
            await _local_docker.close()
        except Exception:  # noqa: BLE001
            logger.exception("failed to close local docker client")
        _local_docker = None
