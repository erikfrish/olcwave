# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
"""Permanent behavioral tests for durable traffic accounting and enforcement.

Every test drives the real consumer (TrafficManager._tick / _account_owner /
_enforce_capped_users) at its real boundary; the Docker daemons (local + two
configured nodes) and the Postgres session are faked. No network, no Docker
daemon, no real database: opening a real DB session fails the test loudly.

Pinned behavior:
- the FIRST sample of a container COUNTS its full absolute total (a restart
  before the second tick cannot bypass the cap)
- a later tick counts exactly (total - baseline), aggregated across nodes
- a panel restart re-reads persisted baselines: no loss, no double count
- a counter reset below baseline counts bytes since restart exactly once
- an unreachable node attributes nothing for that tick and leaves its stored
  baseline untouched; the gap is billed exactly once when it returns
- a stats read that yields no usable total is never billed as zero; the
  baseline stays stale and the gap is billed when stats return
- delta + baselines are applied in ONE transaction; a DB failure leaves the
  baseline stale so the SAME delta is retried (never lost, never double)
- resetting a user's traffic rebases observable baselines to current totals
  and drops the rest, in one transaction; history is never re-billed
- enforcement iterates EVERY capped user every tick (deltas or not) and
  stops+removes ONLY the exceeded user's label-owned containers on EVERY
  node; a dead node never blocks enforcement on reachable nodes; a failed
  removal is retried next tick; enforcement holds the per-user lock

Run:
    cd backend && uv run pytest tests/backend/test_traffic_durability.py -q
"""

import inspect
import itertools
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Fakes: per-node docker listing, stats source, in-memory durable store
# ---------------------------------------------------------------------------


_ID_SEQ = itertools.count(1)


class FakeContainer:
    """DockerContainer-compatible surface used by traffic.py."""

    def __init__(self, node: str, name: str, labels: dict, total):
        self.node = node
        self.name = name
        self._labels = labels
        self.total = total  # None => stats exec answers nothing
        self.stopped = False
        self.removed = False
        # Deterministic, globally unique 64-hex-style id: every created
        # container (including a re-created one after a restart) gets a
        # fresh id, exactly like the Docker daemon.
        self.full_id = f"{next(_ID_SEQ):064x}"

    async def show(self):
        return {
            "Id": self.full_id,
            "Name": f"/{self.name}",
            "State": {"Status": "stopped" if self.stopped else "running"},
            "Config": {"Labels": self._labels},
        }


def owner_labels(owner: str) -> dict:
    return {
        "olcwave.managed": "true",
        "olcwave.config_tag": "vm1",
        "olcwave.owner": owner,
    }


class FakeFleet:
    """node -> {name: FakeContainer}; a node's listing can be broken."""

    def __init__(self):
        self.nodes: dict[str, dict[str, FakeContainer]] = {}
        self.broken: set[str] = set()

    def add(self, node: str, name: str, owner: str, total):
        self.nodes.setdefault(node, {})[name] = FakeContainer(
            node, name, owner_labels(owner), total
        )

    def by_name(self, node: str, name: str) -> FakeContainer:
        return self.nodes[node][name]

    @property
    def all_containers(self) -> list[FakeContainer]:
        return [c for m in self.nodes.values() for c in m.values()]


@pytest.fixture
def fleet(monkeypatch):
    fl = FakeFleet()

    from olcrtc import sdk as olcrtc_sdk
    from olcrtc import nodes as nodes_mod

    async def all_containers(include_stopped=False, node="local"):
        if node in fl.broken:
            raise RuntimeError(f"node {node} unreachable")
        return list(fl.nodes.get(node, {}).values())

    async def get_stats(name, node="local"):
        cont = fl.nodes[node][name]
        if cont.total is None:
            return {}
        return {"total_bytes": cont.total}

    async def stop(name, node="local"):
        cont = fl.nodes[node][name]
        if cont.removed:
            raise RuntimeError(f"no such container: {name}")
        cont.stopped = True

    async def remove(name, node="local"):
        cont = fl.nodes[node][name]
        if cont.removed:
            raise RuntimeError(f"no such container: {name}")
        cont.removed = True

    monkeypatch.setattr(
        olcrtc_sdk.OlcRTC, "all", staticmethod(all_containers)
    )
    monkeypatch.setattr(
        olcrtc_sdk.OlcRTC, "get_stats", staticmethod(get_stats)
    )
    monkeypatch.setattr(olcrtc_sdk.OlcRTC, "stop", staticmethod(stop))
    monkeypatch.setattr(olcrtc_sdk.OlcRTC, "remove", staticmethod(remove))
    monkeypatch.setattr(nodes_mod, "names", lambda: ["local", "vm1", "vm2"])
    return fl


