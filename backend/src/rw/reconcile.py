# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
#
# One policy object for Remnawave access decisions (rw.policy) plus a single
# per-user state machine shared by the request path (SubscriptionGuard) and
# the background reconciler (ReconcileManager).
#
# Every REVOCATION_CHECK_INTERVAL seconds each user with running olcwave
# containers is re-evaluated against the Remnawave user record. On a definite
# deny (missing, disabled, expired, squad-removed) every olcwave container
# owned by that short_uuid is REMOVED immediately - the config with the
# crypto key never keeps running. A subscription revoke in Remnawave rotates
# credentials: the old shortUuid stops resolving there (not_found -> removed
# here), and a revoke-with-kept-shortUuid is caught by the sub_revoked_at
# generation watcher - the rotation event removes this user's containers
# once, so cached old olcrtc keys die while the still-valid user re-provisions
# fresh keys on the next subscription fetch. On Remnawave outage a bounded
# grace window applies (REMNAWAVE_UNAVAILABLE_GRACE_SECONDS, persisted in
# users.access_state via rw.policy so a restart cannot extend it); past the
# window containers are removed too, so no indefinite stale access exists.
# Valid users are never touched between rotations: reconcile only removes,
# never restarts. Container ownership is the strict `olcwave-<tag>-<owner>`
# name scheme plus management labels; no assumption is made about the owner.

import asyncio
import logging

from config import settings
from rw.policy import (
    Decision,
    UserAccessLedger,
    evaluate,
    grace_seconds,
)
from rw import policy
from rw.sdk import RemnawaveUnavailable
from olcrtc.sdk import OlcRTC
from olcrtc.service import Containers

logger = logging.getLogger(__name__)




async def panel_owned_containers(short_uuid: str | None = None) -> list:
    """Panel-managed containers, decided by management LABELS.

    A container counts as panel-owned only when it carries
    `olcwave.managed=true` plus a non-empty `olcwave.config_tag` and a
    non-empty `olcwave.owner` (written by OlcRTC.run and preserved on
    restart). With short_uuid given, only that owner's containers. The owner
    is matched verbatim against the labels - no alphabet, dash or length
    assumption (Remnawave shortUuids may contain '-'/'_' and vary in
    length). Unmarked containers are NEVER classified by name, so foreign
    containers that merely share the `olcwave-` prefix (ops tooling, an
    isolated postgres) can never be touched.
    """
    owned = []
    for cont in await OlcRTC.all(True):
        info = await cont.show()
        parsed = Containers.parse_panel_labels(info)
        if parsed is None:
            continue
        owner = parsed[1]
        if short_uuid is not None and owner != short_uuid:
            continue
        owned.append(cont)
    return owned


async def revoke_user_unchecked(short_uuid: str, reason: str) -> int:
    """UNLOCKED removal of every olcwave container owned by short_uuid.

    MUST be called while holding the per-user lock for short_uuid
    (subscriptions.service.acquire_user_lock). Returns the removed count.
    """
    removed = 0
    for cont in await panel_owned_containers(short_uuid):
        name = (await cont.show())["Name"].lstrip("/")
        try:
            await OlcRTC.remove(name)
            removed += 1
        except Exception:
            logger.exception("revoke: failed to remove container %s", name)
    if removed:
        logger.warning(
            "revocation: removed %d container(s) of %s (reason=%s)",
            removed, short_uuid, reason,
        )
    return removed


