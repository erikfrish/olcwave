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
| 1 | `rw/sdk.py:isUserValid` trusted existence and squad only; a disabled/expired user was accepted. | Access requires ACTIVE, future expiry and configured squad membership. `sub_revoked_at` is a rotation marker, not a permanent ban; previous keys are revoked while the valid current link can recover. |
| 2 | `Subscriptions.get` deleted the user's containers only when *that* request found an invalid user. A client holding cached URIs kept working forever — containers stayed up and the key stayed live. | `backend/src/rw/reconcile.py` — `ReconcileManager` sweeps every `REVOCATION_CHECK_INTERVAL` (default 30 s) and removes **only that user's** `olcwave-<tag>-<short_uuid>` containers on any deny fact. Startup reconciliation runs one sweep before the API serves. |
| 3 | On Remnawave outage upstream threw → 500 → no change; cached credentials stayed valid indefinitely. | Bounded grace: `REMNAWAVE_UNAVAILABLE_GRACE_SECONDS` (default 120 s) since the last successful evaluation. Past the grace the reconciler revokes; a subscription request past the grace is denied (fail-closed). There is **no indefinite stale access**. |
| 4 | Sync deleted the local DB row for users outside the squad but left their containers. | `Users.delete` now removes that user's owned containers (`Containers.remove_all_by_short_uuid`), and sync only scopes *existence* (squad); status/expiry/revocation are enforced at access time from Remnawave, so disabled/expired members remain visible to the operator but denied. |
| 5 | Concurrent requests could create or replace the same container; revocation raced provisioning. | One non-reentrant per-user lock covers authoritative evaluation, provisioning and revocation. Outage requests can only read existing cached configuration within persisted grace. |
| 6 | Transport image floated with the upstream Git branch. | Vendored `ghostlane-project/olcrtc@bac09891b3b4d3d4d9a48a1cb8eb61e674752dd1`, matching the engine source used by Ghostlane 1.0.466. The VM image binary SHA-256 is `691596f155632571c96be893a9218547b725b19af7355041398e0aed7620df3e`; build flags differ from the released binary. Protocol compatibility was proven by real HTTPS through Ghostlane, not byte identity. |
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
- **Blast radius:** panel ownership requires the `olcwave.managed=true`,
  `olcwave.config_tag`, and `olcwave.owner` container labels. An unmarked
  container is never owned merely because its name starts with `olcwave-`.
  The VM is the daemon security boundary; labels alone are not isolation.
- **Locking:** one plain per-user lock surrounds evaluation and mutation;
  private unlocked removal helpers run only inside that critical section.
  There is no nested/reentrant lock acquisition.
- **API deadlines:** every Remnawave HTTP call is bounded (10 s request /
  5 s connect). A stalled panel surfaces as `RemnawaveUnavailable` (grace),
  never as a hung request.

## Configuration

Same env as upstream (see `backend/.env.example`) plus:

```bash
REMNAWAVE_UNAVAILABLE_GRACE_SECONDS=120   # bounded stale-access window
REVOCATION_CHECK_INTERVAL=30              # reconcile sweep period (s)
RW_CA_BUNDLE=                             # optional PEM CA bundle path
RW_SQUAD_NAME=vpn-users                   # required squad by name or UUID (empty = all users)
```

## Isolated deployment (operator runbook)

The OLCWave API must control the Docker daemon that runs its `olcrtc`
containers. On a shared workstation that daemon is production infrastructure:
**compose project names (`-p`) and container-name prefixes are labeling
only — they are NOT security isolation.** Any process holding a daemon socket
can touch every container on that daemon. Do not run this stack against a
shared/production daemon; the proof run's use of the workstation's host
daemon was a contract violation (see "Isolation note"), not a recipe.

Recommended isolated shapes, in order of preference:

1. **Dedicated VM** (e.g. vm4) running its own Docker daemon; OLCWave API
   runs there with `DOCKER_HOST` pointing at that VM's daemon only.
