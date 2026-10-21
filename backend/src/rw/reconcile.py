# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
#
# Continuous background reconciliation of Remnawave access.
#
# Every REVOCATION_CHECK_INTERVAL seconds each user with running olcwave
# containers is re-evaluated against the Remnawave user record. On a definite
# deny (missing, disabled, expired, revoked, squad-removed) every olcwave
# container owned by that short_uuid is REMOVED immediately - the config with
# the crypto key never keeps running. On Remnawave outage a bounded grace
# window applies (REMNAWAVE_UNAVAILABLE_GRACE_SECONDS since the last
# successful evaluation); past the window containers are removed too, so no
# indefinite stale access exists. Valid users are never touched: reconcile
# only removes, it never restarts or re-creates.
#
# Startup reconciliation runs the same sweep once before the API accepts
# traffic, so containers restored by a crash/upgrade get evaluated too.

import asyncio
import logging
import time
from datetime import datetime, timezone

from config import settings
from rw.sdk import RemnawaveUnavailable, evaluateAccess
from rw.access import REASON_MISSING
from olcrtc.sdk import OlcRTC
from olcrtc.service import Containers
from users.service import Users

logger = logging.getLogger(__name__)


class ReconcileManager:
    """Background loop enforcing Remnawave decisions on live containers."""

    _task: asyncio.Task | None = None

    # short_uuid -> monotonic deadline of the outage grace window
    _grace_deadline: dict[str, float] = {}

    @staticmethod
    async def owned_container_names(short_uuid: str) -> list[str]:
        """Names of olcwave containers owned by short_uuid (any state)."""
        names: list[str] = []
        for cont in await OlcRTC.all(True):
            info = await cont.show()
            name = info["Name"].lstrip("/")
            if not await Containers.is_panel_container(cont):
                continue
            if name.endswith(f"-{short_uuid}"):
                names.append(name)
        return names

    @staticmethod
    async def revoke_user(short_uuid: str, reason: str) -> int:
        """Remove every olcwave container owned by short_uuid. Returns count."""
        removed = 0
        for name in await ReconcileManager.owned_container_names(short_uuid):
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

    @staticmethod
    async def reconcile_once() -> dict:
        """One sweep over all users owning olcwave containers."""
        owners: set[str] = set()
        for cont in await OlcRTC.all(True):
            info = await cont.show()
            name = info["Name"].lstrip("/")
            if not await Containers.is_panel_container(cont):
                continue
            parts = name.split("-", 2)
            if len(parts) == 3 and parts[2]:
                owners.add(parts[2])

        result = {
            "checked": 0,
            "revoked": 0,
            "allowed": 0,
            "unavailable": 0,
        }

        for short_uuid in sorted(owners):
            result["checked"] += 1
            outcome = await ReconcileManager._evaluate(short_uuid)

            if outcome == "allowed":
                result["allowed"] += 1
                ReconcileManager._grace_deadline.pop(short_uuid, None)
            elif outcome == "revoked":
                result["revoked"] += 1
                ReconcileManager._grace_deadline.pop(short_uuid, None)
            else:
                result["unavailable"] += 1

        return result

    @staticmethod
    async def _evaluate(short_uuid: str) -> str:
        """Evaluate one owner. Returns 'allowed' | 'revoked' | 'unavailable'."""
        try:
            access = await evaluateAccess(short_uuid)
        except RemnawaveUnavailable as e:
            grace = getattr(settings, "REMNAWAVE_UNAVAILABLE_GRACE_SECONDS", 0)
            now = time.monotonic()
            deadline = ReconcileManager._grace_deadline.get(short_uuid)
            if deadline is None:
                ReconcileManager._grace_deadline[short_uuid] = now + grace
                logger.warning(
                    "remnawave unavailable for %s (%s); grace %ss started",
                    short_uuid, e.detail, grace,
                )
                return "unavailable"
            if now < deadline:
                return "unavailable"
            logger.error(
                "remnawave unavailable for %s longer than %ss grace - revoking",
                short_uuid, grace,
            )
            await ReconcileManager.revoke_user(short_uuid, "remnawave_unreachable_past_grace")
            ReconcileManager._grace_deadline.pop(short_uuid, None)
            return "revoked"

        if access.allowed:
            return "allowed"

        await ReconcileManager.revoke_user(short_uuid, access.reason)
        return "revoked"

    @staticmethod
    async def _run():
        interval = getattr(settings, "REVOCATION_CHECK_INTERVAL", 30)
        # first pass right away: startup reconciliation
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
    """Synchronous-with-request access decision for GET /sub.

    Deny facts (from Remnawave) win immediately. An outage lets a request
    through only within the same bounded grace window the reconciler uses;
    past it the subscription is denied. Never invents access.
    """

    _last_ok: dict[str, float] = {}

    @staticmethod
    async def check(short_uuid: str) -> tuple[bool, str]:
        from rw.sdk import RemnawaveUnavailable

        try:
            access = await evaluateAccess(short_uuid)
        except RemnawaveUnavailable as e:
            grace = getattr(settings, "REMNAWAVE_UNAVAILABLE_GRACE_SECONDS", 0)
            last = SubscriptionGuard._last_ok.get(short_uuid)
            now = time.monotonic()
            if last is not None and now - last <= grace:
                logger.warning(
                    "subscription guard: remnawave unreachable for %s (%s), "
                    "within %ss grace of last success - denying mutation but "
                    "serving cached state",
                    short_uuid, e.detail, grace,
                )
                return True, "remnawave_unreachable_within_grace"
            return False, "remnawave_unreachable"

        if access.allowed:
            SubscriptionGuard._last_ok[short_uuid] = time.monotonic()
            return True, access.reason
        return False, access.reason
