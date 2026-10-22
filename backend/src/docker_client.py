import aiodocker

docker: aiodocker.Docker | None = None


async def init_docker():
    global docker
    docker = aiodocker.Docker()


async def close_docker():
    global docker

    # Remember the local instance the nodes registry shares with us, close
    # every node client (local + remote daemons) exactly once, then drop
    # the module-level reference. Socket lifecycle ends with the API.
    from olcrtc import nodes
    shared_local = nodes.local_docker()
    await nodes.close_all()

    if docker is not None and docker is not shared_local:
        await docker.close()
    docker = None
    nodes.reset_cache()