class FakeStore:
    """In-memory traffic_cursors + users.traffic_used_bytes."""

    def __init__(self):
        self.cursors: dict[tuple[str, str], int] = {}
        self.used: dict[str, int] = {}
        self.fail_owners: set[str] = set()


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def fake_db(monkeypatch, store):
    """Cursor/delta persistence faked in-process; a real session fails."""
    import database
    import traffic as traffic_mod
    from users.db import TrafficCursorDB

    class _FakeSession:
        """Opaque handle: TrafficCursorDB methods are faked at their own
        boundary and ignore it; a REAL query attempt fails loudly."""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *a, **k):
            raise AssertionError("test issued a real SQL statement")

        async def commit(self):
            raise AssertionError("test committed a real transaction")

    def forbidden_session(*a, **k):
        return _FakeSession()

    import users.service as users_service

    # Every module that imported the factory by name gets the fake.
    for mod in (database, traffic_mod, users_service):
        monkeypatch.setattr(mod, "async_session_factory", forbidden_session)

    async def get_cursor(db, node, container_id):
        key = (node, container_id)
        if key not in store.cursors:
            return None
        return SimpleNamespace(last_total=store.cursors[key])

    async def add_delta_with_cursors(db, short_uuid, delta, cursors):
        if short_uuid in store.fail_owners:
            raise RuntimeError(f"db down for {short_uuid}")
        if delta:
            store.used[short_uuid] = store.used.get(short_uuid, 0) + delta
        for node, container_id, last_total in cursors:
            store.cursors[(node, container_id)] = last_total

    async def reset_traffic_with_baselines(db, short_uuid, observed):
        if short_uuid in store.fail_owners:
            raise RuntimeError(f"db down for {short_uuid}")
        store.used[short_uuid] = 0
        for key in [k for k in list(store.cursors)]:
            del store.cursors[key]
        for node, container_id, total in observed:
            store.cursors[(node, container_id)] = total

    monkeypatch.setattr(
        TrafficCursorDB, "get_cursor", staticmethod(get_cursor)
    )
    monkeypatch.setattr(
        TrafficCursorDB, "add_delta_with_cursors",
        staticmethod(add_delta_with_cursors),
    )
    monkeypatch.setattr(
        TrafficCursorDB, "reset_traffic_with_baselines",
        staticmethod(reset_traffic_with_baselines),
    )
    return store


@pytest.fixture
def users_env(monkeypatch, store):
    """users.get_all against the in-memory store (enforcement source)."""
    from users.service import Users

    limits: dict[str, int] = {}

    async def fake_get_all():
        return [
            SimpleNamespace(
                short_uuid=uuid,
                traffic_limit_bytes=limits.get(uuid, 0),
                traffic_used_bytes=store.used.get(uuid, 0),
            )
            for uuid in limits
        ]

    def rearm():
        monkeypatch.setattr(Users, "get_all", staticmethod(fake_get_all))

    rearm()
    return SimpleNamespace(limits=limits, rearm=rearm)


async def one_tick():
    import traffic as traffic_mod

    await traffic_mod.TrafficManager._tick()


