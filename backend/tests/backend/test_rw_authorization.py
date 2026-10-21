# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
"""Permanent behavioral tests for the RW authorization slice.

Every test drives an actual consumer (Subscriptions.get, the reconciler
sweep, the ledger) at its real boundary; the daemon, the Postgres server and
the Remnawave panel are faked. No network, no Docker daemon, no real
database: opening a real DB session fails the test loudly.

Pinned behavior:
- a revocation waits for an in-flight provisioning critical section and then
  removes what provisioning created (same per-user critical section)
- a deny fact through GET /sub revokes owned containers and returns 404
- an outage serves cached configs only; nothing is created or replaced
- the outage grace is bounded by the persisted last_verified and cannot be
  extended by more requests or a restart
- the reconciler does not mutate within grace and revokes past it, without
  ever rewriting last_verified (no window extension)
- a credential rotation removes only pre-rotation containers; the applied
  generation is persisted ONLY after every removal succeeded, and a failed
  removal is retried by later sweeps
- a rotation generation already applied is never re-applied
- ownership is decided by management labels: unmarked `olcwave-`-prefixed
  names are never classified, whatever the owner's length or alphabet
- the traffic limit still answers 403 through the same request path

Run (no other deps needed):
    cd backend && uv sync --group dev && uv run pytest tests/backend/test_rw_authorization.py -q
or, without uv:
    cd backend && .venv/bin/python -m pip install pytest pytest-asyncio
    cd backend && .venv/bin/python -m pytest tests/backend/test_rw_authorization.py -q
(pytest picks pythonpath=["src"] from pyproject.toml.)
"""

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Fakes: docker-like container list, Remnawave access source, no-DB guard
# ---------------------------------------------------------------------------


class FakeContainer:
    def __init__(self, name: str, labels: dict, created: float, state="running"):
        self._info = {
            "Name": f"/{name}",
            "Created": created,
            "State": {"Status": state},
            "Config": {"Image": "olcrtc", "Labels": labels},
            "Id": name[::-1] + "0123456789ab",
        }
        self.removed = False

    async def show(self):
        return self._info

    async def delete(self, force=False):
        self.removed = True


class FakeDocker:
    def __init__(self):
        self.containers: list[FakeContainer] = []

    def add(self, name, tag, owner, created):
        labels = {
            "olcwave.managed": "true",
            "olcwave.config_tag": tag,
            "olcwave.owner": owner,
        }
        c = FakeContainer(name, labels, created)
        self.containers.append(c)
        return c

    def add_unmarked(self, name, created=0.0):
        c = FakeContainer(name, {}, created)
        self.containers.append(c)
        return c


@pytest.fixture
def fake_docker(monkeypatch):
    fd = FakeDocker()

    from olcrtc import sdk as olcrtc_sdk

    async def all_containers(include_stopped=False):
        return fd.containers

    async def remove(name):
        for c in fd.containers:
            if c._info["Name"].lstrip("/") == name:
                c.removed = True
                return
        raise RuntimeError(f"no such container: {name}")

    monkeypatch.setattr(olcrtc_sdk.OlcRTC, "all", staticmethod(all_containers))
    monkeypatch.setattr(olcrtc_sdk.OlcRTC, "remove", staticmethod(remove))
    return fd


class FakeAccess:
    """Programmable replacement for the authoritative evaluate step."""

    def __init__(self):
        self.decision = None
        self.error = None
        self.calls = 0

    async def __call__(self, short_uuid):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.decision


@pytest.fixture
def fake_access(monkeypatch):
    fa = FakeAccess()

    import rw.policy as policy
    from rw import reconcile as rec

    # Both importers of the authoritative step must see the fake.
    monkeypatch.setattr(policy, "evaluate", fa)
    monkeypatch.setattr(rec, "evaluate", fa)
    return fa


