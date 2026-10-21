# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.
#
# A Remnawave user grants access to OLCWave subscriptions only when ALL of:
#   - the subscription lookup is found,
#   - status == ACTIVE (DISABLED is denied),
#   - expire_at is in the future (at evaluation time),
#   - the user belongs to RW_SQUAD_NAME when a squad is configured.
#
# sub_revoked_at is a credential-rotation marker, NOT a permanent ban:
# Remnawave's revoke action rotates the shortUuid (or passwords) and the
# user's CURRENT link keeps working while old links 404 at Remnawave itself.
# OLCWave therefore denies on the old short_uuid only because Remnawave
# no longer resolves it (not_found), and the rotation event invalidates the
# old cached olcrtc credentials: the reconciler removes containers for the
# old owner id; the valid user gets fresh containers/keys under the new
# short_uuid on the next subscription fetch.

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RwAccess:
    """Outcome of one Remnawave access evaluation for a short_uuid."""

    short_uuid: str
    allowed: bool
    reason: str
    username: str | None = None
    expires_at: datetime | None = None
    status: str | None = None
    sub_revoked_at: datetime | None = None

    @property
    def found(self) -> bool:
        return self.reason != "not_found"


REASON_MISSING = "not_found"
REASON_DISABLED = "status_disabled"
REASON_EXPIRED = "expired"
REASON_REVOKED = "subscription_revoked"
REASON_SQUAD = "not_in_squad"
REASON_UNAVAILABLE = "remnawave_unavailable"
REASON_OK = "ok"


class AccessUnavailableError(RuntimeError):
    """Raised when Remnawave cannot answer and no deny fact exists."""

    def __init__(self, short_uuid: str, detail: str):
        self.short_uuid = short_uuid
        self.detail = detail
        super().__init__(f"remnawave unavailable for {short_uuid}: {detail}")


def evaluate_user(user) -> RwAccess:
    """Apply the policy to a fetched Remnawave UserResponseDto.

    `user` carries the full user record (status, expire_at, sub_revoked_at,
    active_internal_squads) - the facts that the subscription-info endpoint
    alone does not expose (status, revocation).
    """
    now = datetime.now(timezone.utc)
    short_uuid = user.short_uuid

    status = str(getattr(user, "status", "") or "")
    if status.upper() != "ACTIVE":
        return RwAccess(short_uuid, False, REASON_DISABLED, user.username, _aware(user.expire_at), status, user.sub_revoked_at)

    expire_at = _aware(user.expire_at)
    if expire_at is None or expire_at <= now:
        return RwAccess(short_uuid, False, REASON_EXPIRED, user.username, expire_at, status, user.sub_revoked_at)

    # sub_revoked_at is recorded but never denied on by itself: Remnawave's
    # revoke action is a credential rotation (old shortUuid stops resolving
    # at Remnawave; a new current link works). Denial of an old link comes
    # from Remnawave returning not_found for it; the rotation event makes
    # the reconciler drop the old olcrtc containers so stale cached keys
    # die, while the valid user re-provisions under the new short_uuid.
    revoked_at = _aware(user.sub_revoked_at)

    if not is_user_in_squad(user):
        return RwAccess(short_uuid, False, REASON_SQUAD, user.username, expire_at, status, revoked_at)

    return RwAccess(short_uuid, True, REASON_OK, user.username, expire_at, status, revoked_at)


def is_user_in_squad(user) -> bool:
    if not settings.RW_SQUAD_NAME:
        return True

    squads = list(getattr(user, "active_internal_squads", None) or [])
    if not squads:
        return False

    return any(
        settings.RW_SQUAD_NAME == squad.name or settings.RW_SQUAD_NAME == str(squad.uuid)
        for squad in squads
    )


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def unavailable_grace() -> timedelta:
    seconds = getattr(settings, "REMNAWAVE_UNAVAILABLE_GRACE_SECONDS", 0)
    return timedelta(seconds=seconds)
