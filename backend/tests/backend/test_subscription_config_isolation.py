"""Per-container config isolation on the subscription path.

Regression: one container whose `cat /tmp/olcwave/config.yaml` exec fails
(Docker 409 while the container restarts) used to fail the whole
asyncio.gather, 503ing a subscription whose other profiles were perfectly
readable - and any code path that treated the failed read as "missing"
would have recreated the container with a FRESH key/room, breaking every
client holding the current config.
"""
import pytest
import yaml

from test_rw_authorization import (
    allow, fake_access, fake_docker, no_db, ok_traffic, panel_env,
)


def _jitsi_profile(tag):
    from profiles.schemas import ProfileSchema

    return ProfileSchema(
        name=tag, tag=tag, profile=yaml.safe_dump({
            "auth": {"provider": "jitsi"},
            "room": {"id": f"https://meet.example.org/{tag}"},
            "net": {"transport": "datachannel"},
        }),
    )


async def _provision_two_profiles(user, panel_env, fake_access):
    """Allow-listed user with profiles `good` and `flap` provisioned once."""
    from subscriptions.service import Subscriptions

    for tag in ("good", "flap"):
        panel_env.profiles.append(_jitsi_profile(tag))
    panel_env.traffic[user] = ok_traffic(user)
    fake_access.decision = allow(user)
    resp = await Subscriptions.get(user)
    return resp


@pytest.mark.asyncio
async def test_restarting_container_409_serves_working_profiles(
    fake_docker, fake_access, no_db, panel_env, monkeypatch,
):
    """One container failing its config read must not 503 the subscription.

    The failing container's config is served from its inspect CONFIG env
    (same key/room the clients hold), the working profile is served
    normally, and NOTHING is removed or recreated.
    """
    from olcrtc import sdk as olcrtc_sdk
    from subscriptions.service import Subscriptions

    user = "flap-user"
    resp = await _provision_two_profiles(user, panel_env, fake_access)
    assert resp.status_code == 200
    issued = dict(panel_env.configs)
    assert len(fake_docker.containers) == 2

    # Exec read fails for the `flap` container only (409 while restarting).
    real_get_config = olcrtc_sdk.OlcRTC.get_config

    async def failing_get_config(name, node="local"):
        if name == "olcwave-flap-" + user:
            from aiodocker import DockerError
            raise DockerError(409, "container is restarting")
        return await real_get_config(name, node=node)

    monkeypatch.setattr(
        olcrtc_sdk.OlcRTC, "get_config", staticmethod(failing_get_config)
    )

    # The inspect CONFIG env fallback must find the issued config.
    fake_container = next(
        c for c in fake_docker.containers
        if c._info["Name"].lstrip("/") == "olcwave-flap-" + user
    )
    fake_container._info["Config"]["Env"] = [
        f"CONFIG={issued['olcwave-flap-' + user]}",
    ]

    resp = await Subscriptions.get(user)

    assert resp.status_code == 200, (
        "one unreadable container config must not fail the subscription"
    )
    body = resp.body.decode()
    assert "$good" in body, "working profile must still be served"
    assert "$flap" in body, "inspect CONFIG fallback must serve the restarting profile"
    # Same key/room as issued - never a fresh credential.
    original = yaml.safe_load(issued["olcwave-flap-" + user])
    assert original["crypto"]["key"] in body
    assert original["room"]["id"] in body
    assert len(fake_docker.containers) == 2, "nothing recreated"
    assert not any(c.removed for c in fake_docker.containers), "nothing removed"