class ReconcileManager:
    """Background loop enforcing Remnawave decisions on live containers.

    Each per-user pass takes the SAME per-user lock as the subscription
    request path and performs, inside that one critical section: the
    authoritative evaluate, the persisted ledger update (last_verified /
    rotation generation), and any removal. No mutation ever happens outside
    the lock, and no removal failure is ever recorded as handled.
    """

    _task: asyncio.Task | None = None

    @staticmethod
    async def revoke_user(short_uuid: str, reason: str) -> int:
        """Remove every panel container owned by short_uuid. Returns count.

        Locking variant for callers outside a per-user critical section
        (router /check, users delete). Raises the last removal error when a
        container could not be removed - never swallowed.
        """
        from subscriptions.service import acquire_user_lock, release_user_lock

        await acquire_user_lock(short_uuid)
        try:
            return await revoke_user_unchecked(short_uuid, reason)
        finally:
            release_user_lock(short_uuid)

    @staticmethod
    async def reconcile_once() -> dict:
        """One sweep over all users owning panel containers."""
        owners: set[str] = set()
        for cont in await panel_owned_containers():
            info = await cont.show()
            owners.add(Containers.parse_panel_labels(info)[1])

        result = {
            "checked": 0,
            "revoked": 0,
            "allowed": 0,
            "unavailable": 0,
        }

        for short_uuid in sorted(owners):
            result["checked"] += 1
            outcome = await ReconcileManager._evaluate(short_uuid)
            result[outcome] += 1  # pyright: ignore[reportArgumentType]

        return result

    @staticmethod
    async def _evaluate(short_uuid: str) -> str:
        """Evaluate one owner. Returns 'allowed' | 'revoked' | 'unavailable'.

        Runs entirely inside the per-user lock: evaluate -> ledger -> removal.
        """
        from subscriptions.service import acquire_user_lock, release_user_lock

        await acquire_user_lock(short_uuid)
        try:
            try:
                decision = await evaluate(short_uuid)
            except RemnawaveUnavailable as e:
                return await ReconcileManager._evaluate_unavailable(
                    short_uuid, str(e)
                )

            await UserAccessLedger.mark_verified(short_uuid)

            return await ReconcileManager._apply_decision(short_uuid, decision)
        finally:
            release_user_lock(short_uuid)

    @staticmethod
    async def _apply_decision(short_uuid: str, decision) -> str:
        """Apply an authoritative decision (lock held)."""
        # Credential-rotation watcher: Remnawave's revoke action stamps
        # sub_revoked_at. Whether it rotated the shortUuid (old link 404s ->
        # denied as not_found here) or kept it, the rotation itself
        # invalidates every olcrtc key this panel issued BEFORE it. Remove
        # every container created before the rotation epoch; the generation
        # is persisted as applied ONLY after all old-generation containers
        # are confirmed gone, so a failed removal is retried by every later
        # sweep instead of being marked handled. The still-valid user
        # re-provisions fresh keys/containers on their next subscription
        # fetch (recovery works; current-link recovery is unaffected).
        if decision.access is not None and decision.access.sub_revoked_at is not None:
            gen = decision.access.sub_revoked_at.timestamp()
            applied = await UserAccessLedger.get_applied_revoke_gen(short_uuid)
            if applied is None or applied < gen:
                await ReconcileManager._remove_old_generation(
                    short_uuid, gen, "subscription_revoked_rotation"
                )
                return "revoked"

        if decision.allowed:
            return "allowed"

        await revoke_user_unchecked(short_uuid, decision.reason)
        return "revoked"

    @staticmethod
    async def _remove_old_generation(
        short_uuid: str, gen: float, reason: str
    ) -> None:
        """Remove every panel container created before `gen` (lock held).

        Persistence order: removals first, applied-generation stamp last and
        only on full success. Any failure raises - the caller's sweep logs it
        and the next sweep retries the same generation. A container created
        AFTER the rotation (fresh keys) is never touched.
        """
        errors: list[str] = []
        for cont in await panel_owned_containers(short_uuid):
            info = await cont.show()
            parsed = Containers.parse_panel_labels(info)
            if parsed is None:
                continue
            name = info["Name"].lstrip("/")
            created_epoch = Containers.created_epoch(info)
            if created_epoch >= gen:
                continue  # fresh post-rotation issuance: keep
            try:
                await OlcRTC.remove(name)
            except Exception as e:
                errors.append(f"{name}: {e}")
                logger.exception(
                    "rotation: failed to remove old-generation container %s",
                    name,
                )
        if errors:
            raise RuntimeError(
                f"rotation {reason} for {short_uuid}: "
                f"{len(errors)} container removal(s) failed; "
                "generation NOT applied - will retry: " + "; ".join(errors)
            )
        await UserAccessLedger.mark_rotation_applied(short_uuid, gen)

    @staticmethod
    async def _evaluate_unavailable(short_uuid: str, detail: str) -> str:
        """Outage handling (lock held) with the persisted bounded grace."""
        lv = await UserAccessLedger.get_last_verified(short_uuid)

        if UserAccessLedger.within_grace(lv):
            logger.warning(
                "remnawave unavailable for %s (%s); within %ss persisted "
                "grace - no mutation, cached state only",
                short_uuid, detail, grace_seconds(),
            )
            return "unavailable"

        # Bounded grace exhausted (or never verified): remove this user's
        # containers. last_verified stays whatever it is - no ledger write can
        # restart the window, so later sweeps and restarts stay denied until
        # Remnawave answers again (no indefinite stale access).
        await revoke_user_unchecked(
            short_uuid, "remnawave_unreachable_past_grace"
        )
        return "revoked"

    @staticmethod
    async def _run():
        interval = getattr(settings, "REVOCATION_CHECK_INTERVAL", 30)
        while True:
            try:
                summary = await ReconcileManager.reconcile_once()
                if summary["revoked"] or summary["checked"]:
                    logger.info("reconcile: %s", summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ReconcileManager sweep failed")
            await asyncio.sleep(interval)

    @staticmethod
    def start():
        if ReconcileManager._task is not None:
            return
        ReconcileManager._task = asyncio.create_task(ReconcileManager._run())

    @staticmethod
    async def stop():
        if ReconcileManager._task is None:
            return
        ReconcileManager._task.cancel()
        try:
            await ReconcileManager._task
        except asyncio.CancelledError:
            pass
        ReconcileManager._task = None


class SubscriptionGuard:
    """Request-path access decision for GET /sub and /sub/{uuid}/check.

    Thin delegate to rw.policy.SubscriptionGuard: the single authoritative
    evaluate with the persisted bounded grace ledger. Returned Decision
    carries the RwAccess record when the panel answered; during an outage
    within grace access is None (cached state only, no mutation).
    """

    @staticmethod
    async def check(short_uuid: str) -> Decision:
        return await policy.SubscriptionGuard.check(short_uuid)