@pytest.fixture
def no_db(monkeypatch):
    """Ledger persistence faked in-process; any real DB session fails hard."""
    store: dict[str, dict] = {}

    import database
    import routing.service
    import settings.service
    import rw.policy as policy
    import users.service as users_service
    import profiles.service as profiles_service

    async def forbidden_session(*a, **k):
        raise AssertionError("test opened a real DB session")

    for mod in (database, routing.service, settings.service, policy,
                users_service, profiles_service):
        monkeypatch.setattr(mod, "async_session_factory", forbidden_session)

    async def get_last_verified(short_uuid):
        lv = (store.get(short_uuid) or {}).get("last_verified")
        return float(lv) if lv is not None else None

    async def mark_verified(short_uuid):
        store.setdefault(short_uuid, {})["last_verified"] = time.time()

    async def get_applied_revoke_gen(short_uuid):
        g = (store.get(short_uuid) or {}).get("applied_revoke_gen")
        return float(g) if g is not None else None

    async def mark_rotation_applied(short_uuid, gen):
        store.setdefault(short_uuid, {})["applied_revoke_gen"] = gen

    monkeypatch.setattr(policy.UserAccessLedger, "get_last_verified", staticmethod(get_last_verified))
    monkeypatch.setattr(policy.UserAccessLedger, "mark_verified", staticmethod(mark_verified))
    monkeypatch.setattr(policy.UserAccessLedger, "get_applied_revoke_gen", staticmethod(get_applied_revoke_gen))
    monkeypatch.setattr(policy.UserAccessLedger, "mark_rotation_applied", staticmethod(mark_rotation_applied))
    return store


@pytest.fixture
def panel_env(monkeypatch, fake_docker, fake_access, no_db):
    """RW-enabled request environment: runtime settings, stored configs,
    profile rows and traffic - all in-memory, no DB, no panel, no daemon."""
    from config import settings
    from settings.service import SettingsService
    from settings.schemas import RuntimeSettings
    from profiles.service import Profiles
    from users.service import Users
    from olcrtc import sdk as olcrtc_sdk
    from xraycore.sdk import XrayCore

    async def no_upstream_routing():
        return False

    monkeypatch.setattr(XrayCore, "is_running", staticmethod(no_upstream_routing))

    monkeypatch.setattr(settings, "RW_ENABLED", True)
    monkeypatch.setattr(SettingsService, "_settings", RuntimeSettings())

    configs: dict[str, str] = {}  # container name -> stored config yaml

    async def fake_get_config(name):
        if name not in configs:
            raise RuntimeError(f"no stored config for {name}")
        return configs[name]

    monkeypatch.setattr(olcrtc_sdk.OlcRTC, "get_config", staticmethod(fake_get_config))

    async def create_container(config, config_tag, user_id, **_kwargs):
        name = f"olcwave-{config_tag}-{user_id}"
        fake_docker.add(name, config_tag, user_id, time.time())
        configs[name] = config

    monkeypatch.setattr(olcrtc_sdk.OlcRTC, "run", staticmethod(create_container))

    profile_rows: list = []  # ProfileSchema rows as Profiles.get_all would return

    async def fake_get_all_profiles():
        return list(profile_rows)

    monkeypatch.setattr(Profiles, "get_all", staticmethod(fake_get_all_profiles))

    from users.schemas import UserSchema

    local_users: dict[str, UserSchema] = {}

    async def fake_get_user(short_uuid):
        if short_uuid not in local_users:
            raise RuntimeError("no local user row")
        return local_users[short_uuid]

    async def fake_add_user(user: UserSchema):
        local_users[user.short_uuid] = user
        return user

    monkeypatch.setattr(Users, "get", staticmethod(fake_get_user))
    monkeypatch.setattr(Users, "add", staticmethod(fake_add_user))

    traffic: dict[str, object] = {}

    async def fake_get_traffic(short_uuid):
        return traffic[short_uuid]

    monkeypatch.setattr(Users, "get_traffic", staticmethod(fake_get_traffic))

    return SimpleNamespace(
        configs=configs, profiles=profile_rows, traffic=traffic,
        users=local_users,
    )


