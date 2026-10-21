import asyncio
from datetime import datetime

from aiodocker.containers import DockerContainer

from xraycore.sdk import XrayCore
from olcrtc.sdk import OlcRTC
from olcrtc.schemas import (
    ContainerConfigSchema,
    ContainerLogsSchema,
    ContainerSchema,
    ContainerStatsSchema,
)


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
    async def to_schema(cont: DockerContainer) -> ContainerSchema | None:
        info = await cont.show()

        parsed = Containers.parse_panel_labels(info)
        if parsed is None:
            return None

        config_tag, user_id = parsed

        return ContainerSchema(
            id=info["Id"][:12],
            name=info["Name"].lstrip("/"),
            short_uuid=user_id,
            config_tag=config_tag,
            status=info["State"]["Status"],
            created=info["Created"],
            created_epoch=Containers.created_epoch(info),
            image=info["Config"]["Image"],
        )

    @staticmethod
    async def all() -> list[ContainerSchema]:
        containers = await OlcRTC.all(include_stopped=True)

        schemas = await asyncio.gather(
            *(Containers.to_schema(container) for container in containers)
        )

        return [schema for schema in schemas if schema is not None]

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
        )

    @staticmethod
    async def start(name: str) -> None:
        await OlcRTC.start(name)

    @staticmethod
    async def stop(name: str) -> None:
        await OlcRTC.stop(name)

    @staticmethod
    async def restart(
        name: str,
        upstream_proxy_addr: str = "",
        upstream_proxy_user: str = "",
        upstream_proxy_pass: str = "",
    ) -> None:
        await OlcRTC.restart(
            name=name,
            upstream_proxy_addr=upstream_proxy_addr,
            upstream_proxy_user=upstream_proxy_user,
            upstream_proxy_pass=upstream_proxy_pass,
        )

    @staticmethod
    async def remove(name: str) -> None:
        await OlcRTC.remove(name)

    @staticmethod
    async def logs(name: str) -> ContainerLogsSchema:
        logs = await OlcRTC.logs(name)

        return ContainerLogsSchema(
            name=name,
            logs=logs,
        )

    @staticmethod
    async def get_config(name: str) -> ContainerConfigSchema:
        config = await OlcRTC.get_config(name)

        if isinstance(config, bytes):
            config = config.decode()

        return ContainerConfigSchema(
            name=name,
            config=config,
        )

    @staticmethod
    async def get_stats(name: str) -> ContainerStatsSchema:
        data = await OlcRTC.get_stats(name)

        return ContainerStatsSchema(
            name=name,
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
                Containers.stop(container.name)
                for container in containers
                if container.short_uuid == short_uuid
            )
        )

    @staticmethod
    async def stop_all_by_config_tag(config_tag: str) -> None:
        containers = await Containers.all()

        await asyncio.gather(
            *(
                Containers.stop(container.name)
                for container in containers
                if container.config_tag == config_tag
            )
        )

    @staticmethod
    async def remove_all_by_short_uuid(short_uuid: str) -> None:
        containers = await Containers.all()

        await asyncio.gather(
            *(
                Containers.remove(container.name)
                for container in containers
                if container.short_uuid == short_uuid
            )
        )

    @staticmethod
    async def remove_all_by_config_tag(config_tag: str) -> None:
        containers = await Containers.all()

        await asyncio.gather(
            *(
                Containers.remove(container.name)
                for container in containers
                if container.config_tag == config_tag
            )
        )