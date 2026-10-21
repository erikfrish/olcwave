import asyncio
import logging
import secrets
from typing import Any

import emoji
import yaml

from fastapi import Response

from config import settings
from settings.service import SettingsService
from users.schemas import TrafficInfoSchema, UserSchema
from olcrtc.sdk import OlcRTC
from profiles.roomGenerator import RoomChecker, RoomGenerator
from profiles.service import Containers
from profiles.service import Profiles
from users.service import Users
import rw.reconcile as reconcile_module
from rw.reconcile import ReconcileManager, SubscriptionGuard

# Boring per-user lock: one plain asyncio.Lock per short_uuid. NOT reentrant.
# Every entry into a per-user critical section takes it exactly once; helpers
# that must run under the lock (revocation removal) are provided unlocked and
# are called only while the lock is held, so no held-set cleverness is needed
# and an inner release can never unlock an outer critical section.
_profile_locks: dict[str, asyncio.Lock] = {}


def _lock_for(short_uuid: str) -> asyncio.Lock:
    lock = _profile_locks.get(short_uuid)
    if lock is None:
        lock = asyncio.Lock()
        _profile_locks[short_uuid] = lock
    return lock


async def acquire_user_lock(short_uuid: str) -> None:
    """Take the per-user lock exactly once per critical section."""
    await _lock_for(short_uuid).acquire()


def release_user_lock(short_uuid: str) -> None:
    """Release the per-user lock; must pair 1:1 with acquire_user_lock."""
    lock = _profile_locks.get(short_uuid)
    if lock is not None and lock.locked():
        lock.release()


async def _ensure_locked(short_uuid: str):
    """Run the ensure step under the per-user lock (no re-verification)."""
    await acquire_user_lock(short_uuid)
    try:
        return await Subscriptions.ensure_profiles_running(short_uuid)
    finally:
        release_user_lock(short_uuid)


TRANSPORT_NAMES = {
    "vp8channel": "vp8",
    "seichannel": "sei",
    "videochannel": "video",
}

TRANSPORT_OPTIONS = {
    "vp8": {
        "fps": "vp8-fps",
        "batch_size": "vp8-batch",
    },
    "sei": {
        "fps": "fps",
        "batch_size": "batch",
        "fragment_size": "frag",
        "ack_timeout_ms": "ack-ms",
    },
    "video": {
        "width": "video-w",
        "height": "video-h",
        "fps": "video-fps",
        "bitrate": "video-bitrate",
        "hw": "video-hw",
        "codec": "video-codec",
        "qr_size": "video-qr-size",
        "qr_recovery": "video-qr-recovery",
        "tile_module": "video-tile-module",
        "tile_rs": "video-tile-rs",
    },
}


def bytes_to_notation(num: float):
    notations = ["b", "kb", "mb", "gb", "tb"]
    ptr = 0
    while num > 1000 and ptr < len(notations) - 1:
        num /= 1000
        ptr += 1

    return f"{int(num)}{notations[ptr]}"