def ok_traffic(uuid):
    from users.schemas import TrafficInfoSchema

    return TrafficInfoSchema(
        short_uuid=uuid, limit=0, used=0, remaining=0, unlimited=True, exceeded=False
    )


MINIMAL_CONFIG = """\
auth:
  provider: jitsi
  token: ''
net:
  transport: datachannel
room:
  id: room/a/b
crypto:
  key: placeholder
"""


def allow(uuid, revoked_at=None):
    """Authoritative allow whose access carries a user record, exactly as
    RwAccess.user does on the real request path."""
    return SimpleNamespace(
        allowed=True,
        reason="ok",
        access=SimpleNamespace(
            allowed=True,
            reason="ok",
            short_uuid=uuid,
            sub_revoked_at=revoked_at,
            user=SimpleNamespace(
                username="u",
                expire_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            ),
        ),
    )


def deny(uuid, reason="status_disabled", revoked_at=None):
    return SimpleNamespace(
        allowed=False,
        reason=reason,
        access=SimpleNamespace(
            allowed=False,
            reason=reason,
            short_uuid=uuid,
            sub_revoked_at=revoked_at,
        ),
    )


def unavailable(detail="boom"):
    from rw.sdk import RemnawaveUnavailable

    return RemnawaveUnavailable(detail)


def _allow_with_revoke(uuid, revoked_epoch):
    dt = datetime.fromtimestamp(revoked_epoch, tz=timezone.utc)
    return allow(uuid, revoked_at=dt)


# ---------------------------------------------------------------------------
# Request path: revocation can never interleave with provisioning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_waits_for_inflight_ensure_then_removes_created(
    fake_docker, fake_access, no_db, panel_env, monkeypatch
):
    """A deny/revocation landing while a subscription request is mid-provision
    runs in the same per-user critical section: it waits for the ensure to
    finish and then removes the container the ensure just created."""
    from profiles.schemas import ProfileSchema
    from profiles.service import Containers
    from subscriptions.service import Subscriptions
    from rw import reconcile as rec

    uuid = "race-user"
    panel_env.profiles.append(
        ProfileSchema(name="Jitsi", tag="jitsi", profile=MINIMAL_CONFIG)
    )
    panel_env.traffic[uuid] = ok_traffic(uuid)
    fake_access.decision = allow(uuid)

    order = []
    real_ensure = Subscriptions.ensure_profiles_running

    async def slow_ensure(short_uuid):
        order.append("ensure:start")
        result = await real_ensure(short_uuid)
        order.append("ensure:end")
        return result

    monkeypatch.setattr(
        Subscriptions, "ensure_profiles_running", staticmethod(slow_ensure)
    )

    real_revoke = rec.revoke_user_unchecked

    async def slow_revoke(short_uuid, reason):
        order.append("revoke:start")
        out = await real_revoke(short_uuid, reason)
        order.append("revoke:end")
        return out

    monkeypatch.setattr(rec, "revoke_user_unchecked", slow_revoke)

    async def request_task():
        return await Subscriptions.get(uuid)

    async def revoke_task():
        return await rec.ReconcileManager.revoke_user(uuid, "test")

    a = asyncio.ensure_future(request_task())
    await asyncio.sleep(0.01)  # let the request take the per-user lock first
    b = asyncio.ensure_future(revoke_task())
    await asyncio.gather(a, b)

    assert order == [
        "ensure:start",
        "ensure:end",
        "revoke:start",
        "revoke:end",
    ], "revocation must wait for the in-flight provisioning critical section"

    # the revocation then removed what the ensure had just created
    assert fake_docker.containers, "ensure must have provisioned a container"
    assert all(c.removed for c in fake_docker.containers)


# ---------------------------------------------------------------------------
# Request path: deny, outage, allow, traffic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_deny_revokes_and_returns_404(
    fake_docker, fake_access, no_db, panel_env
):
    from subscriptions.service import Subscriptions

    uuid = "deny-get-user"
    fake_docker.add("olcwave-jitsi-" + uuid, "jitsi", uuid, created=time.time())
    fake_access.decision = deny(uuid, "status_disabled")

    resp = await Subscriptions.get(uuid)

    assert resp.status_code == 404
    assert fake_docker.containers[0].removed, (
        "a deny fact must remove the user's containers before answering"
    )


