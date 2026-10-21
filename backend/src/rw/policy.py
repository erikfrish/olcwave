# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
#
# One policy object for Remnawave access decisions plus a small persisted
# per-user ledger shared by the request path (SubscriptionGuard,
# Subscriptions.get) and the background reconciler (ReconcileManager).
#
# Deny facts (missing / disabled / expired / squad-removed) deny access and
# remove the user's olcwave containers. sub_revoked_at is a credential-
# rotation marker: it never denies by itself; a NEW sub_revoked_at invalidates
# every olcrtc key issued BEFORE it (per-container creation time is compared
# against the rotation timestamp), and the rotation generation is persisted
# ONLY after all old-generation containers are confirmed gone.
#
# Remnawave outage: the grace window is `last_verified + GRACE_SECONDS`,
# where last_verified is the wall-clock epoch of the last successful
# authoritative evaluation, persisted in users.access_state. The window is
# therefore stable across restarts, can never be extended by a request or a
# restart, and within it ONLY cached existing state may be served - no
# container is created or mutated. Past the window everything is denied and
# the reconciler removes the user's containers.

import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from config import settings
from database import async_session_factory
from users.models import User
from rw.sdk import RemnawaveUnavailable, evaluateAccess

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    """One access decision, shared by request path and reconciler.

    allowed=True means: serve cached subscription content (existing
    containers/configs). Mutating (creating/starting containers) is only
    allowed when `access` is set, i.e. the panel answered authoritatively.
    """

    allowed: bool
    reason: str
    access: object | None = None  # RwAccess when the panel answered

async def evaluate(short_uuid: str) -> Decision:
    """One evaluation of the authoritative user record.

    Raises RemnawaveUnavailable when the panel cannot be reached; callers
    apply the persisted grace ledger below.
    """
    access = await evaluateAccess(short_uuid)
    if access.allowed:
        return Decision(True, access.reason, access)
    return Decision(False, access.reason, access)

def grace_seconds() -> int:
    return getattr(settings, "REMNAWAVE_UNAVAILABLE_GRACE_SECONDS", 0)


class UserAccessLedger:
    """Per-user access state persisted in the local DB (users.access_state).

    State JSON: {"last_verified": float|None, "applied_revoke_gen": float|None}

    - last_verified: epoch of the last successful authoritative evaluation.
      Anchors the bounded outage window: cached state may be served only while
      now - last_verified <= REMNAWAVE_UNAVAILABLE_GRACE_SECONDS. Wall-clock
      epoch so the bound survives restarts; a restart can never extend it.
    - applied_revoke_gen: sub_revoked_at epoch whose old-generation containers
      are CONFIRMED removed. Written only after removal succeeded; a failed
      removal leaves it unset so every sweep retries the same generation.
    """

    _CACHE: dict[str, dict] = {}

    @staticmethod
    async def _state(short_uuid: str) -> dict:
        cached = UserAccessLedger._CACHE.get(short_uuid)
        if cached is not None:
            return cached
        raw = None
        try:
            async with async_session_factory() as db:
                row = await db.scalar(
                    select(User).where(User.short_uuid == short_uuid)
                )
            raw = getattr(row, "access_state", None) if row is not None else None
        except Exception:
            logger.exception(
                "access ledger: cannot read persisted state for %s", short_uuid
            )
        if isinstance(raw, dict):
            state = {
                "last_verified": raw.get("last_verified"),
                "applied_revoke_gen": raw.get("applied_revoke_gen"),
            }
        else:
            state = {"last_verified": None, "applied_revoke_gen": None}
        UserAccessLedger._CACHE[short_uuid] = state
        return state

    @staticmethod
    async def _persist(short_uuid: str, state: dict) -> None:
        try:
            async with async_session_factory() as db:
                row = await db.scalar(
                    select(User).where(User.short_uuid == short_uuid)
                )
                if row is None:
                    return
                row.access_state = dict(state)
                await db.commit()
        except Exception:
            # Persistence is best-effort for availability; the in-process
            # cache still bounds the window in the running process, and a
            # failed write can only make the NEXT process re-verify (never
            # extend access past the bound).
            logger.exception(
                "access ledger: cannot persist state for %s", short_uuid
            )

    @staticmethod
    async def get_last_verified(short_uuid: str) -> float | None:
        state = await UserAccessLedger._state(short_uuid)
        lv = state.get("last_verified")
        return float(lv) if lv is not None else None

    @staticmethod
    async def mark_verified(short_uuid: str) -> None:
        state = await UserAccessLedger._state(short_uuid)
        state["last_verified"] = time.time()
        UserAccessLedger._CACHE[short_uuid] = state
        await UserAccessLedger._persist(short_uuid, state)

    @staticmethod
    async def get_applied_revoke_gen(short_uuid: str) -> float | None:
        state = await UserAccessLedger._state(short_uuid)
        gen = state.get("applied_revoke_gen")
        return float(gen) if gen is not None else None

    @staticmethod
    async def mark_rotation_applied(short_uuid: str, gen: float) -> None:
        """Persist the rotation generation as fully applied.

        Called ONLY after every container created before `gen` is confirmed
        gone, so a failed removal never gets recorded as handled.
        """
        state = await UserAccessLedger._state(short_uuid)
        current = state.get("applied_revoke_gen")
        if current is not None and float(current) >= gen:
            return
        state["applied_revoke_gen"] = gen
        UserAccessLedger._CACHE[short_uuid] = state
        await UserAccessLedger._persist(short_uuid, state)
        logger.info(
            "rotation generation %.0f applied for %s (old keys invalidated)",
            gen, short_uuid,
        )

    @staticmethod
    def within_grace(last_verified: float | None, now: float | None = None) -> bool:
        """True when cached state may still be served during an outage."""
        if last_verified is None:
            return False
        now = time.time() if now is None else now
        return now - last_verified <= grace_seconds()


class SubscriptionGuard:
    """Request-path access decision helpers.

    Deny facts (from Remnawave) win immediately. An outage lets a request
    serve CACHED STATE only while now <= last_verified + grace (persisted);
    past it, and for any deny fact, the request is denied. Never invents
    access.
    """

    @staticmethod
    async def outage_cached_ok(short_uuid: str) -> bool:
        """Whether an outage may still be bridged with cached state."""
        lv = await UserAccessLedger.get_last_verified(short_uuid)
        ok = UserAccessLedger.within_grace(lv)
        if ok:
            logger.warning(
                "remnawave unreachable for %s; within %ss persisted grace of "
                "last verify - cached state only, no mutation",
                short_uuid, grace_seconds(),
            )
        else:
            logger.error(
                "remnawave unreachable for %s and no valid persisted grace - "
                "denying", short_uuid,
            )
        return ok

    @staticmethod
    async def check(short_uuid: str) -> Decision:
        try:
            decision = await evaluate(short_uuid)
        except RemnawaveUnavailable:
            ok = await SubscriptionGuard.outage_cached_ok(short_uuid)
            if ok:
                return Decision(
                    True, "remnawave_unreachable_within_grace", None
                )
            return Decision(False, "remnawave_unreachable", None)

        await UserAccessLedger.mark_verified(short_uuid)
        return decision
