import asyncio
import logging
import json
import secrets
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import emoji
import yaml

from fastapi import Response

from config import settings
from settings.service import SettingsService
from users.schemas import TrafficInfoSchema, UserSchema
from profiles.roomGenerator import RoomChecker, RoomGenerator
from olcrtc import nodes
from profiles.service import Containers
from profiles.service import Profiles
from users.service import Users
import rw.reconcile as reconcile_module
from rw.reconcile import ReconcileManager, SubscriptionGuard

# Boring per-user lock: one plain asyncio.Lock per short_uuid. NOT reentrant.
# Every entry into a per-user critical section takes it exactly once; helpers
# that must run under the lock (revocation removal) are provided unlocked and
# are called only while the lock is held, so no held-set cleverness is needed
_profile_locks: dict[str, asyncio.Lock] = {}


class NodeUnavailableError(RuntimeError):
    """A node holding the user's profiles could not be inspected."""


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

async def _config_from_inspect(handle) -> str | None:
    """CONFIG env captured at container creation, via inspect only.

    OlcRTC.run writes the issued config into the CONFIG env and
    OlcRTC.restart already treats that env as the authoritative config, so
    it is the standard fallback when the in-container file cannot be read
    (e.g. Docker exec 409 while the container restarts). It carries the
    SAME key and room the clients already hold - never a fresh credential.
    """
    try:
        info = await handle.show()
    except Exception:  # noqa: BLE001 - inspect here is best-effort
        return None
    for item in info.get("Config", {}).get("Env") or []:
        if item.startswith("CONFIG="):
            value = item.split("=", 1)[1]
            return value if value.strip() else None
    return None


async def _load_config_isolated(tag: str, handle) -> tuple[str, str | None]:
    """Read one container's config without failing its siblings.

    Returns (tag, config-or-None): a failed exec read falls back to the
    inspect CONFIG env; only when neither source yields a config does the
    tag drop out (and it is then never recreated - the container exists).
    Logs the tag and exception TYPE only - never the error text, which may
    carry credentials.
    """
    log = logging.getLogger(__name__)
    try:
        return tag, await Containers.get_config_of(handle)
    except Exception as exc:  # noqa: BLE001 - per-container isolation
        config = await _config_from_inspect(handle)
        if config is not None:
            log.warning(
                "config exec failed for %s (%s); serving inspect CONFIG",
                tag, type(exc).__name__,
            )
            return tag, config
        log.warning(
            "config unavailable for %s (%s)", tag, type(exc).__name__,
        )
        return tag, None