@pytest.mark.asyncio
async def test_get_outage_serves_cached_only_never_creates(
    fake_docker, fake_access, no_db, panel_env, monkeypatch
):
    from profiles.schemas import ProfileSchema
    from profiles.service import Containers
    from subscriptions.service import Subscriptions

    uuid = "outage-get-user"
    container = fake_docker.add(
        "olcwave-jitsi-" + uuid, "jitsi", uuid, created=time.time()
    )
    panel_env.configs["olcwave-jitsi-" + uuid] = MINIMAL_CONFIG
    panel_env.profiles.append(
        ProfileSchema(name="Jitsi", tag="jitsi", profile=MINIMAL_CONFIG)
    )
    panel_env.traffic[uuid] = ok_traffic(uuid)

    fake_access.decision = allow(uuid)
    first = await Subscriptions.get(uuid)  # verifies once and serves
    assert first.status_code == 200

    fake_access.error = unavailable("down")
    fake_access.decision = None

    calls = {"n": 0}
    real_ensure = Subscriptions.ensure_profiles_running

    async def counting_ensure(short_uuid):
        calls["n"] += 1
        return await real_ensure(short_uuid)

    created = []

    async def forbid_run(config, config_tag, short_uuid):
        created.append(config_tag)

    monkeypatch.setattr(
        Subscriptions, "ensure_profiles_running", staticmethod(counting_ensure)
    )
    monkeypatch.setattr(Containers, "run", staticmethod(forbid_run))

    resp = await Subscriptions.get(uuid)

    assert resp.status_code == 200
    assert b"olcrtc://jitsi" in resp.body, "cached config must be served"
    assert calls["n"] == 0, "outage must not run the provisioning step"
    assert created == [], "outage must never create containers"
    assert not container.removed


@pytest.mark.asyncio
async def test_get_allow_provisions_missing_profile(
    fake_docker, fake_access, no_db, panel_env, monkeypatch
):
    from profiles.schemas import ProfileSchema
    from profiles.service import Containers
    from subscriptions.service import Subscriptions

    uuid = "allow-get-user"
    panel_env.profiles.append(
        ProfileSchema(name="Jitsi", tag="jitsi", profile=MINIMAL_CONFIG)
    )
    panel_env.traffic[uuid] = ok_traffic(uuid)
    fake_access.decision = allow(uuid)

    created = []

    async def fake_run(config, config_tag, short_uuid):
        created.append((config_tag, short_uuid))
        fake_docker.add(
            f"olcwave-{config_tag}-{short_uuid}", config_tag, short_uuid,
            created=time.time(),
        )

    monkeypatch.setattr(Containers, "run", staticmethod(fake_run))

    resp = await Subscriptions.get(uuid)

    assert resp.status_code == 200
    assert b"olcrtc://jitsi" in resp.body
    assert created == [("jitsi", uuid)], (
        "an authoritative allow must provision the missing profile container"
    )


@pytest.mark.asyncio
async def test_get_traffic_limit_returns_403(
    fake_docker, fake_access, no_db, panel_env
):
    from users.schemas import TrafficInfoSchema
    from subscriptions.service import Subscriptions

    uuid = "traffic-user"
    panel_env.traffic[uuid] = TrafficInfoSchema(
        short_uuid=uuid, limit=1000, used=1000, remaining=0,
        unlimited=False, exceeded=True,
    )
    fake_access.decision = allow(uuid)

    resp = await Subscriptions.get(uuid)

    assert resp.status_code == 403
    assert b"Traffic limit exceeded" in resp.body