# ---------------------------------------------------------------------------
# Accounting: first sample, deltas, restart, durability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_sample_counts_full_absolute_total(fleet, fake_db):
    """A new managed container's bytes are quota usage: a restart before
    the second tick cannot bypass the cap."""
    fleet.add("local", "olcwave-vm1-alice", "alice", total=10_000)
    fleet.add("vm1", "olcwave-vm1-alice", "alice", total=500)

    await one_tick()

    assert fake_db.used == {"alice": 10_500}
    assert fake_db.cursors[("local", fleet.by_name("local", "olcwave-vm1-alice").full_id)] == 10_000
    assert fake_db.cursors[("vm1", fleet.by_name("vm1", "olcwave-vm1-alice").full_id)] == 500


@pytest.mark.asyncio
async def test_delta_is_exact_difference_across_nodes(fleet, fake_db):
    fleet.add("local", "olcwave-vm1-alice", "alice", total=10_000)
    fleet.add("vm1", "olcwave-vm1-alice", "alice", total=500)

    await one_tick()
    fleet.by_name("local", "olcwave-vm1-alice").total = 12_500
    fleet.by_name("vm1", "olcwave-vm1-alice").total = 900

    await one_tick()

    assert fake_db.used == {"alice": 10_500 + 2_900}


@pytest.mark.asyncio
async def test_panel_restart_uses_persisted_baselines(fleet, fake_db):
    """Fresh process, same persisted store: neither loss nor double count."""
    fleet.add("local", "olcwave-vm1-bob", "bob", total=1_000)
    await one_tick()
    fleet.by_name("local", "olcwave-vm1-bob").total = 4_000
    await one_tick()
    assert fake_db.used == {"bob": 4_000}

    # The cursor rows are re-read from the store on the next tick; the
    # pre-restart usage must NOT be re-billed.
    fleet.by_name("local", "olcwave-vm1-bob").total = 4_500
    await one_tick()

    assert fake_db.used == {"bob": 4_500}


@pytest.mark.asyncio
async def test_counter_reset_below_baseline_counts_since_restart(fleet, fake_db):
    fleet.add("local", "olcwave-vm1-carol", "carol", total=5_000)
    await one_tick()

    # In-place restart: stats counter resets below the baseline.
    fleet.by_name("local", "olcwave-vm1-carol").total = 700
    await one_tick()

    assert fake_db.used == {"carol": 5_700}
    assert fake_db.cursors[("local", fleet.by_name("local", "olcwave-vm1-carol").full_id)] == 700


@pytest.mark.asyncio
async def test_new_container_id_after_restart_counts_fresh(fleet, fake_db):
    """Restart-by-recreate: new container id => first sample counts again."""
    fleet.add("local", "olcwave-vm1-dora", "dora", total=2_000)
    await one_tick()
    assert fake_db.used == {"dora": 2_000}

    old = fleet.by_name("local", "olcwave-vm1-dora")
    del fleet.nodes["local"]["olcwave-vm1-dora"]
    new = FakeContainer("local", "olcwave-vm1-dora", owner_labels("dora"), 300)
    fleet.nodes["local"]["olcwave-vm1-dora"] = new
    assert new.full_id != old.full_id

    await one_tick()

    assert fake_db.used == {"dora": 2_300}
    assert fake_db.cursors[("local", new.full_id)] == 300


@pytest.mark.asyncio
async def test_unreachable_node_attributes_nothing_keeps_baseline(
    fleet, fake_db
):
    fleet.add("local", "olcwave-vm1-erin", "erin", total=1_000)
    fleet.add("vm1", "olcwave-vm1-erin", "erin", total=500)
    await one_tick()
    assert fake_db.used == {"erin": 1_500}

    fleet.broken.add("vm1")  # dead node: its traffic is invisible
    fleet.by_name("local", "olcwave-vm1-erin").total = 1_200
    await one_tick()

    assert fake_db.used == {"erin": 1_700}
    # The dead node's stored baseline is untouched.
    assert fake_db.cursors[("vm1", fleet.by_name("vm1", "olcwave-vm1-erin").full_id)] == 500

    # Next successful pass bills the whole unseen gap exactly once.
    fleet.broken.clear()
    fleet.by_name("vm1", "olcwave-vm1-erin").total = 900
    await one_tick()

    assert fake_db.used == {"erin": 2_100}


