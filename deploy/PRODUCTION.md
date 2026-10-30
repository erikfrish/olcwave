# OLCWave production controller and three Ghostlane nodes

## Live deployment

- Controller: Kubernetes namespace `olcwave` in talos-home
  (`apps/olcwave/*.yaml`, aggregate apps chart; operations in
  `talos-home/apps/README.md#olcwave`). Former VM100 `olcwave`
  (`10.69.1.100`) was retired at the 2026-10-30 cutover: units disabled, data
  retained but not authoritative.
- Admin panel: `https://olcwave.home.arpa` (LAN/private CA), or
  `https://olcwave.nazavo.de` (publicly trusted TLS, LAN allow-list).
- Client subscriptions: `https://olcsub.nazavo.de/<short_uuid>` (public HTTPS).
  This origin exposes only subscription bundles/check and user SPA, not admin API.
- Kubernetes edge ownership: `talos-home/apps/olcwave/ingressroute.yaml`.
  Traefik reaches `olcwave-web` (`8080` panel, `8081` subscriptions) over plain
  in-cluster HTTP; Secret `ingress/olcwave-origin-ca` is no longer used.
- LAN Unbound overrides map both panel names to10.69.1.90. VPN clients that
  override system DNS may still resolve the public address and get403: use the
  home.arpa alias, not a broader WAN admin allow-list.

## Kubernetes layout

- Images from this fork: `deploy/api/Dockerfile` (api), `deploy/k8s/web.Dockerfile`
  (Caddy with both SPAs, `deploy/k8s/Caddyfile`) and `deploy/k8s/ops.Dockerfile`
  (relay, exporter, quota job); pinned by digest in
  `harbor.home.arpa/talos/olcwave-{api,web,ops}:<version>-k8s-<commit>`.
- `olcwave-api`: one replica, `Recreate`; `OLCWAVE_LOCAL_NODE=false`, so the
  API never touches a local Docker socket or XrayCore and every profile tag must
  map to a remote node in `OLCWAVE_NODES_JSON`. Sidecars `relay-vm1/2/3` replace
  `olcwave-node-relay@.service`; `metrics` replaces `olcwave-metrics.service`.
- `olcwave-postgres` (PVC `olcwave-postgres-data`) replaces the Compose database.
- CronJob `olcwave-monthly-quota` replaces the systemd timer; its month journal is
  on PVC `olcwave-quota-state`.
- CronJob `olcwave-backup` (daily 02:47 MSK) writes a verified `pg_dump -Fc`
  plus a copy of the quota journal to PVC `olcwave-backups` (`nfs-backups`),
  keeping 30 days.
- Remnawave: `RW_API_URL=http://remnawave.remnawave.svc.cluster.local:3000`.
- vm2 is reached through the cluster `vpn-gateway` SOCKS proxy.

## Access boundaries

Transport nodes vm1/vm2/vm3 each run a dedicated rootless daemon under
`olcwave-node`, data-root `/home/olcwave-node/docker`, Unix socket
`/run/olcwave-node-docker/docker.sock`. Existing root Docker and bare
`olcrtc.service` / `olcrtc-oleg.service` are not managed by OLCWave.

Controller-side relay sockets `/run/olcwave-nodes/{vm1,vm2,vm3}.sock` are
provided by the `relay-*` sidecars (socat + ssh, uid 10001) on a shared
`emptyDir`. The controller has a separate SSH key (Secret `olcwave-node-ssh`).
Node authorized_keys forces ONLY
`docker -H unix:///run/olcwave-node-docker/docker.sock system dial-stdio`;
`restrict` disables shells/PTY/agent/X11/TCP forwarding. SSH Match policy also
forbids forwarding. This grants full access to that unprivileged user's isolated
Docker daemon, NOT to root daemon or host root shell. Controller API sees the
root-only relay directory, not remote TCP Docker ports.
Each relay owns only its own socket, so restarting one sidecar leaves the other
sockets intact. Recovery: restart the API pod; do not recreate VPN engines.
The VM100 `olcwave-node-relay@.service` template with
`RuntimeDirectoryPreserve=yes` is retained for VM rollback only.

The `metrics` sidecar runs the private exporter at
`olcwave-metrics.olcwave.svc.cluster.local:9465`.
It checks API auth, Remnawave reachability (`olcwave_remnawave_up`), node
states and per-state container counts, relay sockets, monthly usage/limits and
public subscription issuance for erikfrish/OLEG using supported API calls; the
expected profile count is derived from the profiles and
`OLCWAVE_PROFILE_USERS_JSON`, not hard-coded.
No direct production DB queries; credentials stay inside the API environment.
Collection is serialized, command timeout 50s, delay between samples 30s;
HTTP scrapes read only the cached snapshot. Failed snapshots omit user data
and export failure/freshness signals rather than inventing zero usage.
Grafana/VM rules live in talos-home's monitoring chart; dashboard UID
`olcwave-health`. Alerts route to Telegram (`alert_class` infrastructure,
quota warning business). For monitoring rollback remove only the `metrics`
sidecar; VPN untouched.