# ---------------------------------------------------------------------------
# Outage grace: cached-only, bounded, not extendable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outage_within_grace_denies_mutation_but_allows_cached(
    fake_docker, fake_access, no_db
):
    import rw.policy as policy

    uuid = "outage-user-1"
    fake_docker.add("olcwave-jitsi-" + uuid, "jitsi", uuid, created=time.time() - 60)

    # never verified: grace does not apply -> denied outright
    fake_access.error = unavailable("panel down")
    decision = await policy.SubscriptionGuard.check(uuid)
    assert decision.allowed is False

    # verify once, then outage: within grace cached-only (access is None -
    # no authoritative record to mutate anything from)
    fake_access.error = None
    fake_access.decision = allow(uuid)
    decision = await policy.SubscriptionGuard.check(uuid)
    assert decision.allowed is True and decision.access is not None

    fake_access.error = unavailable("panel down")
    fake_access.decision = None
    decision = await policy.SubscriptionGuard.check(uuid)
    assert decision.allowed is True and decision.access is None, (
        "outage within grace must allow serving cached state only"
    )


@pytest.mark.asyncio
async def test_grace_is_bounded_and_not_extended_by_restart(
    fake_docker, fake_access, no_db
):
    import rw.policy as policy

    uuid = "grace-user-1"

    # verified long ago (past any sane grace)
    await policy.UserAccessLedger.mark_verified(uuid)
    no_db[uuid]["last_verified"] = time.time() - 10_000

    fake_access.error = unavailable("down")
    fake_access.decision = None

    decision = await policy.SubscriptionGuard.check(uuid)
    assert decision.allowed is False, "stale verification must not bridge an outage"

    # a fresh "process" (cache cleared) reads the same persisted state
    policy.UserAccessLedger._CACHE.clear()
    decision = await policy.SubscriptionGuard.check(uuid)
    assert decision.allowed is False, "restart must not restart the grace clock"


@pytest.mark.asyncio
async def test_reconciler_outage_no_mutation_then_past_grace_revoke(
    fake_docker, fake_access, no_db, monkeypatch
):
    from rw import reconcile as rec

    uuid = "recon-user-1"
    fake_docker.add("olcwave-jitsi-" + uuid, "jitsi", uuid, created=time.time() - 30)

    # verify once: anchors the persisted grace window
    fake_access.decision = allow(uuid)
    assert await rec.ReconcileManager._evaluate(uuid) == "allowed"

    fake_access.error = unavailable("down")
    fake_access.decision = None

    async def forbid_remove(name):
        raise AssertionError(f"removal attempted within grace: {name}")

    real_remove = rec.OlcRTC.remove
    monkeypatch.setattr(rec.OlcRTC, "remove", staticmethod(forbid_remove))

    outcome = await rec.ReconcileManager._evaluate(uuid)
    assert outcome == "unavailable", "within grace the sweep must not touch containers"
    assert not any(c.removed for c in fake_docker.containers)

    # push last_verified far into the past: past grace -> revoke
    stale = time.time() - 10_000
    no_db[uuid]["last_verified"] = stale
    monkeypatch.setattr(rec.OlcRTC, "remove", staticmethod(real_remove))

    outcome = await rec.ReconcileManager._evaluate(uuid)
    assert outcome == "revoked"
    assert any(c.removed for c in fake_docker.containers)
    # the window cannot be restarted by the sweep itself
    assert no_db[uuid]["last_verified"] == stale, (
        "revocation must not rewrite last_verified - no indefinite extension"
    )


# ---------------------------------------------------------------------------
# Rotation: applied only after confirmed removal, per-container generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rotation_removes_pre_rotation_and_keeps_fresh(
    fake_docker, fake_access, no_db
):
    from rw import reconcile as rec

    uuid = "rot-user-1"
    gen = 1700000000.0
    fake_docker.add("olcwave-old-" + uuid, "old", uuid, created=gen - 100)
    fake_docker.add("olcwave-new-" + uuid, "new", uuid, created=gen + 100)

    fake_access.error = None
    fake_access.decision = _allow_with_revoke(uuid, gen)

    outcome = await rec.ReconcileManager._evaluate(uuid)
    assert outcome == "revoked"

    old = fake_docker.containers[0]
    new = fake_docker.containers[1]
    assert old.removed, "pre-rotation container must be removed"
    assert not new.removed, "post-rotation container with fresh keys must survive"
    assert no_db[uuid]["applied_revoke_gen"] == gen