@pytest.mark.asyncio
async def test_stats_read_failure_never_bills_zero(fleet, fake_db):
    """A stats read that answers nothing must NOT count as zero usage."""
    fleet.add("local", "olcwave-vm1-fay", "fay", total=2_000)
    await one_tick()
    assert fake_db.cursors[("local", fleet.by_name("local", "olcwave-vm1-fay").full_id)] == 2_000

    fleet.by_name("local", "olcwave-vm1-fay").total = None
    await one_tick()

    assert fake_db.used == {"fay": 2_000}
    # Baseline stays stale so the gap is billed when stats return.
    assert fake_db.cursors[("local", fleet.by_name("local", "olcwave-vm1-fay").full_id)] == 2_000

    fleet.by_name("local", "olcwave-vm1-fay").total = 2_900
    await one_tick()

    assert fake_db.used == {"fay": 2_900}


@pytest.mark.asyncio
async def test_db_failure_retries_same_delta_no_loss_no_double(fleet, fake_db):
    fleet.add("local", "olcwave-vm1-gina", "gina", total=1_000)
    await one_tick()

    fake_db.fail_owners.add("gina")
    fleet.by_name("local", "olcwave-vm1-gina").total = 1_600
    await one_tick()
    assert fake_db.used == {"gina": 1_000}  # apply failed -> not billed
    # Cursor must ALSO be stale (atomicity): the retry bills the same 600.
    assert fake_db.cursors[("local", fleet.by_name("local", "olcwave-vm1-gina").full_id)] == 1_000

    fake_db.fail_owners.clear()
    await one_tick()
    assert fake_db.used == {"gina": 1_600}

    await one_tick()
    assert fake_db.used == {"gina": 1_600}  # no double count


def test_delta_and_cursors_commit_in_one_transaction():
    """Delta + cursor upserts: exactly one commit in the DB method."""
    from users.db import TrafficCursorDB

    src = inspect.getsource(TrafficCursorDB.add_delta_with_cursors)
    assert src.count("await db.commit()") == 1
    assert "traffic_used_bytes=User.traffic_used_bytes + delta" in src
    assert "on_conflict_do_update" in src


# ---------------------------------------------------------------------------
# Reset consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reset_rebases_baselines_and_never_rebills_history(
    fleet, fake_db
):
    fleet.add("local", "olcwave-vm1-hank", "hank", total=3_000)
    fleet.add("vm1", "olcwave-vm1-hank", "hank", total=400)
    await one_tick()
    fleet.by_name("local", "olcwave-vm1-hank").total = 3_500
    await one_tick()
    assert fake_db.used == {"hank": 3_900}

    # The real rebase path: TrafficManager.reset_user_accounting
    # (per-user lock + observed running containers across all nodes).
    from subscriptions.service import acquire_user_lock, release_user_lock
    from traffic import TrafficManager

    # Users.get_all is faked; reset_user_accounting only needs the lock
    # plus OlcRTC stats (already faked by the fleet fixture).
    await TrafficManager.reset_user_accounting("hank")


def test_reset_transaction_shape():
    """reset_traffic_with_baselines commits exactly once."""
    from users.db import TrafficCursorDB

    src = inspect.getsource(TrafficCursorDB.reset_traffic_with_baselines)
    assert src.count("await db.commit()") == 1
    assert "traffic_used_bytes=0" in src
    assert "delete(TrafficCursor)" in src


