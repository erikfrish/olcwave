# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
#
# Background traffic accounting and limit enforcement.
#
# Accounting is durable: per-container ABSOLUTE-byte baselines ("cursors")
# are persisted in the traffic_cursors table keyed by (node, container_id).
# The FIRST observation of a container counts its full absolute total - a
# new managed container's bytes are this user's quota usage, so a restart
# (new container id, counter from zero) can never bypass the cap by
# re-appearing as an "unobserved" container. Every later tick applies
# (total - baseline); a counter reset below the baseline (in-place restart)
# counts the bytes since restart. Baseline + user delta are written in ONE
# transaction, so a crash or DB failure loses nothing and double-counts
# nothing. Resetting a user's traffic zeroes the aggregate AND rebases every
# observable cursor to its current total in the same locked critical
# section, so pre-reset traffic can never be counted twice.
#
# Enforcement is fail-closed and multi-node: EVERY tick, EVERY local user at
# or over its cap is enforced independently of whether it produced a delta
# this tick (a capped user with zero traffic or a failed persist is still
# torn down, and failed removals are retried). Each configured node (local +
# OLCWAVE_NODES_JSON) is swept independently: a dead node never blocks
# enforcement on reachable nodes, and only label-owned containers of the
# exceeded user are stopped+removed on any node.

import asyncio
import logging

from database import async_session_factory

from olcrtc.sdk import OlcRTC
from olcrtc.service import Containers
from olcrtc import nodes
from settings.service import SettingsService
from users.db import TrafficCursorDB
from users.service import Users

logger = logging.getLogger(__name__)


def _usable_total(stats: dict) -> int | None:
    """Absolute byte total from a stats read, or None when unusable.

    NEVER fabricates a total: a stats read that answers nothing (missing
    file, exec failure -> {}) is not zero usage.
    """
    raw = stats.get("total_bytes")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return int(raw)


