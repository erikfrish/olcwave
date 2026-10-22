# Copyright (C) 2026 invdevv - https://github.com/invdevv
# This file is part of olcwave.
# OLCWave is free software licensed under AGPL-3.0.

from uuid import UUID

from remnawave.models.users import GetAllUsersResponseDto, UserResponseDto
from remnawave.models.subscription import GetSubscriptionInfoResponseDto
from remnawave.models import SubscriptionInfoResponseDto
from remnawave import RemnawaveSDK
from remnawave.exceptions.general import NotFoundError

from config import settings
from rw.access import (
    AccessUnavailableError,
    RwAccess,
    REASON_MISSING,
    evaluate_user,
)


def make_uuid_optional(model) -> None:
    field = model.model_fields.get("uuid")
    if field is None:
        return

    field.default = None
    field.annotation = UUID | None
    model.model_rebuild(force=True)


def patch_remnawave_users() -> None:
    """Patch the Remnawave SDK models to make the `uuid` field optional in certain user-related response DTOs."""
    import remnawave.models as rw_models

    for name in dir(rw_models):
        if not name.startswith("GetUserBy"):
            continue
        model = getattr(rw_models, name)
        if isinstance(model, type) and "uuid" in getattr(model, "model_fields", {}):
            make_uuid_optional(model)

    # Remnawave 3.4.5 omits `uuid` in list responses; patch the item model
    # directly, then rebuild the wrapper.
    from remnawave.models.users import UserResponseDto
    if "uuid" in UserResponseDto.model_fields:
        make_uuid_optional(UserResponseDto)

    GetAllUsersResponseDto.model_rebuild(force=True)


patch_remnawave_users()

class RemnawaveUnavailable(RuntimeError):
    """Remnawave API did not answer with a usable response."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


_remnawave: RemnawaveSDK | None = None


def _get_sdk() -> RemnawaveSDK:
    global _remnawave
    if _remnawave is None:
        ca_bundle = getattr(settings, "RW_CA_BUNDLE", "") or None
        import httpx

        http_client = None
        if ca_bundle:
            http_client = httpx.AsyncClient(
                base_url=_prepare_base_url(settings.RW_API_URL),
                headers=_prepare_headers(settings.RW_API_TOKEN, settings.RW_API_URL),
                verify=ca_bundle,
                # A hung panel must surface as RemnawaveUnavailable (bounded
                # grace), never as an indefinite request stall.
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
        _remnawave = RemnawaveSDK(
            client=http_client,
            base_url=settings.RW_API_URL,
            token=settings.RW_API_TOKEN,
            caddy_token=settings.RW_CADDY_TOKEN or None,
        )
        if http_client is None:
            # SDK built its own client: still bound the request deadline so
            # a stalled panel becomes RemnawaveUnavailable, not a hang.
            _remnawave._client.timeout = httpx.Timeout(10.0, connect=5.0)
    return _remnawave


def _prepare_base_url(base_url: str) -> str:
    url = base_url
    if url.endswith("/"):
        url = url[:-1]
    if not url.endswith("/api"):
        url += "/api"
    return url


def _prepare_headers(token: str, base_url: str) -> dict:
    headers = {}
    if token:
        headers["Authorization"] = (
            token if token.startswith("Bearer ") else f"Bearer {token}"
        )
    if "http://" in base_url:
        headers["x-forwarded-proto"] = "https"
        headers["x-forwarded-for"] = "127.0.0.1"
    return headers

def _ensure_enabled():
    if not settings.RW_ENABLED:
        raise RuntimeError("Remnawave is not enabled")


async def getAllUsers() -> GetAllUsersResponseDto:
    _ensure_enabled()
    sdk = _get_sdk()
    PAGE_SIZE = 100

    start = 0
    users: list[UserResponseDto] = []

    while True:
        response = await sdk.users.get_all_users(
            start=start,
            size=PAGE_SIZE,
        )

        users.extend(response.users)

        if len(response.users) < PAGE_SIZE or len(users) >= response.total:
            break

        start += len(response.users)

    return GetAllUsersResponseDto(
        users=users,
        total=len(users),
    )


def isUserInSquad(user: UserResponseDto) -> bool:
    from rw.access import is_user_in_squad

    return is_user_in_squad(user)


async def getUserByShortUuid(short_uuid: str) -> UserResponseDto | None:
    """Fetch the full user record. None when Remnawave has no such user."""
    _ensure_enabled()
    sdk = _get_sdk()

    try:
        return await sdk.users.get_user_by_short_uuid(short_uuid)  # pyright: ignore[reportAssignmentType]
    except NotFoundError:
        return None


async def evaluateAccess(short_uuid: str) -> RwAccess:
    """Full access decision from the authoritative user record.

    Raises RemnawaveUnavailable when the panel cannot be reached; callers
    decide how long previously-granted access may ride on cached state.
    """
    _ensure_enabled()
    sdk = _get_sdk()

    try:
        user = await sdk.users.get_user_by_short_uuid(short_uuid)  # pyright: ignore[reportAssignmentType]
    except NotFoundError:
        return RwAccess(short_uuid, False, REASON_MISSING)
    except Exception as e:
        raise RemnawaveUnavailable(str(e) or e.__class__.__name__) from e

    return evaluate_user(user)


async def isUserValid(short_uuid: str) -> SubscriptionInfoResponseDto | None:
    """Backwards-compatible helper: subscription info for an allowed user.

    Returns None for a definite deny (missing, disabled, expired, revoked,
    squad-removed). Raises AccessUnavailableError when the panel cannot be
    reached, so callers never mistake an outage for a valid user.
    """
    access = await evaluateAccess(short_uuid)

    if not access.allowed:
        return None

    sdk = _get_sdk()
    try:
        sub: GetSubscriptionInfoResponseDto = await sdk.subscription.get_subscription_info_by_short_uuid(short_uuid)  # pyright: ignore[reportAssignmentType]
    except NotFoundError:
        return None
    except Exception as e:
        raise RemnawaveUnavailable(str(e) or e.__class__.__name__) from e

    if not sub.is_found:
        return None

    return sub


async def getSubscriptionSettings():
    _ensure_enabled()
    sdk = _get_sdk()

    sub = await sdk.subscriptions_settings.get_settings()

    return sub


__all__ = [
    "AccessUnavailableError",
    "RemnawaveUnavailable",
    "RwAccess",
    "evaluateAccess",
    "getAllUsers",
    "getSubscriptionSettings",
    "getUserByShortUuid",
    "isUserInSquad",
    "isUserValid",
    "make_uuid_optional",
    "patch_remnawave_users",
]