`OLCWAVE_NODES_JSON` maps profile tag exactly to node. Invalid config or unknown
tag fails, never falls back to the controller. Each node's image is preloaded
from the pinned engine build. API restart preserves valid keys and container
IDs. Node status is visible in OLCWave GUI; unreachable nodes report errors.

Panel scope: in Remnawave mode, Users synchronizes identities from Remnawave;
local user creation is hidden. There is no self-registration or provider-account
registration flow. Profiles has credential-entry help for Telemost Session_id
and WB access-token autogeneration, not an account login/registration wizard.
Node health appears as badges in Containers; node creation, SSH relay setup and
profile-to-node mapping remain operator-owned configuration, not GUI controls.
Restricted carriers are managed profiles `vm3jazz`, `vm3vk`, `vm3telemost`,
`vm3vkartem` (vm3) and `vm1egovm` (vm1: meet.egovm.ru answers 403 to vm3).
The private `OLCWAVE_PROFILE_USERS_JSON` maps those tags to short UUIDs;
unspecified tags remain available to all eligible users. erikfrish/OLEG
subscriptions contain eight profiles; other users receive only the three Jitsi
profiles. `vm3mamba` was removed on 2026-10-31: meet.mamba.group refuses
connections from every node.
Each user has a separate labeled container/key in the shared trial room, so
traffic, revocation and quota enforcement use the normal OLCWave lifecycle.
Former standalone trial containers are retired; their keys are obsolete.
Manual phone trial entries were removed after subscribed SaluteJazz/VK passed
HTTPS. Telemost retains its experimental status: prior mobile proof failed.
Frontend validation accepts the engine's SaluteJazz/VK/MTS provider names.
Removing a trial from issuance requires removing its profile via the API,
which also removes its managed containers. Dashboard "Online" means the API
responds, not tunnel health.

Dashboard cache repair: Dashboard and Containers now both cache the
`/containers/all_status` envelope under `containers-all`; Dashboard reads its
`containers` field. Previously the two pages cached an array and an envelope
under the same key, causing `filter is not a function` after navigation.
The flat-list frontend accessor was removed. Verified the production build and
actual Containers → Dashboard navigation with no page errors; API, keys and
transport containers were unchanged. Frontend rollback image on VM100:
`localhost/olcwave-frontend:before-dashboard-cache-fix`.

Jitsi room isolation: every newly provisioned user/profile config generates a
fresh random room on the template's Jitsi origin, whether the template is a
bare host, host/room or full URL. Template room paths are not reused. Ordinary
subscription refresh reuses existing config; reprovisioning generates again.
The six existing erikfrish/OLEG configs were recreated: six distinct rooms and
keys, all six verified with actual HTTPS. Clients must refresh their existing
subscriptions; URLs and quotas were unchanged. Tests cover two independent
users receiving different rooms for all three template formats (failed before,
passed after). API rollback image: `localhost/olcwave-api:before-room-isolation`;
do not restore colliding room configs. This random-room policy applies to Jitsi,
not the shared non-Jitsi trial rooms. Private proof:
`talos-home/.ignore/olcwave-production/room-cutover-*/`.



## erikfrish rollout policy

A separate Remnawave identity `erikfrish-ghostlane` belongs to `ghostlane-users`.
The existing `erikfrish` VLESS/AWG identity, URL, keys and unlimited limits were
preserved. Shared provider title is `vpn@nazavo.de`; account names are separate.
German transport labels are prefixed `🇩🇪 DE · ` during subscription rendering.
Emoji metadata always ends with a newline: without it, following URI lines
were swallowed by the Ghostlane metadata filter. All three server rows are
verified in both subscriptions. Provider header stays ASCII. Keys unchanged.

Current personal limits: no contractual expiry (Remnawave2099 sentinel),
aggregate500GB decimal per calendar month across vm1/vm2/vm3, reset at00:00
Europe/Moscow on the first day. The selected monthly reset applies to both
managed users; existing usage was preserved when lifting the initial trial.
This quota is NOT combined with VLESS traffic. No Ghostlane HWID cap is claimed.

Enforcement: authoritative Remnawave status/expiry/squad is checked on fetch and
background sweeps10s; cached keys are removed on all reachable nodes without
refresh. Unreachable-node removal is retried, not assumed complete. Panel outage
has persisted120s cached-state grace; new containers cannot be created then.
Traffic cursors `(node, container_id)` plus aggregate usage persist atomically in
OLCWave DB. First sample counts absolute bytes; restart/recreate cannot reset quota.
Polling accounting may overshoot the exact cap; this is not wire-byte-exact billing.
Quota lift does not automatically clear used bytes; explicit reset is separate.
Reset while a node is unreachable can conservatively re-count its traffic.