class TrafficManager:
    """Durable, restart-resistant traffic accounting and enforcement."""

    @staticmethod
    async def _sweep_node(node: str) -> dict[str, dict[tuple[str, str], int]]:
        """Sample every running owned container on one node.

        Returns {owner: {(node, container_id): absolute_total}}. The full
        64-hex container id differentiates same-named containers across
        daemons. A node listing failure returns {} - those owners' traffic
        is simply not observed this tick (no invented data) and their
        durable baselines stay untouched for the next successful pass.
        """
        samples: dict[str, dict[tuple[str, str], int]] = {}

        try:
            containers = await OlcRTC.all(include_stopped=False, node=node)
        except Exception as exc:
            logger.error("traffic: node %s listing failed: %s", node, exc)
            return samples

        for cont in containers:
            try:
                info = await cont.show()
            except Exception:
                logger.exception("traffic: inspect failed on %s", node)
                continue

            parsed = Containers.parse_panel_labels(info)
            if parsed is None:
                continue
            owner = parsed[1]

            if info["State"]["Status"] != "running":
                continue

            container_id = info["Id"]
            if not container_id:
                continue

            try:
                stats = await OlcRTC.get_stats(
                    info["Name"].lstrip("/"), node=node
                )
            except Exception:
                logger.exception(
                    "traffic: stats failed for %s on %s",
                    info["Name"], node,
                )
                continue

            total = _usable_total(stats)
            if total is None:
                # Unusable stats: skip the sample; this container's stored
                # baseline stays stale so the unobserved gap is billed when
                # stats return.
                logger.warning(
                    "traffic: no usable total for %s on %s; sample skipped",
                    info["Name"], node,
                )
                continue

            samples.setdefault(owner, {})[(node, container_id)] = total

        return samples

    @staticmethod
    async def _sweep_all() -> dict[str, dict[tuple[str, str], int]]:
        """Sample ALL nodes independently; merge per owner."""
        all_samples: dict[str, dict[tuple[str, str], int]] = {}
        for node in nodes.names():
            for owner, per_cont in (
                await TrafficManager._sweep_node(node)
            ).items():
                all_samples.setdefault(owner, {}).update(per_cont)
        return all_samples

    @staticmethod
    async def _account_owner(
        short_uuid: str,
        per_cont: dict[tuple[str, str], int],
    ) -> None:
        """Apply one owner's deltas + baselines (per-user lock held).

        Cursor reads and the delta+baseline write run in ONE session/one
        transaction inside the same per-user critical section as reset and
        delete, so a concurrent reset can never race a stale baseline read
        and a crash can neither lose nor double-count. First observation of
        a container counts its full absolute total (a new managed
        container's bytes are quota usage; a restart creates a new id and
        cannot bypass the cap).
        """
        async with async_session_factory() as db:
            delta = 0
            cursors: list[tuple[str, str, int]] = []

            for (node, container_id), total in per_cont.items():
                cursors.append((node, container_id, total))
                row = await TrafficCursorDB.get_cursor(
                    db, node, container_id
                )

                if row is None:
                    delta += total
                    continue

                previous = int(row.last_total)
                if total >= previous:
                    delta += total - previous
                else:
                    # Counter reset below baseline (in-place container
                    # restart): bytes since restart are new usage; baseline
                    # follows.
                    delta += total

            await TrafficCursorDB.add_delta_with_cursors(
                db, short_uuid, delta, cursors
            )

    @staticmethod
    async def reset_user_accounting(short_uuid: str) -> None:
        """Zero the user's aggregate and rebase baselines (per-user lock).

        Runs under the SAME per-user critical section as provisioning,
        revocation and the accounting tick. In one transaction:
        traffic_used_bytes = 0, every OBSERVED running container's cursor
        baseline is set to its current absolute total (counting restarts
        from now, history is never re-billed), and cursor rows of
        unobservable containers are dropped (a container that reappears is
        a first sample again - deterministic).
        """
        from subscriptions.service import acquire_user_lock, release_user_lock

        await acquire_user_lock(short_uuid)
        try:
            observed: list[tuple[str, str, int]] = []

            for node in nodes.names():
                try:
                    containers = await OlcRTC.all(
                        include_stopped=True, node=node
                    )
                except Exception as exc:
                    logger.error(
                        "traffic: node %s listing failed during reset of "
                        "%s: %s", node, short_uuid, exc,
                    )
                    continue

                for cont in containers:
                    try:
                        info = await cont.show()
                    except Exception:
                        logger.exception(
                            "traffic: inspect failed on %s during reset",
                            node,
                        )
                        continue

                    parsed = Containers.parse_panel_labels(info)
                    if parsed is None or parsed[1] != short_uuid:
                        continue
                    if info["State"]["Status"] != "running":
                        continue

                    try:
                        stats = await OlcRTC.get_stats(
                            info["Name"].lstrip("/"), node=node
                        )
                    except Exception:
                        logger.exception(
                            "traffic: stats failed for %s during reset",
                            info["Name"],
                        )
                        continue

                    total = _usable_total(stats)
                    if total is not None:
                        observed.append((node, info["Id"], total))

            async with async_session_factory() as db:
                await TrafficCursorDB.reset_traffic_with_baselines(
                    db, short_uuid, observed
                )
        finally:
            release_user_lock(short_uuid)

    @staticmethod
    async def _stop_user_containers(short_uuid: str) -> None:
        """Stop+remove the user's label-owned containers on EVERY node.

        Independent per node and per container: a dead node or a failed
        removal is logged and retried on the next tick; it never aborts the
        others. OlcRTC.stop/remove are label-guarded, so foreign containers
        are never touched even on a name collision.
        """
        for node in nodes.names():
            try:
                containers = await OlcRTC.all(include_stopped=True, node=node)
            except Exception as exc:
                logger.error(
                    "traffic: node %s listing failed during enforcement "
                    "of %s: %s", node, short_uuid, exc,
                )
                continue

            for cont in containers:
                try:
                    info = await cont.show()
                except Exception:
                    logger.exception(
                        "traffic: inspect failed on %s during enforcement",
                        node,
                    )
                    continue

                parsed = Containers.parse_panel_labels(info)
                if parsed is None or parsed[1] != short_uuid:
                    continue

                name = info["Name"].lstrip("/")
                try:
                    await OlcRTC.stop(name, node=node)
                    await OlcRTC.remove(name, node=node)
                except Exception:
                    logger.exception(
                        "traffic: failed to stop/remove %s on %s for %s; "
                        "will retry next tick",
                        name, node, short_uuid,
                    )

    @staticmethod
    async def _enforce_capped_users() -> None:
        """Tear down EVERY local user at/over cap, every tick.

        Deliberately independent of this tick's deltas: a capped user with
        zero new traffic, a failed persist, or a node outage is still
        enforced, and previously failed removals are retried.
        """
        from subscriptions.service import acquire_user_lock, release_user_lock

        try:
            users = await Users.get_all()
        except Exception:
            logger.exception("traffic: cannot list users for enforcement")
            return

        for user in users:
            if user.traffic_limit_bytes <= 0:
                continue
            if user.traffic_used_bytes < user.traffic_limit_bytes:
                continue

            short_uuid = user.short_uuid
            try:
                # Same per-user critical section as provisioning and
                # revocation: a cap teardown can never interleave with a
                # subscription request re-creating this user's containers.
                await acquire_user_lock(short_uuid)
                try:
                    await TrafficManager._stop_user_containers(short_uuid)
                finally:
                    release_user_lock(short_uuid)
            except Exception:
                logger.exception(
                    "traffic: enforcement failed for %s", short_uuid
                )
                continue

    @staticmethod
    async def _tick() -> None:
        from subscriptions.service import acquire_user_lock, release_user_lock

        # 1. Accounting: per owner, compute + persist under the per-user
        #    lock (cursor reads, delta and baseline write are one critical
        #    section vs reset/delete).
        samples = await TrafficManager._sweep_all()
        for short_uuid, per_cont in samples.items():
            try:
                await acquire_user_lock(short_uuid)
                try:
                    await TrafficManager._account_owner(short_uuid, per_cont)
                finally:
                    release_user_lock(short_uuid)
            except Exception:
                # One broken user must not stall the others' accounting.
                logger.exception(
                    "traffic: accounting failed for %s", short_uuid
                )
                continue

        # 2. Enforcement: every capped user, every tick.
        await TrafficManager._enforce_capped_users()

    @staticmethod
    async def run():
        while True:
            try:
                await TrafficManager._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("traffic: tick failed")
            await asyncio.sleep(
                SettingsService.get().traffic_collect_interval
            )
