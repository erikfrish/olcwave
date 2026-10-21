# Ghostlane / Remnawave integration (OLCWave fork)

This fork of [invdevv/olcwave](https://github.com/invdevv/olcwave) (upstream
commit `80958c462cf04a4a825e9438fe4f36f82898e368`, AGPL-3.0, © invdevv /
contributors — see `LICENSE`) fixes Remnawave access enforcement and pins the
OLCRTC transport to the engine that Ghostlane 1.0.466 actually uses.

Upstream authors and license are preserved: all upstream files keep their
AGPL-3.0 headers; new files carry the same header. The vendored OLCRTC engine
(`backend/olcrtc/olcrtc-pinned/`) is Apache-2.0 © ghostlane-project/olcrtc
contributors — see `olcrtc-pinned/LICENSE` and `olcrtc-pinned/NOTICE`.

## What upstream got wrong, and what changed

| # | Upstream behavior | This fork |
|---|---|---|
| 1 | `rw/sdk.py:isUserValid` trusted `sub/{uuid}/info`: `is_found` + squad only. A **DISABLED** user, an **expired** user, or a user whose subscription was **revoked** in Remnawave still got a subscription (proven against Remnawave 3.4.5: disabled user with `expireAt=2020` returned `accepted=True, status=DISABLED`). | `backend/src/rw/access.py` — one policy: access requires `status == ACTIVE`, `expire_at` in the future, `sub_revoked_at is None`, squad membership when configured. Enforced at every subscription request **and** continuously in the background. |
| 2 | `Subscriptions.get` deleted the user's containers only when *that* request found an invalid user. A client holding cached URIs kept working forever — containers stayed up and the key stayed live. | `backend/src/rw/reconcile.py` — `ReconcileManager` sweeps every `REVOCATION_CHECK_INTERVAL` (default 30 s) and removes **only that user's** `olcwave-<tag>-<short_uuid>` containers on any deny fact. Startup reconciliation runs one sweep before the API serves. |
| 3 | On Remnawave outage upstream threw → 500 → no change; cached credentials stayed valid indefinitely. | Bounded grace: `REMNAWAVE_UNAVAILABLE_GRACE_SECONDS` (default 120 s) since the last successful evaluation. Past the grace the reconciler revokes; a subscription request past the grace is denied (fail-closed). There is **no indefinite stale access**. |
| 4 | Sync deleted the local DB row for users outside the squad but left their containers. | `Users.delete` now removes that user's owned containers (`Containers.remove_all_by_short_uuid`), and sync only scopes *existence* (squad); status/expiry/revocation are enforced at access time from Remnawave, so disabled/expired members remain visible to the operator but denied. |
| 5 | Two concurrent `GET /sub/{uuid}` requests could both see a profile "missing" and double-create/replace the same container (`OlcRTC.run` force-deletes then creates). | Per-user `asyncio.Lock` around `ensure_profiles_running` (`subscriptions/service.py:ensure_profiles_running_locked`). |
| 6 | Transport image floated: `git clone --depth=1 openlibrecommunity/olcrtc` at build time — whatever HEAD was that day. | Vendored pinned source `backend/olcrtc/olcrtc-pinned/` @ `bac09891b3b4d3d4d9a48a1cb8eb61e674752dd1` = engine `v0.0.0-20261012005350-bac09891b3b4` reported by Ghostlane 1.0.466. Rebuilt with `go build -trimpath ./cmd/olcrtc` this is **byte-identical** to the engine binary released with Ghostlane 1.0.466 (SHA-256 `d19cf4e55122c9c7b19b45267508b71272f38a0840222c4054b9d2eefcc39988`). |
| 7 | Subscription advertised traffic as if authoritative. | Header comment in code + docs: the `#used/#available` values are **local** SOCKS-proxy accounting from this panel's own containers — not Remnawave node traffic. No combined accounting is claimed. |
| 8 | CA handling fixed to system store only. | `RW_CA_BUNDLE` (optional PEM path) is passed to the Remnawave SDK's httpx client; empty = system trust store (`/etc/ssl/certs/ca-certificates.crt` or `Talos_Home_CA_Root_CA.pem`). |

## Revocation model (explicit limits)

- **Latency:** a deny fact (disabled / expired / revoked / squad-removed /
  deleted in Remnawave) removes the user's containers within
  `REVOCATION_CHECK_INTERVAL` (30 s default) via the reconciler, or
  immediately when the user (or their client) touches `/sub/...` or
  `/sub/.../check`.
