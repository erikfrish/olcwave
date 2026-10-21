import asyncio
from datetime import datetime

from aiodocker.containers import DockerContainer

from xraycore.sdk import XrayCore
from olcrtc import nodes
from olcrtc.sdk import OlcRTC
from olcrtc.schemas import (
    ContainerConfigSchema,
    ContainerLogsSchema,
    ContainerSchema,
    ContainersListSchema,
    ContainerStatsSchema,
    NodeStatusSchema,
)


class LocatedContainer:
    """A panel container handle carrying its source node.

    Returned by Containers.locate()/panel_containers() so every follow-up
    call (remove/config/logs/stop) executes on the SAME node the container
    was found on - no name re-resolution, no local fallback.
    """

    __slots__ = ("node", "name", "container")

    def __init__(self, node: str, name: str, container=None):
        self.node = node
        self.name = name
        self.container = container  # DockerContainer bound to that node's client

    async def show(self) -> dict:
        """Inspect on the handle's OWN node (passthrough to aiodocker)."""
        if self.container is None:
            raise RuntimeError(
                f"handle {self!r} has no container bound (node unreachable at collect time)"
            )
        return await self.container.show()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"LocatedContainer(node={self.node!r}, name={self.name!r})"
async def _collect_node_containers(node: str):
    """List one node's containers, capturing (not raising) daemon errors.

    Returns (node, containers_or_[], error_or_None): a dead daemon yields
    its own error entry and never poisons the other nodes' gather.
    """
    from olcrtc.sdk import OlcRTC
    try:
        containers = await OlcRTC.all(include_stopped=True, node=node)
        return node, containers, None
    except Exception as exc:  # noqa: BLE001 - per-node isolation by design
        return node, [], str(exc) or type(exc).__name__