class Subscriptions:
    @staticmethod
    def remove_last_emoji(s: str) -> tuple[str, str]:
        matches = list(emoji.emoji_list(s))
        if not matches:
            return s, ""

        last = matches[-1]

        return (
            s[:last["match_start"]] + s[last["match_end"]:],
            last["emoji"],
        )

    @staticmethod
    async def profile_to_config(profile: str):
        config = yaml.safe_load(profile)
        config.pop("data", None)
        config.setdefault("crypto", {})["key"] = secrets.token_hex(32)
        if (
            config["auth"]["provider"] in ["telemost", "wbstream"]
            and config["auth"].get("token", "") != ""
            and config.get("room", {}).get("id", "") == ""
        ):
            room_id = await RoomGenerator.generate_room_id(
                config["auth"]["provider"],
                config["auth"]["token"],
            )

            if not room_id:
                raise RuntimeError(
                    "Failed to generate room id"
                )

            config["room"] = config.get("room", {})
            config["room"]["id"] = room_id

        if config["auth"]["provider"] == "telemost":
            config["auth"].pop("token", None)

        if config["auth"]["provider"] == "jitsi":
            room_url = config["room"]["id"].split("/")

            if len(room_url) == 3:
                room_url.append(
                    str(secrets.token_hex(16))
                )

            elif len(room_url) == 4 and not room_url[3]:
                room_url[3] = str(secrets.token_hex(16))

            config["room"]["id"] = "/".join(room_url)
        return yaml.dump(config)

    @staticmethod
    def build_transport_options(cfg: dict) -> str:
        transport = cfg["net"]["transport"]

        if transport == "datachannel":
            return ""

        short = TRANSPORT_NAMES[transport]

        params = "&".join(
            f"{TRANSPORT_OPTIONS[short][k]}={v}"
            for k, v in cfg[short].items()
        )

        return f"<{params}>"

    @staticmethod
    def config_to_uri(config: str, name: str) -> str:
        cfg = yaml.safe_load(config)

        options = Subscriptions.build_transport_options(cfg)

        return (
            f"olcrtc://{cfg['auth']['provider']}?"
            f"{cfg['net']['transport']}"
            f"{options}"
            f"@{cfg['room']['id']}#"
            f"{cfg['crypto']['key']}${name}"
        )

    @staticmethod
    async def get_launched_tags(short_uuid: str):
        servers = []

        for srv in await OlcRTC.all():
            info = await srv.show()
            parsed = Containers.parse_panel_labels(info)

            if parsed is not None and parsed[1] == short_uuid:
                servers.append(parsed[0])

        return servers

    @staticmethod
    def prepare_sub_text(
        uris: list[str],
        name: str,
        used: int = 0,
        limit: int = 0,
    ):
        txt = (
            f"#name: {name}\n"
            f"#update: 2147483647\n"
            f"#refresh: {SettingsService.get().sub_update_interval}\n"
        )
        # Traffic here is what this panel's own SOCKS proxy counted inside
        # its olcrtc containers - local accounting only, not Remnawave
        # node traffic.
        if limit == 0:
            txt += f"#used: {bytes_to_notation(used)}\n"
        else:
            txt += (
                f"#used: {bytes_to_notation(used)}/"
                f"{bytes_to_notation(limit)}\n"
                f"#available: "
                f"{bytes_to_notation(limit-used)}\n\n"
            )

        for uri in uris:
            name = uri[uri.find("$") + 1:]

            name, icon = Subscriptions.remove_last_emoji(name)

            txt += (
                f"{uri}\n"
                f"##name: {name}\n"
            )

            if icon:
                txt += f"##icon: {icon}"

        return txt

    @staticmethod
    async def _cleanup_user_containers(short_uuid: str):
        await reconcile_module.revoke_user_unchecked(
            short_uuid, "profile_room_gone"
        )


    @staticmethod
    async def _ensure_local_user_from_rw(short_uuid: str, rw_user: Any):
        """Create the local user row from the authoritative record.

        `rw_user` is the Remnawave UserResponseDto carried by RwAccess.user -
        the same object the access decision came from, so no second panel
        call can contradict it.
        """
        try:
            await Users.get(short_uuid)
        except Exception:
            await Users.add(
                UserSchema(
                    short_uuid=short_uuid,
                    name=rw_user.username,
                    expires_at=rw_user.expire_at,
                )
            )

    @staticmethod
    def traffic_limit_response(traffic: TrafficInfoSchema):
        traffic_uri = (
            "olcrtc://wbstream?"
            "datachannel@0#"
            "0000000000000000000000000000000000000000000000000000000000000000"
            "$Traffic limit exceeded"
        )

        return Response(
            content=Subscriptions.prepare_sub_text(
                [traffic_uri],
                SettingsService.get().sub_name,
                traffic.used,
                traffic.limit,
            ),
            status_code=403,
            media_type="text/plain",
        )

    @staticmethod
    async def ensure_profiles_running(short_uuid: str):

        running_tags = await Subscriptions.get_launched_tags(
            short_uuid
        )

        async def load_config(tag):

            container_name = (
                f"olcwave-{tag}-{short_uuid}"
            )

            config = await OlcRTC.get_config(
                container_name
            )

            if isinstance(config, bytes):
                config = config.decode()

            return tag, config

        loaded = await asyncio.gather(
            *(load_config(tag) for tag in running_tags)
        )

        configs = dict(loaded)

        async def check_profile(tag, config):

            obj = yaml.safe_load(config)

            provider = obj["auth"]["provider"]

            if provider in ("telemost", "wbstream"):

                exists = await RoomChecker.check_room_id(
                    provider,
                    obj["room"]["id"],
                    obj["auth"].get("token", ""),
                )

                if not exists:
                    await OlcRTC.remove(
                        f"olcwave-{tag}-{short_uuid}"
                    )

        await asyncio.gather(
            *(check_profile(tag, cfg)
              for tag, cfg in configs.items())
        )

        profiles_list = await Profiles.get_all()

        profiles = {
            profile.tag: profile
            for profile in profiles_list
        }

        missing = profiles.keys() - configs.keys()

        async def start_profile(tag):
            config = await Subscriptions.profile_to_config(
                profiles[tag].profile
            )

            configs[tag] = config

            await Containers.run(
                config,
                tag,
                short_uuid,
            )

        await asyncio.gather(
            *(start_profile(tag) for tag in missing)
        )

        return configs, profiles

    @staticmethod
    async def get(short_uuid: str):
        if settings.RW_ENABLED:
            # One authorization + provisioning critical section per request:
            # the deny check and the ensure run under the SAME per-user lock,
            # so a revocation can never interleave with container creation
            # and a denied user can never race a fresh allow into creation.
            # No mutation ever happens during a Remnawave outage: within the
            # persisted grace window only the already-running containers'
            # cached configs are served; past it (or on a deny fact) the
            # request is refused and revocation happens without creating
            # anything.
            await acquire_user_lock(short_uuid)
            try:
                # Authoritative evaluate INSIDE the same per-user lock that
                # covers provisioning: the decision can never race a revoker.
                decision = await SubscriptionGuard.check(short_uuid)

                if not decision.allowed:
                    # Deny fact OR outage past grace: revoke owned containers
                    # and refuse. Never serve data on a stale authorization.
                    await reconcile_module.revoke_user_unchecked(
                        short_uuid,
                        f"subscription_denied: {decision.reason}",
                    )
                    return Response(status_code=404)

                outage_only = decision.access is None

                if not outage_only:
                    # Single authoritative call: the user record rides on the
                    # decision (RwAccess.user) - no second panel call that
                    # could contradict the decision above.
                    access = decision.access
                    rw_user = getattr(access, "user", None)
                    if rw_user is not None:
                        await Subscriptions._ensure_local_user_from_rw(
                            short_uuid, rw_user
                        )

                traffic = await Users.get_traffic(short_uuid)
                if traffic.exceeded:
                    return Subscriptions.traffic_limit_response(traffic)

                try:
                    if outage_only:
                        # Outage within the persisted grace: cached state
                        # only - NO container creation or replacement.
                        configs, profiles = await Subscriptions.get_existing(
                            short_uuid
                        )
                    else:
                        configs, profiles = (
                            await Subscriptions.ensure_profiles_running(
                                short_uuid
                            )
                        )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "subscription build failed for %s", short_uuid
                    )
                    return Response(status_code=503)
            finally:
                release_user_lock(short_uuid)
        else:
            try:
                await Users.get(short_uuid)
            except Exception:
                return Response(status_code=404)

            traffic = await Users.get_traffic(short_uuid)
            if traffic.exceeded:
                return Subscriptions.traffic_limit_response(traffic)

            try:
                configs, profiles = await _ensure_locked(short_uuid)
            except Exception:
                logging.getLogger(__name__).exception(
                    "subscription build failed for %s", short_uuid
                )
                return Response(status_code=503)

        uris = [
            Subscriptions.config_to_uri(
                configs[tag],
                profiles[tag].name,
            )
            for tag in profiles
        ]

        return Response(
            content=Subscriptions.prepare_sub_text(
                uris,
                SettingsService.get().sub_name,
                traffic.used,
                traffic.limit,
            ),
            media_type="text/plain",
        )

    @staticmethod
    async def get_existing(short_uuid: str):
        """Read the running containers' configs without creating anything.

        Used during a Remnawave outage (within the persisted grace window):
        cached state may be served, but no container is ever created or
        replaced while the panel cannot confirm access.
        """
        running_tags = await Subscriptions.get_launched_tags(short_uuid)

        async def load_config(tag):
            config = await OlcRTC.get_config(
                f"olcwave-{tag}-{short_uuid}"
            )
            if isinstance(config, bytes):
                config = config.decode()
            return tag, config

        loaded = await asyncio.gather(
            *(load_config(tag) for tag in running_tags)
        )
        configs = dict(loaded)

        profiles_list = await Profiles.get_all()
        profiles = {
            profile.tag: profile
            for profile in profiles_list
            if profile.tag in configs
        }

        return configs, profiles