## Observed production verification

- Released Ghostlane1.0.466 imported public subscription: three entries, all usable.
- All three personal profiles forwarded certificate-verified HTTPS:
  vm1 egress185.233.107.198, vm2 202.61.243.109, vm3 5.45.105.214.
- Aggregate counter increased after real traffic and remained21818 bytes across
  controller restart (no loss/double count in this observed window).
- Disposable1024-byte fixture exceeded cap at7279 bytes; all three owned engines
  were removed and public fetch returned403. Overshoot is explicitly recorded.
- Disposable Remnawave disable removed all three engines in7.8 seconds without
  refreshing subscription. Timed expiry removed all three and returned404.
- Production GUI login succeeded, all node states/containers/traffic were visible.
-30 behavior regressions passed after integration. Build passed; npm reported
  inherited frontend dependency advisories, not a completed security audit.
- Fixtures were deleted. Existing bare Ghostlane services and main erikfrish
  limits/credentials are checked against pre-rollout capture.

## Operate and rollback

Kubernetes is authoritative: change images, replicas and configuration in
talos-home Git. The apps HelmRelease corrects drift, so a manual
`kubectl scale` is reverted. To hold the API down, commit `replicas: 0` or
suspend the HelmRelease first. Never run the API against an unreachable
Remnawave: past the 120s grace the reconciler removes unverifiable owners'
containers.

`install-rootless-node.sh` installs only the unprivileged node daemon.
Image alias `olcrtc` is pinned by the deployed image archive checksum in custody.
Never point a node relay at `/var/run/docker.sock` of a VPS root daemon.

VM100 rollback (before VM deletion only): stop the cluster API, dump the
cluster database and restore it into the VM, re-enable `olcwave-controller`,
`olcwave-metrics`, `olcwave-node-docker`, the relay units and the quota timer,
then point the IngressRoutes back at the VM. The VM database is a 2026-10-30
snapshot and must not be used as is.

Rollback only this integration: disable the limited Ghostlane identity via
Remnawave, observe its owned engines gone, scale `olcwave-api` to zero in Git and
remove the OLCWave IngressRoutes through GitOps. Legacy bare Ghostlane services
are permanently retired; preserve existing Remnawave VLESS/AWG instead.
Do not run broad Docker prune. Keep DB/keys/SSH state backups before changes;
production DB access or restore requires explicit separate permission.

## Private custody

`/mnt/extssd/talos-home/.ignore/olcwave-production/` holds policy before/after,
private subscription URL/body, scoped API token, controller credentials, node
registry/tunnel units, runtime proof and image archive. Files0600, directory0700.
A plaintext secret archive is NOT safe to share. Origin CA is long-lived; origin
leaf was renewed for365 days, renewal is an operator action before expiry.

## OLEG monthly quota

Separate Remnawave `oleg-ghostlane` receives all three managed Ghostlane nodes.
Access has no contractual expiry (panel uses2099 sentinel). Quota500GB decimal
(500000000000 bytes) resets once per calendar month, Europe/Moscow, first day
00:00. `monthly-quota-reset.py` calls the supported OLCWave reset API and keeps
a month journal (`olcwave-quota-state` PVC). The CronJob runs hourly; repeated
runs in the same month are no-ops. Private Secret `olcwave-monthly-quota`
(`config.json`) selects the users. Initial reset and repeated
no-op were exercised; next calendar boundary is2026-11-01 00:00 MSK.
Old Oleg static links and cached olcRTC keys were revoked on2026-10-22;
only the managed OLCWave access remains, so that old quota bypass is closed.
Three issued profiles passed actual HTTPS, including retries after node readiness.
Private URL/policy/proof: `.ignore/olcwave-production/oleg/` in talos-home.


## Standalone Ghostlane revocation — 2026-10-22

Both old bearer URLs at sub.nazavo.de return404 for GET, HEAD and browser
requests. The four old olcRTC credentials failed actual connection probes.
Legacy bare units on vm1/vm2/vm3 are stopped, disabled and masked; working
configs, keys and standalone binary were removed. Old publisher/installers
are removed from talos-home. The old static publisher now serves only help,
without profile/private-route Secret mounts.

New OLCWave keys for erikfrish and OLEG passed HTTPS on each of the three
nodes (six successful probes). Their URLs and limits were not changed.
No Remnawave VLESS/AWG identity was revoked; those active services are separate
from the retired olcRTC keys. Protected pre-revocation backups are historical
custody only, not a redeployment source. Proof is in
`talos-home/.ignore/olcwave-production/legacy-revocation/<UTC>/`.

