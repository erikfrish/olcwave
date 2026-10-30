import aiodocker

docker: aiodocker.Docker | None = None


async def init_docker():
    global docker
    from olcrtc import nodes
    docker = aiodocker.Docker() if nodes.local_enabled() else None


async def close_docker():
    global docker

    # Remember the local instance the nodes registry shares with us, close
    # every node client (local + remote daemons) exactly once, then drop
    # the module-level reference. Socket lifecycle ends with the API.
    from olcrtc import nodes
    shared_local = nodes.local_docker() if nodes.local_enabled() else None
    await nodes.close_all()

    if docker is not None and docker is not shared_local:
        await docker.close()
    docker = None
    nodes.reset_cache()