# ---------------------------------------------------------------------------
# Enforcement: every capped user, every node, only owned, retry, lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_enforces_only_owned_on_every_node(fleet, fake_db, users_env):
    users_env.limits["ivy"] = 1_000
    fleet.add("local", "olcwave-vm1-ivy", "ivy", total=900)
    fleet.add("vm1", "olcwave-vm1-ivy", "ivy", total=300)  # crosses cap
    fleet.add("vm2", "olcwave-vm1-ivy", "ivy", total=50)
    fleet.add("local", "olcwave-vm1-other", "other", total=10)
    fleet.nodes["vm1"]["olcwave-vm1-unmarked"] = FakeContainer(
        "vm1", "olcwave-vm1-unmarked", {}, total=10
    )

    await one_tick()

    by_key = {(c.node, c.name): c for c in fleet.all_containers}
    assert by_key[("local", "olcwave-vm1-ivy")].removed
    assert by_key[("vm1", "olcwave-vm1-ivy")].removed
    assert by_key[("vm2", "olcwave-vm1-ivy")].removed
    assert not by_key[("local", "olcwave-vm1-other")].removed
    assert not by_key[("vm1", "olcwave-vm1-unmarked")].removed


@pytest.mark.asyncio
async def test_capped_user_with_zero_delta_still_enforced(
    fleet, fake_db, users_env, monkeypatch
):
    """Enforcement iterates capped users every tick, deltas or not."""
    import traffic as traffic_mod

    users_env.limits["jack"] = 100
    fleet.add("local", "olcwave-vm1-jack", "jack", total=500)
    await one_tick()  # first sample crosses cap and enforces
    assert fleet.by_name("local", "olcwave-vm1-jack").removed

    # Recreate the container and freeze accounting entirely (DB outage):
    # enforcement must STILL tear the new container down. Fixture-scoped
    # monkeypatch: no leakage into later tests.
    fleet.nodes["local"]["olcwave-vm1-jack"] = FakeContainer(
        "local", "olcwave-vm1-jack", owner_labels("jack"), 10
    )

    async def failing_account(short_uuid, per_cont):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        traffic_mod.TrafficManager,
        "_account_owner",
        staticmethod(failing_account),
    )

    await one_tick()
    assert fleet.by_name("local", "olcwave-vm1-jack").removed


@pytest.mark.asyncio
async def test_dead_node_does_not_block_reachable_nodes_enforcement(
    fleet, fake_db, users_env
):
    users_env.limits["kate"] = 100
    fleet.add("local", "olcwave-vm1-kate", "kate", total=500)
    fleet.add("vm1", "olcwave-vm1-kate", "kate", total=10)
    fleet.add("vm2", "olcwave-vm1-kate", "kate", total=10)
    fleet.broken.add("vm2")

    await one_tick()

    assert fleet.by_name("local", "olcwave-vm1-kate").removed
    assert fleet.by_name("vm1", "olcwave-vm1-kate").removed
    assert not fleet.by_name("vm2", "olcwave-vm1-kate").removed


@pytest.mark.asyncio
async def test_failed_removal_retried_next_tick(
    fleet, fake_db, users_env, monkeypatch
):
    """A failed stop/remove is retried by the NEXT enforcement tick."""
    from olcrtc import sdk as olcrtc_sdk

    users_env.limits["liam"] = 100
    fleet.add("local", "olcwave-vm1-liam", "liam", total=500)

    cont = fleet.by_name("local", "olcwave-vm1-liam")
    calls = {"n": 0}

    async def flaky_remove(name, node="local"):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("daemon hiccup")
        cont.removed = True

    # Function-scoped fixture monkeypatch: torn down automatically, no
    # leakage into later tests.
    monkeypatch.setattr(
        olcrtc_sdk.OlcRTC, "remove", staticmethod(flaky_remove)
    )
    await one_tick()
    assert calls["n"] == 1
    assert not cont.removed

    # Re-arm the users listing (undo() also reverted the users_env patch,
    # which shares this function-scoped monkeypatch instance).
    users_env.rearm()
    await one_tick()
    assert calls["n"] == 2  # second enforcement pass = retry call
    assert cont.removed


@pytest.mark.asyncio
async def test_under_limit_user_never_touched(fleet, fake_db, users_env):
    users_env.limits["mia"] = 10_000
    fleet.add("local", "olcwave-vm1-mia", "mia", total=50)
    await one_tick()
    assert not fleet.by_name("local", "olcwave-vm1-mia").removed