2. **Dedicated rootless daemon** (`dockerd-rootless.sh`) on a separate user
   account, with the API as a plain process on that host. NOTE: on the proof
   workstation rootless dockerd could build but not RUN runc containers
   (devpts gid=5 / /proc mounts refused without rootlesskit+slirp4netns
   packages), so this shape was **built and verified only up to image build**;
   `deploy/docker-compose.isolated.yaml` targets it and remains unproven for
   `docker run`.
3. Any other daemon that is not the production one.

Smoke procedure (run on whatever isolated daemon you chose):

```bash
cd /mnt/extssd/Developer/olcwave
docker -H <dedicated-daemon> build -t olcrtc backend/olcrtc   # pinned engine
docker compose -f deploy/docker-compose.isolated.yaml -p olcwave-iso up -d
ghostlane add 'http://localhost:18000/sub/<short_uuid>'   # or via caddy HTTPS
ghostlane list && ghostlane connect <idx> --proxy
curl --proxy socks5h://127.0.0.1:1080 https://ifconfig.me   # must answer
# revoke in Remnawave (disable / expire / delete / squad-removal):
#   within REVOCATION_CHECK_INTERVAL the containers vanish; the still-cached
#   client URI must then FAIL to connect; recovery = re-enable, re-import.
```

## Rollback

```bash
docker compose -f deploy/docker-compose.isolated.yaml -p olcwave-iso down \
  -v                          # removes only this compose project + its volumes
docker -H <dedicated-daemon> rmi olcrtc localhost/olcwave-api:pinned
                             # remove only images this stack built
git checkout 80958c462cf04a4a825e9438fe4f36f82898e368 -- .  # upstream code
```

Never `docker rmi` broadly on a shared daemon, and never run
`docker compose down` against a project name you did not create: `-p` is a
label, not a boundary.

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

## Observed proofs (real Remnawave 3.4.5 + Ghostlane 1.0.466, 2026-10-21)

All against the real panel; isolated test users `olcwave-*-proof` were
created and **deleted afterwards**. Secrets never printed or committed.

| Scenario | Observed result |
|---|---|
| Import `/sub/<uuid>` line into Ghostlane 1.0.466 (engine `bac0989`) running headless in a dedicated client container | `added Jitsi DC` → `State: up` → `curl -x socks5h://127.0.0.1:1080 https://ifconfig.me` answered `185.233.107.198` (workstation egress; no upstream proxy configured). **CLI only — the Ghostlane GUI was NOT exercised.** |
| Disable user in Remnawave, client holds cached key, **no refresh** | container removed by reconciler after **10 s**; client reconnect with cached key: `State: failed` (empty room); `GET /sub` → 404 |
| Re-enable | next `GET /sub` → 200, fresh container, fresh key; re-import → `State: up`, HTTPS through tunnel |
| Expire (expiry set to now+70 s) | sweep at expiry: `reason=expired` removal |
| Remove from squad (`RW_SQUAD_NAME=vpn-users`) | `reason=not_in_squad` removal within one sweep; re-adding squad → allowed again |
| Delete user in Remnawave | `reason=not_found` removal within one sweep; cached-key reconnect fails |
| Panel outage (connection refused), no requests | container kept 120 s (no false revoke), then `longer than 120s grace - revoking` |
| Panel outage, subscription requested, never verified in this process | request fails closed immediately (404); no access invented |
| Recovery after outage | `GET /sub` → 200; new container; valid-user key stable across sweeps (`restarts=0`) |
| 5 concurrent `GET /sub` racing a mid-flight disable | no double-create, container removed once deny landed; no resurrection |

Bounded latencies observed: deny → container removal ≤ 1 sweep
(`REVOCATION_CHECK_INTERVAL`, 30 s default; measured 6–10 s). Outage →
revocation = `REMNAWAVE_UNAVAILABLE_GRACE_SECONDS` (120 s) + ≤ 1 sweep
(measured ~121 s).

### Isolation note for this workstation — CONTRACT VIOLATION, recorded

The proof run **used the workstation's shared host Docker daemon
(`/var/run/docker.sock`) from a plain-user process**, which the safety
contract forbids: holding a daemon socket is full daemon control regardless
of which user the API runs as, and `-p` project names / `olcwave-` name
prefixes are labels, not isolation. This was a violation, not a mitigation.

