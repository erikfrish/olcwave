import json

import aiodocker
import yaml
from aiodocker import DockerError
from aiodocker.containers import DockerContainer

import docker_client
from olcrtc import nodes

# Bounded resources for EVERY container the panel creates on ANY node:
# 256 MiB RAM, 256 PIDs, bounded json-file log, stable restart policy.
# `unless-stopped` restores the container after daemon/node restart unless
# it was explicitly stopped - that keeps the config key stable across
# reboots of a VPS node.
CONTAINER_MEMORY_BYTES = 256 * 1024 * 1024
CONTAINER_PIDS_LIMIT = 256

_LOG_CONFIG = {
    "Type": "json-file",
    "Config": {"max-size": "10m", "max-file": "3"},
}
_RESTART_POLICY = {"Name": "unless-stopped"}


def host_config(extra_hosts: list[str] | None = None) -> dict:
    """HostConfig shared by run/restart on every node.

    `extra_hosts` carries the LOCAL routing gateway (host.docker.internal ->
    host-gateway) ONLY when the panel actually runs its own Xray routing -
    remote nodes have no such local gateway, so no special routing is added
    unless the caller explicitly passes it.
    """
    hc: dict = {
        "Memory": CONTAINER_MEMORY_BYTES,
        "PidsLimit": CONTAINER_PIDS_LIMIT,
        "LogConfig": dict(_LOG_CONFIG),
        "RestartPolicy": dict(_RESTART_POLICY),
    }
    if extra_hosts:
        hc["ExtraHosts"] = list(extra_hosts)
    return hc


def _client(node: str) -> aiodocker.Docker:
    """aiodocker client of the node; unknown node -> explicit error.

    There is NO fallback to the local node: a typo or a dead remote must
    surface as an error, not drain traffic to the panel host.
    """
    if node == nodes.LOCAL_NODE:
        return docker_client.docker or nodes.local_docker()
    return nodes.client(node)


def _node_of(node: str | None, config_tag: str | None = None) -> str:
    """Node for a new container: explicit arg wins over tag routing.

    node=None  -> route by EXACT config_tag (each profile one node);
    node given -> used verbatim ('local' pins the panel daemon, a remote
    name must be configured or UnknownNodeError is raised).
    """
    if node:
        return nodes.resolve_node(node=node)
    return nodes.resolve_node(config_tag=config_tag)