- **Outage:** Remnawave unreachable → previously verified users ride the
  bounded grace (`REMNAWAVE_UNAVAILABLE_GRACE_SECONDS`, 120 s default) counted
  **per user since last successful check**; past grace their containers are
  removed and new subscriptions are refused. Recovery is automatic and safe:
  once Remnawave answers again, valid users re-verify on their next request;
  the reconciler never touches a user whose evaluation says `allowed`.
- **Credential stability:** for valid users nothing rotates — container
  configs and their `crypto.key` persist; the reconciler only removes.
- **Blast radius:** every removal path filters names by
  `olcwave-<tag>-<short_uuid>` (name endswith `-<short_uuid>` **and**
  `olcwave-` prefix and 3-part name). Nothing else on the Docker daemon is
  started, stopped or removed.

## Configuration

Same env as upstream (see `backend/.env.example`) plus:

```bash
REMNAWAVE_UNAVAILABLE_GRACE_SECONDS=120   # bounded stale-access window
REVOCATION_CHECK_INTERVAL=30              # reconcile sweep period (s)
RW_CA_BUNDLE=                             # optional PEM CA bundle path
RW_SQUAD_NAME=vpn-users                   # required squad (empty = all users)
```

## Isolated deployment (operator runbook)

Never point this stack at the production Docker daemon of a shared host. Use
a dedicated daemon/VM. The proof run used the workstation's own Docker in a
dedicated compose project with `olcwave-` prefixed names — no existing
service shares those names.

```bash
cd /mnt/extssd/Developer/olcwave
docker compose -p olcwave-iso up -d --build     # api + postgres (+ caddy optional)
# 1. Build the pinned engine image once:
docker build -t olcrtc backend/olcrtc
# 2. Login to the panel (ADMIN_USERNAME/ADMIN_PASSWORD from backend/.env),
#    add a jitsi profile (docs/config_examples/jitsi_dc.yaml) with a real room.
# 3. Import a Remnawave user's subscription URL into Ghostlane CLI:
ghostlane add 'http://localhost:8000/sub/<short_uuid>'   # or via caddy HTTPS
ghostlane list && ghostlane connect <idx> --proxy
curl --proxy socks5h://127.0.0.1:1080 https://ifconfig.me   # must answer
# 4. Revoke in Remnawave (disable / expire / delete / remove from squad):
#    within REVOCATION_CHECK_INTERVAL + reconciliation the containers vanish;
#    the still-cached client URI must then FAIL to connect (room empty).
# 5. Recovery: re-enable in Remnawave → next GET /sub/<uuid> recreates
#    containers with a fresh key; Ghostlane refresh picks it up.
```

## Rollback

```bash
docker compose -p olcwave-iso down          # removes only olcwave-iso containers
docker rmi olcrtc olcwave-olcrtc            # remove built images
# upstream code: git checkout implementation && git branch -D integration-remnawave
```

The compose project name (`-p olcwave-iso`) and the `olcwave-` container-name
prefix isolate it from the vm1/vm2/vm3 olcrtc systemd services, which run as
bare processes outside Docker.

## Changed files vs upstream `80958c4`

- `backend/src/rw/access.py` — new: the access policy (single source of truth).
- `backend/src/rw/sdk.py` — rewritten: `evaluateAccess` on the full user
  record, `RemnawaveUnavailable` fail-closed error, CA bundle support.
- `backend/src/rw/reconcile.py` — new: background revocation, startup
  reconciliation, per-user grace ledger, `SubscriptionGuard`.
- `backend/src/subscriptions/service.py` — guarded `get`, per-user lock,
  revocation on deny, honest traffic header.
- `backend/src/subscriptions/router.py` — `/check` endpoint denies and revokes.
- `backend/src/users/service.py` — delete removes owned containers; sync
  comment clarified (existence scoping only).
- `backend/src/main.py` — startup reconciliation + reconciler lifecycle.
- `backend/src/config.py` — three new settings.
- `backend/olcrtc/Dockerfile` — pinned vendored engine build.
- `backend/olcrtc/olcrtc-pinned/` — vendored ghostlane-project/olcrtc @ bac0989.
- `docs/ghostlane-integration.md` — this file.

## Verification status

Exercised against the real Remnawave 3.4.5 panel (isolated test user created
and **removed afterwards**): policy unit checks (valid/expired/disabled/
revoked/squad-removed/wrong-squad), engine build reproducibility
(byte-identical), Docker build of the pinned image, and the end-to-end smoke
described above. See the delivery report for the exact observed outputs.
