from fastapi import APIRouter, HTTPException, Response, Request
from config import settings
from settings.service import SettingsService
from subscriptions.service import Subscriptions
from users.service import Users

router = APIRouter(prefix="/sub", tags=["subscriptions"])


@router.get("/{short_uuid}/check")
async def get_provider_name(short_uuid: str):
    if settings.RW_ENABLED:
        from rw.reconcile import SubscriptionGuard, ReconcileManager

        decision = await SubscriptionGuard.check(short_uuid)
        if not decision.allowed:
            try:
                if decision.access is not None:
                    # Definite deny fact: revoke owned containers. During an
                    # outage there is nothing authoritative to act on and
                    # nothing is mutated.
                    await ReconcileManager.revoke_user(
                        short_uuid,
                        f"subscription_check_denied: {decision.reason}",
                    )
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
async def get(short_uuid: str, request: Request):
    sub = await Subscriptions.get(short_uuid)
    # Ghostlane parses each text line as a connection. OLCBox metadata
    # comments would appear as bogus unsupported profiles in that client.
    if sub.status_code == 200 and "ghostlane" in request.headers.get("user-agent", "").lower():
        lines = [line for line in sub.body.decode().splitlines()
                 if line.startswith(("olcrtc://", "vless://", "hysteria2://"))]
        headers = dict(sub.headers)
        headers.pop("content-length", None)
        headers.update({"Cache-Control": "private, no-store", "Vary": "User-Agent"})
        return Response(content="\n".join(lines) + "\n", media_type="text/plain",
                        headers=headers)
    sub.headers["Cache-Control"] = "private, no-store"
    sub.headers["Vary"] = "User-Agent"
    return sub