class Subscriptions:
    @staticmethod
    def profiles_for_user(profiles, short_uuid):
        restrictions = json.loads(settings.OLCWAVE_PROFILE_USERS_JSON)
        return {
            profile.tag: profile for profile in profiles
            if profile.tag not in restrictions or short_uuid in restrictions[profile.tag]
        }

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
            room = config['room']['id']
            origin = urlsplit(room if '://' in room else f'https://{room}')
            config['room']['id'] = urlunsplit((
                origin.scheme, origin.netloc, f'/{secrets.token_hex(16)}', '', '',
            ))
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
        """Tags of the user's panel containers on EVERY node.

        Returns ((tag, handle) pairs, unreachable node names): the handle
        carries the source node, so config reads and removals below hit the
        SAME daemon - remote containers are served/revoked exactly like local
        ones. A node in the unreachable set contributed nothing, so its tags
        being absent says nothing about whether their containers exist.
        """
        launched = []
        handles, node_errors = await Containers.panel_containers(short_uuid)
        for handle in handles:
            info = await handle.show()
            parsed = Containers.parse_panel_labels(info)
            if parsed is not None and parsed[1] == short_uuid:
                launched.append((parsed[0], handle))

        # panel_containers reports every failure as "<node>: <error>".
        unreachable = {err.split(":", 1)[0] for err in node_errors}
        return launched, unreachable

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
                txt += f"##icon: {icon}\n"

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

        # (tag, handle) pairs across ALL nodes; handle routes every follow-up
        # call to the daemon the container actually lives on.
        running, unreachable = await Subscriptions.get_launched_tags(short_uuid)

        profiles_list = await Profiles.get_all()

        profiles = Subscriptions.profiles_for_user(profiles_list, short_uuid)

        # An unreachable node hides its containers: their tags would look
        # missing and be recreated with fresh rooms/keys, silently breaking
        # every client holding the current config. Refuse before mutating
        # anything; the client keeps its last subscription and retries.
        blocked = sorted(
            tag for tag in profiles
            if unreachable and nodes.node_for_tag(tag) in unreachable
        )
        if blocked:
            raise NodeUnavailableError(
                f"nodes {sorted(unreachable)} unreachable; "
                f"not provisioning {blocked}"
            )

        # Per-container isolation: one container's failed config read (e.g.
        # exec 409 while it restarts) must not 503 the whole subscription or
        # make the tag look missing and get recreated with a fresh key/room.
        # The inspect CONFIG fallback serves the SAME credential the clients
        # already hold. A tag with no config at all is DROPPED, never
        # recreated - its container demonstrably exists.
        loaded = await asyncio.gather(
            *(_load_config_isolated(tag, handle) for tag, handle in running)
        )

        configs = {tag: cfg for tag, cfg in loaded if cfg is not None}
        handles = {tag: handle for tag, handle in running}

        # Per-profile isolation for the whole ensure step: one profile's
        # failing room check (upstream outage) or failed container start
        # must not 503 the subscription when other profiles are fine - and
        # must never be retried by recreating an existing container with a
        # fresh key/room.
        async def check_profile(tag, config):
            try:
                obj = yaml.safe_load(config)

                provider = obj["auth"]["provider"]

                if provider in ("telemost", "wbstream"):

                    exists = await RoomChecker.check_room_id(
                        provider,
                        obj["room"]["id"],
                        obj["auth"].get("token", ""),
                    )

                    if not exists:
                        # Remove on the SAME node the handle was found on.
                        await Containers.remove_handle(handles[tag])
                        configs.pop(tag, None)
                        handles.pop(tag, None)
            except Exception as exc:  # noqa: BLE001 - per-profile isolation
                logging.getLogger(__name__).warning(
                    "room check failed for %s of %s (%s); profile excluded",
                    tag, short_uuid, type(exc).__name__,
                )
                configs.pop(tag, None)

        await asyncio.gather(
            *(check_profile(tag, cfg)
              for tag, cfg in list(configs.items()))
        )

        # Provision only genuinely absent containers: a tag whose config
        # could not be read still HAS a container - recreating it would
        # mint a fresh key/room and break every current client.
        missing = profiles.keys() - handles.keys()

        async def start_profile(tag):
            try:
                config = await Subscriptions.profile_to_config(
                    profiles[tag].profile
                )

                await Containers.run(
                    config,
                    tag,
                    short_uuid,
                )

                configs[tag] = config
            except Exception as exc:  # noqa: BLE001 - per-profile isolation
                logging.getLogger(__name__).warning(
                    "provisioning failed for %s of %s (%s); profile excluded",
                    tag, short_uuid, type(exc).__name__,
                )

        await asyncio.gather(
            *(start_profile(tag) for tag in missing)
        )

        if running and not configs:
            raise NodeUnavailableError(
                f"all {len(running)} container config(s) of {short_uuid} "
                "unreadable; not serving an empty subscription"
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
                except NodeUnavailableError as exc:
                    logging.getLogger(__name__).warning(
                        "subscription deferred for %s: %s", short_uuid, exc
                    )
                    return Response(status_code=503)
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
            except NodeUnavailableError as exc:
                logging.getLogger(__name__).warning(
                    "subscription deferred for %s: %s", short_uuid, exc
                )
                return Response(status_code=503)
            except Exception:
                logging.getLogger(__name__).exception(
                    "subscription build failed for %s", short_uuid
                )
                return Response(status_code=503)

        # Per-profile isolation to the very end: a config that cannot be
        # turned into a URI (corrupt YAML, missing keys) is dropped instead
        # of failing the whole bundle.
        uris = []
        for tag in profiles:
            if tag not in configs:
                continue
            config = configs[tag]
            try:
                uris.append(Subscriptions.config_to_uri(
                    config,
                    "🇩🇪 DE · " + profiles[tag].name
                    if tag in {"vm1", "vm2", "vm3"} else profiles[tag].name,
                ))
            except Exception as exc:  # noqa: BLE001 - per-profile isolation
                logging.getLogger(__name__).warning(
                    "dropping unreadable config of %s (%s)",
                    tag, type(exc).__name__,
                )

        if not uris:
            return Response(status_code=503)

        return Response(
            content=Subscriptions.prepare_sub_text(
                uris,
                SettingsService.get().sub_name,
                traffic.used,
                traffic.limit,
            ),
            media_type="text/plain",
            headers={
                "profile-title": SettingsService.get().sub_name,
                "subscription-userinfo": (
                    f"upload=0; download={traffic.used}; total={traffic.limit}; "
                    f"expire={int((await Users.get(short_uuid)).expires_at.timestamp())}"
                ),
                "profile-update-interval": "1",
            },
        )

    @staticmethod
    async def get_existing(short_uuid: str):
        """Read the running containers' configs without creating anything.

        Used during a Remnawave outage (within the persisted grace window):
        cached state may be served, but no container is ever created or
        replaced while the panel cannot confirm access.
        """
        # (tag, handle) pairs across ALL nodes; configs are read on each
        # handle's own daemon. Read-only: nothing is created or replaced.
        running, _unreachable = await Subscriptions.get_launched_tags(short_uuid)

        # Per-container isolation (same policy as ensure_profiles_running):
        # one container's failed exec read falls back to its inspect CONFIG
        # env, never fails the whole read-only sweep, and never recreates
        # anything.
        loaded = await asyncio.gather(
            *(_load_config_isolated(tag, handle) for tag, handle in running)
        )
        configs = {tag: cfg for tag, cfg in loaded if cfg is not None}

        profiles_list = await Profiles.get_all()
        profiles = {
            tag: profile for tag, profile in
            Subscriptions.profiles_for_user(profiles_list, short_uuid).items()
            if tag in configs
        }

        if running and not configs:
            # The containers were SEEN but none yielded a valid config:
            # fail closed with 503 rather than an empty-but-200 bundle.
            raise NodeUnavailableError(
                f"all {len(running)} container config(s) of {short_uuid} "
                "unreadable; not serving an empty subscription"
            )

        return configs, profiles