@pytest.mark.asyncio
async def test_unreadable_config_is_never_recreated(
    fake_docker, fake_access, no_db, panel_env, monkeypatch,
):
    """A tag whose config cannot be read at all is dropped, not recreated.

    The container demonstrably exists; recreating it would mint a fresh
    key/room. The remaining readable profiles are still served with 200.
    """
    from olcrtc import sdk as olcrtc_sdk
    from profiles.service import Containers
    from subscriptions.service import Subscriptions

    user = "drop-user"
    resp = await _provision_two_profiles(user, panel_env, fake_access)
    assert resp.status_code == 200
    issued = dict(panel_env.configs)

    async def failing_get_config(name, node="local"):
        if name == "olcwave-flap-" + user:
            from aiodocker import DockerError
            raise DockerError(409, "container is restarting")
        return panel_env.configs[name]

    monkeypatch.setattr(
        olcrtc_sdk.OlcRTC, "get_config", staticmethod(failing_get_config)
    )
    # No CONFIG env on the container either: nothing to fall back to.
    fake_container = next(
        c for c in fake_docker.containers
        if c._info["Name"].lstrip("/") == "olcwave-flap-" + user
    )
    fake_container._info["Config"]["Env"] = ["CONFIG="]

    created = []

    async def forbid_run(*a, **k):
        created.append(a)

    monkeypatch.setattr(
        Containers, "run", staticmethod(forbid_run),
    )

    resp = await Subscriptions.get(user)

    assert resp.status_code == 200, "readable profiles must still be served"
    body = resp.body.decode()
    assert "$good" in body
    assert "$flap" not in body, "an unavailable profile is not served"
    assert created == [], "an existing container is never recreated"
    assert len(fake_docker.containers) == 2
    assert not any(c.removed for c in fake_docker.containers)
    # The dropped profile's stored config is untouched.
    assert issued["olcwave-flap-" + user] == panel_env.configs[
        "olcwave-flap-" + user
    ]


@pytest.mark.asyncio
async def test_all_configs_unreadable_is_503_not_empty_success(
    fake_docker, fake_access, no_db, panel_env, monkeypatch,
):
    """Every config unreadable -> 503 fail-closed, never an empty 200."""
    from aiodocker import DockerError
    from olcrtc import sdk as olcrtc_sdk
    from subscriptions.service import Subscriptions

    user = "dark-user"
    resp = await _provision_two_profiles(user, panel_env, fake_access)
    assert resp.status_code == 200

    async def failing_get_config(name, node="local"):
        from aiodocker import DockerError
        raise DockerError(409, "container is restarting")

    monkeypatch.setattr(
        olcrtc_sdk.OlcRTC, "get_config", staticmethod(failing_get_config)
    )

    resp = await Subscriptions.get(user)

    assert resp.status_code == 503, (
        "all-unavailable must fail closed, not serve an empty subscription"
    )
    assert len(fake_docker.containers) == 2, "nothing recreated"
    assert not any(c.removed for c in fake_docker.containers)


@pytest.mark.asyncio
async def test_get_existing_skips_unreadable_without_recreating(
    fake_docker, fake_access, no_db, panel_env, monkeypatch,
):
    """get_existing (outage grace path): unreadable tag is skipped, readable
    ones are returned; nothing is created or removed."""
    from aiodocker import DockerError
    from olcrtc import sdk as olcrtc_sdk
    from subscriptions.service import Subscriptions

    user = "outage-user"
    resp = await _provision_two_profiles(user, panel_env, fake_access)
    assert resp.status_code == 200

    async def failing_get_config(name, node="local"):
        if name == "olcwave-flap-" + user:
            raise DockerError(409, "container is restarting")
        return panel_env.configs[name]

    monkeypatch.setattr(
        olcrtc_sdk.OlcRTC, "get_config", staticmethod(failing_get_config)
    )

    configs, profiles = await Subscriptions.get_existing(user)

    assert set(profiles) == {"good"}, "unreadable tag is skipped"
    assert set(configs) == {"good"}
    assert len(fake_docker.containers) == 2
    assert not any(c.removed for c in fake_docker.containers)


@pytest.mark.asyncio
async def test_all_invalid_configs_return_503(
    fake_docker, fake_access, no_db, panel_env,
):
    from subscriptions.service import Subscriptions

    user = "invalid-user"
    await _provision_two_profiles(user, panel_env, fake_access)
    for name in panel_env.configs:
        panel_env.configs[name] = "auth: {provider: jitsi}"

    response = await Subscriptions.get(user)
    assert response.status_code == 503
    assert not any(c.removed for c in fake_docker.containers)