class OlcRTC:
    @staticmethod
    async def build(rebuild: bool = False, node: str = nodes.LOCAL_NODE):
        docker = _client(node)
        image = nodes.image_for(node)
        if not rebuild:
            try:
                _ = await docker.images.get(image)
                return
            except DockerError:
                pass

        # The image is PRELOADED on each node by the operator (immutable
        # alias supplied by the parent install). There is deliberately no
        # remote dynamic build fallback: a missing image on a node is an
        # operational error to fix on that node, not something the panel
        # rebuilds over a management socket.
        raise DockerError(
            404,
            f"image '{image}' not preloaded on node '{node}'; "
            "build/push it there before running containers",
        )

    @staticmethod
    async def run(
        config: str,
        config_tag: str,
        user_id: str,
        upstream_proxy_addr: str = "",
        upstream_proxy_user: str = "",
        upstream_proxy_pass: str = "",
        node: str | None = None,
    ):
        target = _node_of(node, config_tag)
        docker = _client(target)
        name = f"olcwave-{config_tag}-{user_id}"

        from olcrtc.service import Containers

        try:
            old = await docker.containers.get(name)
            info = await old.show()
            # Ownership guard before ANY mutation, including the
            # run-collision replacement: the squatting container must be
            # panel-labeled AND owned by the SAME user this run is for
            # (node + profile + owner all verified). An unmarked container
            # or one owned by a DIFFERENT user is never deleted - run
            # fails loudly instead of destroying someone else's container.
            parsed = Containers.parse_panel_labels(info)
            if parsed is None:
                raise DockerError(
                    409,
                    f"container '{name}' exists on node '{target}' "
                    "but has no olcwave.managed labels; refusing to replace",
                )
            existing_tag, existing_owner = parsed
            if existing_owner != user_id or existing_tag != config_tag:
                raise DockerError(
                    409,
                    f"container '{name}' on node '{target}' is owned by "
                    f"'{existing_owner}' (tag '{existing_tag}'); refusing to "
                    f"replace with owner '{user_id}' (tag '{config_tag}')",
                )
            await old.delete(force=True)
        except DockerError as exc:
            if exc.status != 404:
                raise

        extra_hosts = []
        if upstream_proxy_addr:
            # Local routing gateway only when routing is actually used;
            # on remote nodes this is only ever set by explicit restart args.
            extra_hosts.append("host.docker.internal:host-gateway")

        container = await docker.containers.create(
            config={
                "Image": nodes.image_for(target),
                "Labels": Containers.panel_labels(config_tag, user_id),
                "Env": [
                    f"CONFIG={config}",
                    f"UPSTREAM_SOCKS={upstream_proxy_addr}",
                    f"UPSTREAM_USER={upstream_proxy_user}",
                    f"UPSTREAM_PASS={upstream_proxy_pass}",
                ],
                "HostConfig": host_config(extra_hosts),
            },
            name=name,
        )

        await container.start()

        return container

    @staticmethod
    async def start(name: str, node: str = nodes.LOCAL_NODE):
        docker = _client(node)
        container = await docker.containers.get(name)
        await OlcRTC._require_owned(container, name, node)
        await container.start()

    @staticmethod
    async def stop(name: str, node: str = nodes.LOCAL_NODE):
        docker = _client(node)
        container = await docker.containers.get(name)
        await OlcRTC._require_owned(container, name, node)
        await container.stop()

    @staticmethod
    async def _require_owned(container: DockerContainer, name: str, node: str) -> None:
        """Label-ownership check BEFORE any mutation on any node.

        A container without the exact management labels (including a name
        collision with a foreign container) is never touched by panel
        mutations. This runs on every node with the same label semantics.
        """
        from olcrtc.service import Containers

        info = await container.show()
        if Containers.parse_panel_labels(info) is None:
            raise DockerError(
                409,
                f"container '{name}' on node '{node}' has no "
                "olcwave.managed labels; refusing to mutate",
            )

    @staticmethod
    async def restart(
        name: str,
        upstream_proxy_addr: str = "",
        upstream_proxy_user: str = "",
        upstream_proxy_pass: str = "",
        node: str = nodes.LOCAL_NODE,
    ):
        docker = _client(node)
        container = await docker.containers.get(name)

        info = await container.show()
        await OlcRTC._require_owned(container, name, node)

        image = info["Config"]["Image"]

        env = {}
        for item in info["Config"].get("Env", []):
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v

        env["UPSTREAM_SOCKS"] = upstream_proxy_addr
        env["UPSTREAM_USER"] = upstream_proxy_user
        env["UPSTREAM_PASS"] = upstream_proxy_pass

        if "CONFIG" in env:
            try:
                cfg = yaml.safe_load(env["CONFIG"])
                if isinstance(cfg, dict):
                    cfg.pop("data", None)
                    env["CONFIG"] = yaml.dump(cfg)
            except yaml.YAMLError:
                pass

        state = info["State"]["Status"]

        if state == "running":
            await container.delete(force=True)

        new_container = await docker.containers.create(
            config={
                "Image": image,
                # Preserve the panel management labels across restarts.
                "Labels": info["Config"].get("Labels") or {},
                "Env": [f"{k}={v}" for k, v in env.items()],
                "HostConfig": host_config(
                    ["host.docker.internal:host-gateway"]
                    if upstream_proxy_addr else None
                ),
            },
            name=name,
        )

        await new_container.start()

    @staticmethod
    async def remove(name: str, node: str = nodes.LOCAL_NODE):
        docker = _client(node)
        container = await docker.containers.get(name)
        await OlcRTC._require_owned(container, name, node)
        await container.delete(force=True)

    @staticmethod
    async def logs(name: str, node: str = nodes.LOCAL_NODE) -> str:
        docker = _client(node)
        container = await docker.containers.get(name)
        logs = await container.log(stdout=True, stderr=True)
        return "".join(logs)

    @staticmethod
    async def get(name: str, node: str = nodes.LOCAL_NODE) -> DockerContainer:
        docker = _client(node)
        return await docker.containers.get(name)

    @staticmethod
    async def all(include_stopped: bool = False,
                  node: str = nodes.LOCAL_NODE) -> list[DockerContainer]:
        docker = _client(node)
        return await docker.containers.list(all=include_stopped)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]

    @staticmethod
    async def get_config(name: str, node: str = nodes.LOCAL_NODE):
        docker = _client(node)
        container = await docker.containers.get(name)

        exec_ = await container.exec(cmd=["cat", "/tmp/olcwave/config.yaml"])
        stream = exec_.start(detach=False)
        config = await stream.read_out()
        return config.data.decode().strip()

    @staticmethod
    async def get_stats(name: str, node: str = nodes.LOCAL_NODE) -> dict:  # pyright: ignore[reportUnknownParameterType]
        docker = _client(node)
        container = await docker.containers.get(name)

        exec_ = await container.exec(
            cmd=["cat", "/tmp/olcwave/stats.json"]
        )
        stream = exec_.start(detach=False)
        result = await stream.read_out()

        raw = result.data.decode().strip()

        if not raw:
            return {}

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}

        return data if isinstance(data, dict) else {}