@pytest.mark.asyncio
async def test_failed_rotation_removal_is_not_marked_applied(
    fake_docker, fake_access, no_db, monkeypatch
):
    from rw import reconcile as rec

    uuid = "rot-user-2"
    gen = 1700000000.0
    fake_docker.add("olcwave-old-" + uuid, "old", uuid, created=gen - 100)

    fake_access.decision = _allow_with_revoke(uuid, gen)

    real_remove = rec.OlcRTC.remove
    failures = {"n": 0}

    async def flaky_remove(name):
        if failures["n"] == 0:
            failures["n"] += 1
            raise RuntimeError("docker daemon hiccup")
        return await real_remove(name)

    # first sweep: removal fails -> must raise and NOT persist the generation
    monkeypatch.setattr(rec.OlcRTC, "remove", staticmethod(flaky_remove))
    with pytest.raises(RuntimeError):
        await rec.ReconcileManager._evaluate(uuid)
    assert "applied_revoke_gen" not in no_db.get(uuid, {}), (
        "a failed removal must never be persisted as applied"
    )
    assert not fake_docker.containers[0].removed

    # next sweep retries the same generation and succeeds
    await rec.ReconcileManager._evaluate(uuid)
    assert fake_docker.containers[0].removed
    assert no_db[uuid]["applied_revoke_gen"] == gen
    assert failures["n"] == 1


@pytest.mark.asyncio
async def test_rotation_applied_generation_skips_second_sweep(
    fake_docker, fake_access, no_db
):
    from rw import reconcile as rec

    uuid = "rot-user-3"
    gen = time.time()
    fake_docker.add("olcwave-old-" + uuid, "old", uuid, created=gen - 100)
    fake_access.decision = _allow_with_revoke(uuid, gen)

    await rec.ReconcileManager._evaluate(uuid)
    assert fake_docker.containers[0].removed

    # a fresh container created by the (valid) user after the rotation, and a
    # repeated evaluation carrying the same generation: nothing may be removed
    fake_docker.add("olcwave-new-" + uuid, "new", uuid, created=gen + 50)
    outcome = await rec.ReconcileManager._evaluate(uuid)
    assert outcome == "allowed"
    assert not fake_docker.containers[1].removed


# ---------------------------------------------------------------------------
# Ownership: labels decide, names never
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unmarked_prefix_containers_never_owned(fake_docker):
    from rw import reconcile as rec

    fake_docker.add_unmarked("olcwave-iso-postgres", created=time.time())
    fake_docker.add_unmarked("olcwave-anything", created=time.time())

    owned = await rec.panel_owned_containers()
    assert owned == [], "unmarked containers must never be classified as panel-owned"

    removed = await rec.revoke_user_unchecked("iso-postgres", "test")
    assert removed == 0
    assert all(not c.removed for c in fake_docker.containers)


@pytest.mark.asyncio
async def test_owner_length_not_assumed(fake_docker):
    from rw import reconcile as rec

    for owner in ("a", "abc12", "u" * 16, "u" * 40):
        fake_docker.add(f"olcwave-jitsi-{owner}", "jitsi", owner, created=1.0)

    owned = await rec.panel_owned_containers()
    assert len(owned) == 4, "owners of any length must be classified by label"

    short = await rec.panel_owned_containers("a")
    assert len(short) == 1


@pytest.mark.asyncio
async def test_denied_user_containers_removed_under_lock(
    fake_docker, fake_access, no_db
):
    from rw import reconcile as rec

    uuid = "deny-user-1"
    fake_docker.add("olcwave-jitsi-" + uuid, "jitsi", uuid, created=time.time())

    fake_access.decision = deny(uuid, "status_disabled")
    outcome = await rec.ReconcileManager._evaluate(uuid)

    assert outcome == "revoked"
    assert fake_docker.containers[0].removed
    assert no_db[uuid].get("last_verified") is not None