class Containers:
    # Management labels written on every container this panel creates.
    # Ownership is decided by these labels, NOT by the container name: a
    # foreign container that merely shares the `olcwave-` name prefix (ops
    # tooling, an isolated postgres, ...) never matches and is never touched.
    MANAGED_LABEL = "olcwave.managed"
    TAG_LABEL = "olcwave.config_tag"
    OWNER_LABEL = "olcwave.owner"

    @staticmethod
    def panel_labels(config_tag: str, short_uuid: str) -> dict[str, str]:
        return {
            Containers.MANAGED_LABEL: "true",
            Containers.TAG_LABEL: config_tag,
            Containers.OWNER_LABEL: short_uuid,
        }

    @staticmethod
    def parse_panel_labels(info: dict) -> tuple[str, str] | None:
        """(config_tag, owner) from labels, or None for unmarked containers.

        The owner is whatever Remnawave issued (any non-empty string, may
        contain '-' or '_'; no length or alphabet assumption). The canonical
        container name is `olcwave-<tag>-<owner>` VERBATIM - never re-derived
        by splitting the name, which is ambiguous when the owner itself
        contains dashes.
        """
        labels = info.get("Config", {}).get("Labels") or {}
        if str(labels.get(Containers.MANAGED_LABEL, "")).lower() != "true":
            return None
        tag = labels.get(Containers.TAG_LABEL) or ""
        owner = labels.get(Containers.OWNER_LABEL) or ""
        if not tag or not owner:
            return None
        return tag, owner

    @staticmethod
    def created_epoch(info: dict) -> float:
        """Docker inspect Created (ISO-8601) as a wall-clock epoch."""
        raw = info.get("Created")
        if raw is None:
            return 0.0
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            return datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return 0.0

    @staticmethod
    async def is_panel_container(cont: DockerContainer) -> bool:
        info = await cont.show()
        return Containers.parse_panel_labels(info) is not None

    @staticmethod
    async def to_schema(cont: DockerContainer, node: str = nodes.LOCAL_NODE) -> ContainerSchema | None:
        info = await cont.show()

        parsed = Containers.parse_panel_labels(info)
        if parsed is None:
            return None

        config_tag, user_id = parsed

        return ContainerSchema(
            id=info["Id"][:12],
            full_id=info["Id"],
            node=node,
            name=info["Name"].lstrip("/"),
            short_uuid=user_id,
            config_tag=config_tag,
            status=info["State"]["Status"],
            created=info["Created"],
            created_epoch=Containers.created_epoch(info),
            image=info["Config"]["Image"],
        )

    @staticmethod
    async def _collect_node(node: str, include_stopped: bool) -> tuple[list[ContainerSchema], NodeStatusSchema]:
        """List one node; failures are REPORTED, never swallowed as empty.

        A dead daemon yields ok=False with the error text - the caller must
        not mistake an outage for 'no containers on this node'.
        """
        try:
            containers = await OlcRTC.all(include_stopped=include_stopped, node=node)
        except Exception as exc:  # noqa: BLE001 - any daemon failure is a node status
            return [], NodeStatusSchema(node=node, ok=False, error=str(exc) or type(exc).__name__)
        schemas = await asyncio.gather(
            *(Containers.to_schema(c, node=node) for c in containers)
        )
        return (
            [s for s in schemas if s is not None],
            NodeStatusSchema(node=node, ok=True, containers=len(schemas)),
        )

    @staticmethod
    async def all_with_status(include_stopped: bool = True) -> ContainersListSchema:
        """Combined listing over local + all configured nodes.

        Every configured node is always probed (even if it has no
        containers), so the GUI can show per-node health explicitly.
        """
        node_names = nodes.names()
        results = await asyncio.gather(
            *(Containers._collect_node(n, include_stopped) for n in node_names)
        )
        containers: list[ContainerSchema] = []
        statuses: list[NodeStatusSchema] = []
        for schemas, status in results:
            containers.extend(schemas)
            statuses.append(status)
        return ContainersListSchema(nodes=statuses, containers=containers)

    @staticmethod
    async def all() -> list[ContainerSchema]:
        """Flat combined list across nodes (legacy shape preserved).

        On a node outage the error PROPAGATES (fail loud) - callers of the
        plain list must not run mutation/enforcement logic against a
        partial view; the GUI uses all_with_status() instead.
        """
        listing = await Containers.all_with_status()
        failed = [s for s in listing.nodes if not s.ok]
        if failed:
            raise RuntimeError(
                "node(s) unreachable: "
                + ", ".join(f"{s.node}: {s.error}" for s in failed)
            )
        return listing.containers

    @staticmethod
    def _configured_tags() -> dict[str, set[str]]:
        """node -> set of tags configured for it (exact strings)."""
        table: dict[str, set[str]] = {nodes.LOCAL_NODE: set()}
        # local node tags are not configured via env; its containers are
        # resolved by name directly (historical behavior).
        for name in nodes.names():
            if name == nodes.LOCAL_NODE:
                continue
            table[name] = set(nodes.tags_for(name))
        return table

    @staticmethod
    def _resolve(name_or_tag: str, node: str | None) -> tuple[str, str]:
        """(node, container-name) from 'node:name', bare name, or node arg.

        Explicit 'node:name' (colon form) and explicit node argument win;
        otherwise the name is resolved by _locate (exact configured-tag
        scan), falling back to 'local' like the single-node era.
        """
        if node:
            return node, name_or_tag
        if ":" in name_or_tag:
            n, _, nm = name_or_tag.partition(":")
            if n in nodes.names():
                return n, nm
            raise RuntimeError(
                f"unknown node '{n}' in '{name_or_tag}'; "
                f"configured nodes: {nodes.names()}"
            )
        return nodes.LOCAL_NODE, name_or_tag

    @staticmethod
    async def _locate(name_or_tag: str, node: str | None = None) -> tuple[str, str]:
        """Resolve (node, name) for an EXISTING container.

        Bare `olcwave-<tag>-<owner>` names are routed by EXACT tag matching
        against the configured tags table: the owner may contain dashes, so
        every dash position is tried as a tag/owner split and only tags
        that EXACTLY match a configured tag route to that node. A tag that
        matches several nodes is ambiguous -> explicit 'node:name' is
        required; no match -> 'local' (single-node and legacy names).
        """
        if node or ":" in name_or_tag:
            return Containers._resolve(name_or_tag, node)

        nm = name_or_tag
        table = Containers._configured_tags()
        candidates: set[str] = set()
        if nm.startswith("olcwave-"):
            middle = nm[len("olcwave-"):]
            idx = middle.find("-")
            while idx != -1:
                tag_candidate = middle[:idx]
                for n_name, tags in table.items():
                    if tag_candidate and tag_candidate in tags:
                        candidates.add(n_name)
                idx = middle.find("-", idx + 1)
        if len(candidates) > 1:
            raise RuntimeError(
                f"ambiguous container '{nm}': tag matches multiple nodes "
                f"{sorted(candidates)}; address it as node:name"
            )
        if candidates:
            return candidates.pop(), nm
        return nodes.LOCAL_NODE, nm

    @staticmethod
    async def run(
        config: str,
        config_tag: str,
        short_uuid: str,
    ) -> None:
        routing_socks_addr = ""

        if await XrayCore.is_running():
            routing_socks_addr = "host.docker.internal:10808"

        await OlcRTC.run(
            config=config,
            config_tag=config_tag,
            user_id=short_uuid,
            upstream_proxy_addr=routing_socks_addr,
            # NEW container: node selected by EXACT config_tag -> tags match.
            node=None,
        )

    @staticmethod
    async def start(name: str, node: str | None = None) -> None:
        target, nm = await Containers._locate(name, node)
        await OlcRTC.start(nm, node=target)

    @staticmethod
    async def stop(name: str, node: str | None = None) -> None:
        target, nm = await Containers._locate(name, node)
        await OlcRTC.stop(nm, node=target)

    @staticmethod
    async def restart(
        name: str,
        upstream_proxy_addr: str = "",
        upstream_proxy_user: str = "",
        upstream_proxy_pass: str = "",
        node: str | None = None,
    ) -> None:
        target, nm = await Containers._locate(name, node)
        await OlcRTC.restart(
            name=nm,
            upstream_proxy_addr=upstream_proxy_addr,
            upstream_proxy_user=upstream_proxy_user,
            upstream_proxy_pass=upstream_proxy_pass,
            node=target,
        )

    @staticmethod
    async def remove(name: str, node: str | None = None) -> None:
        target, nm = await Containers._locate(name, node)
        await OlcRTC.remove(nm, node=target)

    @staticmethod
    async def logs(name: str, node: str | None = None) -> ContainerLogsSchema:
        target, nm = await Containers._locate(name, node)
        logs = await OlcRTC.logs(nm, node=target)

        return ContainerLogsSchema(
            name=nm,
            logs=logs,
        )

    @staticmethod
    async def get_config(name: str, node: str | None = None) -> ContainerConfigSchema:
        target, nm = await Containers._locate(name, node)
        config = await OlcRTC.get_config(nm, node=target)

        if isinstance(config, bytes):
            config = config.decode()

        return ContainerConfigSchema(
            name=nm,
            config=config,
        )

    @staticmethod
    async def get_stats(name: str, node: str | None = None) -> ContainerStatsSchema:
        target, nm = await Containers._locate(name, node)
        # One inspect for identity (full id) + one exec for stats: the id is
        # the cross-node-stable quota key (names may collide across nodes).
        cont = await OlcRTC.get(nm, node=target)
        data = await OlcRTC.get_stats(nm, node=target)

        return ContainerStatsSchema(
            name=nm,
            node=target,
            container_id=cont._id,
            upload_bytes=int(data.get("upload_bytes", 0)),
            download_bytes=int(data.get("download_bytes", 0)),
            total_bytes=int(data.get("total_bytes", 0)),
            upload_rate_bps=int(data.get("upload_rate_bps", 0)),
            download_rate_bps=int(data.get("download_rate_bps", 0)),
        )

    @staticmethod
    async def stop_all_by_short_uuid(short_uuid: str) -> None:
        containers = await Containers.all()

        await asyncio.gather(
            *(
                Containers.stop(container.name, node=container.node)
                for container in containers
                if container.short_uuid == short_uuid
            )
        )

    @staticmethod
    async def stop_all_by_config_tag(config_tag: str) -> None:
        containers = await Containers.all()

        await asyncio.gather(
            *(
                Containers.stop(container.name, node=container.node)
                for container in containers
                if container.config_tag == config_tag
            )
        )

    @staticmethod
    async def remove_all_by_short_uuid(short_uuid: str) -> None:
        containers = await Containers.all()

        await asyncio.gather(
            *(
                Containers.remove(container.name, node=container.node)
                for container in containers
                if container.short_uuid == short_uuid
            )
        )

    @staticmethod
    async def remove_all_by_config_tag(config_tag: str) -> None:
        containers = await Containers.all()

        await asyncio.gather(
            *(
                Containers.remove(container.name, node=container.node)
                for container in containers
                if container.config_tag == config_tag
            )
        )

    @staticmethod
    async def panel_containers(
        short_uuid: str | None = None,
    ) -> tuple[list[LocatedContainer], list[str]]:
        """Panel-managed containers across ALL nodes as node-aware handles.

        Sweeps every configured node INDEPENDENTLY: a dead daemon degrades
        only its own contribution - healthy nodes still yield their handles
        (revoke on an active node is never blocked by a dead sibling).
        Returns (handles, node_errors); node_errors carries one entry per
        unreachable node ("node: error") so callers decide whether a
        partial view is acceptable for their invariant.
        """
        node_names = nodes.names()
        results = await asyncio.gather(
            *(_collect_node_containers(n) for n in node_names)
        )

        handles: list[LocatedContainer] = []
        node_errors: list[str] = []
        for node_name, containers, error in results:
            if error is not None:
                node_errors.append(f"{node_name}: {error}")
                continue
            for cont in containers:
                try:
                    info = await cont.show()
                except Exception as exc:  # noqa: BLE001 - inspect may fail per container
                    node_errors.append(f"{node_name}: inspect failed: {exc}")
                    continue
                parsed = Containers.parse_panel_labels(info)
                if parsed is None:
                    continue
                if short_uuid is not None and parsed[1] != short_uuid:
                    continue
                name = info["Name"].lstrip("/")
                handles.append(LocatedContainer(node_name, name, container=cont))
        return handles, node_errors

    @staticmethod
    async def locate(name_or_tag: str, node: str | None = None) -> LocatedContainer:
        """Locate ONE existing container, returning a node-aware handle.

        Resolution per _locate; the handle's follow-up calls go to the same
        node. Unknown names raise (no local-fallback guessing).
        """
        target, nm = await Containers._locate(name_or_tag, node)
        from olcrtc.sdk import OlcRTC
        cont = await OlcRTC.get(nm, node=target)
        return LocatedContainer(target, nm, container=cont)

    @staticmethod
    async def get_config_of(handle: LocatedContainer) -> str:
        """Config of a node-aware handle (exec on its own node)."""
        from olcrtc.sdk import OlcRTC
        config = await OlcRTC.get_config(handle.name, node=handle.node)
        if isinstance(config, bytes):
            config = config.decode()
        return config

    @staticmethod
    async def remove_handle(handle: LocatedContainer) -> None:
        """Remove a node-aware handle (label-guarded, on its own node)."""
        from olcrtc.sdk import OlcRTC
        await OlcRTC.remove(handle.name, node=handle.node)
