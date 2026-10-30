import io
import tarfile

import aiodocker
import docker_client
from aiodocker import DockerError
from aiodocker.containers import DockerContainer
from fastapi import HTTPException

from olcrtc import nodes


class XrayCore:
    """Panel-side Xray routing container on the LOCAL Docker daemon.

    Unavailable when OLCWAVE_LOCAL_NODE=false: is_running() is False and
    every other call raises HTTP 409 instead of touching a missing daemon.
    """

    CONTAINER_NAME = "olcwave-xraycore"

    @staticmethod
    def available() -> bool:
        return nodes.local_enabled()

    @staticmethod
    def ensure_available() -> None:
        XrayCore._docker()

    @staticmethod
    def _docker() -> aiodocker.Docker:
        if not XrayCore.available() or docker_client.docker is None:
            raise HTTPException(
                status_code=409,
                detail="routing is unavailable: the panel has no local Docker node "
                "(OLCWAVE_LOCAL_NODE=false)",
            )
        return docker_client.docker

    @staticmethod
    async def run(xray_json: str) -> DockerContainer:
        docker = XrayCore._docker()
        try:
            old = await docker.containers.get(XrayCore.CONTAINER_NAME)
            await old.delete(force=True)
        except DockerError:
            pass

        container = await docker.containers.create(
            config={
                "Image": "xraycore",
                "Env": [
                    f"CONFIG={xray_json}",
                ],
                "HostConfig": {
                    "PortBindings": {
                        "10808/tcp": [
                            {
                                "HostIp": "172.17.0.1",
                                "HostPort": "10808",
                            }
                        ],
                        "10808/udp": [
                            {
                                "HostIp": "172.17.0.1",
                                "HostPort": "10808",
                            }
                        ],
                    },
                },
                "ExposedPorts": {
                    "10808/tcp": {},
                    "10808/udp": {},
                },
            },
            name=XrayCore.CONTAINER_NAME,
        )

        await container.start()

        return container

    @staticmethod
    async def start() -> None:
        docker = XrayCore._docker()
        try:
            container = await docker.containers.get(XrayCore.CONTAINER_NAME)
            await container.start()
        except DockerError:
            print("XRAY CONTAINER NOT FOUND")

    @staticmethod
    async def stop() -> None:
        docker = XrayCore._docker()
        container = await docker.containers.get(XrayCore.CONTAINER_NAME)
        await container.stop()

    @staticmethod
    async def logs() -> str:
        docker = XrayCore._docker()
        container = await docker.containers.get(XrayCore.CONTAINER_NAME)
        logs = await container.log(stdout=True, stderr=True)
        return "".join(logs)

    @staticmethod
    async def get() -> DockerContainer:
        docker = XrayCore._docker()
        return await docker.containers.get(XrayCore.CONTAINER_NAME)

    @staticmethod
    async def is_running() -> bool:
        if not XrayCore.available():
            return False
        docker = XrayCore._docker()
        try:
            container = await docker.containers.get(XrayCore.CONTAINER_NAME)
            info = await container.show()
            return info["State"]["Status"] == "running"
        except DockerError:
            return False

    @staticmethod
    async def _get_archive(path: str) -> bytes:
        docker = XrayCore._docker()
        async with docker._query(
            f"containers/{XrayCore.CONTAINER_NAME}/archive",
            method="GET",
            params={"path": path},
        ) as response:
            archive = await response.read()    

            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                member = tar.getmembers()[0]

                fp = tar.extractfile(member)
                if fp is None:
                    raise FileNotFoundError(path)

                return fp.read()

    @staticmethod
    async def get_geoip() -> bytes:
        return await XrayCore._get_archive("/app/geoip.dat")

    @staticmethod
    async def get_geosite() -> bytes:
        return await XrayCore._get_archive("/app/geosite.dat")