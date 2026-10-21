from fastapi import APIRouter, HTTPException, Response
from config import settings
from settings.service import SettingsService
from subscriptions.service import Subscriptions
from users.service import Users

router = APIRouter(prefix="/sub", tags=["subscriptions"])


@router.get("/{short_uuid}/check")
async def get_provider_name(short_uuid: str):
    if settings.RW_ENABLED:
        from rw.reconcile import SubscriptionGuard, ReconcileManager

        allowed, _reason = await SubscriptionGuard.check(short_uuid)
        if not allowed:
            try:
                await ReconcileManager.revoke_user(short_uuid, "subscription_check_denied")
            except Exception:
                pass
            raise HTTPException(status_code=404, detail="Not found")
    else:
        try:
            await Users.get(short_uuid)
        except HTTPException:
            raise HTTPException(status_code=404, detail="Not found")

    name = SettingsService.get().sub_name

    return Response(
        content=name,
        media_type="text/plain"
    )


@router.get("/{short_uuid}")
async def get(short_uuid: str):
    sub = await Subscriptions.get(short_uuid)

    return sub