Real incident caused by this setup: the reconciler initially mis-classified
any `olcwave-*`-prefixed container as panel-owned and **force-removed an
unrelated test container named `olcwave-iso-postgres` (and
`olcwave-iso-pgtwo`)** on the shared daemon (SIGKILL + destroy, events
21:24–22:07). A later parallel `olcwave-iso-pg` survived only after the
ownership-pattern fix. A foreign-name container on a production daemon could
have been any service.

Cleanup truth: all containers created for the proof were removed (test
olcrtc containers, `olcwave-iso-pg`/`olcwave-iso-postgres`, `gl-e2e-box`,
probe/hello containers), the temporary API process and rootless dockerd were
stopped, `/tmp` evidence dirs cleaned, and the `olcrtc` image + two
`postgres:16-alpine` image references remain on the host daemon. No
vm1/vm2/vm3 service, production DB, or alertlab container was touched.

Remaining on this workstation: rootless-dind scaffolding under
`Developer/olcwave/.rundata/` (dockerd stopped; logs kept), the built
`olcrtc` image, and `/tmp` evidence files listed below. No further
shared-daemon operations were or will be performed; production use must
follow the dedicated-daemon/VM runbook above.

## Current verification status

The shared-workstation trial above is historical evidence and violated the
isolation contract. The authoritative acceptance is the dedicated VM100 run:
see [VM acceptance report](../deploy/ACCEPTANCE.vm.md). Remnawave/OLCWave GUI,
Ghostlane 1.0.466 CLI, real HTTPS, GUI disable/enable/expiry/squad removal,
full and password-only revoke, restart stability and bounded panel outage
were exercised there. The Ghostlane desktop/mobile GUI was not exercised.

Production controller and three isolated VPS execution nodes are now deployed:
see [production operations and observed proofs](../deploy/PRODUCTION.md).
The limited erikfrish Ghostlane identity is separate from the existing main
VLESS/AWG account. Quota is aggregate Ghostlane-only, not combined VPN billing.


## Evidence paths (raw artifacts of each proof row)

- Tunnel-up / HTTPS egress: `/tmp/sub1.txt`, `/tmp/sub4.txt` (subscription
  bodies with `olcrtc://` lines), `/tmp/olcwave-api.log` (API requests),
  `/tmp/gl-e2e-daemon.log` (Ghostlane client daemon log). Ghostlane itself
  ran inside container `gl-e2e-box`; its per-command outputs are quoted in
  the transcript, daemon log survives at the path above.
- Disable → 10 s removal + cached-key fail: `/tmp/olcwave-api.log` lines
  `revocation: removed 1 container(s) ... (reason=status_disabled)` and
  `GET /sub/... 404`; client `State: failed` quoted in transcript.
- Expire: same log, `reason=expired`; squad: `reason=not_in_squad`; delete:
  `reason=not_found`.
- Outage grace + past-grace revoke: `/tmp/olcwave-api.log`
  `grace 120s started` → `longer than 120s grace - revoking`.
- Race test: `/tmp/out1.txt`–`/tmp/out5.txt` (5 concurrent 200 responses),
  subsequent `revocation: ... (reason=status_disabled)`.
- Remnawave-side user lifecycle (create/enable/disable/delete responses,
  never containing secrets): `/tmp/e2e.json`, `/tmp/dis.json`, `/tmp/en.json`,
  `/tmp/exp.json`, `/tmp/sq.json`, `/tmp/outage.json`, `/tmp/race.json`,
  `/tmp/usr*.json`, `/tmp/u2.json`.
- Panel commit IDs, oldest → newest:
  `475c9dfc08caa55da6905ac0ac38c5669cc7669f`,
  `375b784a9be74479c634c56b48a7d9a2d1f3cafc`,
  `bf83fc36e7b473b768d9c19adcce9056d727639e`,
  `f46ddd40c4fe0ef79afc7c7185762ac0ba64c34e`,
  `337b140203057c0ede02038747a2404c21720de2`,
  `50f9eea2f2aceae68f37a63bd4b02116f2622965`.
