# OLCWave production controller and three Ghostlane nodes

## Live deployment

- Controller: dedicated VM100 `olcwave`, `10.69.1.100`, Proxmox `pve`, onboot1.
- Admin panel: `https://olcwave.home.arpa` (LAN/private CA), or
  `https://olcwave.nazavo.de` (publicly trusted TLS, LAN allow-list).
- Client subscriptions: `https://olcsub.nazavo.de/<short_uuid>` (public HTTPS).
  This origin exposes only subscription bundles/check and user SPA, not admin API.
- Kubernetes edge ownership: `talos-home/apps/olcwave/ingressroute.yaml`, included
  by aggregate apps Helm allow-list. TLS origin is verified with Secret
  `ingress/olcwave-origin-ca`; no insecureSkipVerify.
- LAN Unbound overrides map both panel names to10.69.1.90. VPN clients that
  override system DNS may still resolve the public address and get403: use the
  home.arpa alias, not a broader WAN admin allow-list.

## Access boundaries

Transport nodes vm1/vm2/vm3 each run a dedicated rootless daemon under
`olcwave-node`, data-root `/home/olcwave-node/docker`, Unix socket
`/run/olcwave-node-docker/docker.sock`. Existing root Docker and bare
`olcrtc.service` / `olcrtc-oleg.service` are not managed by OLCWave.

Controller-side root-only relay sockets `/run/olcwave-nodes/{vm1,vm2,vm3}.sock`
are provided by `olcwave-node-relay@.service`. The VM has a separate SSH key.
Node authorized_keys forces ONLY
`docker -H unix:///run/olcwave-node-docker/docker.sock system dial-stdio`;
`restrict` disables shells/PTY/agent/X11/TCP forwarding. SSH Match policy also
forbids forwarding. This grants full access to that unprivileged user's isolated
Docker daemon, NOT to root daemon or host root shell. Controller API sees the
root-only relay directory, not remote TCP Docker ports.

`OLCWAVE_NODES_JSON` maps profile tag exactly to node. Invalid config or unknown
tag fails, never falls back to the controller. Each node's image is preloaded
from the pinned engine build. API restart preserves valid keys and container
IDs. Node status is visible in OLCWave GUI; unreachable nodes report errors.

## erikfrish rollout policy

A separate Remnawave identity `erikfrish-ghostlane` belongs to `ghostlane-users`.
The existing `erikfrish` VLESS/AWG identity, URL, keys and unlimited limits were
preserved. Provider title is `vpn@nazavo.de-erikfrish`.

Chosen restrictions: seven days from delivery, aggregate10GiB Ghostlane quota
across vm1/vm2/vm3, no automatic reset. This quota is NOT combined with VLESS
traffic in Remnawave. No Ghostlane HWID cap is claimed. Actual private expiry
and URL are in protected custody, never in Git.

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

Inside controller VM only:

```sh
cd /opt/olcwave
export HOST_IP=10.69.1.100
docker compose -f deploy/docker-compose.vm.yaml -f deploy/docker-compose.production.yaml up -d
systemctl status olcwave-controller olcwave-node-relay@vm1 olcwave-node-relay@vm2 olcwave-node-relay@vm3
```

`olcwave-controller.service` is enabled; node relay units start independently and
retry. `install-rootless-node.sh` installs only the unprivileged node daemon.
Image alias `olcrtc` is pinned by the deployed image archive checksum in custody.
Never point node relay to `/var/run/docker.sock` of a VPS root daemon.

Rollback only this integration: disable the limited Ghostlane identity via
Remnawave, observe its owned engines gone, stop controller/relay units and
remove the OLCWave ingress through GitOps. Keep old Ghostlane bare services
and existing VLESS/AWG intact. Do not rotate main erikfrish or run broad Docker
prune. Keep DB/keys/SSH state backups before changes; production DB access or
restore requires explicit separate permission.

## Private custody

`/mnt/extssd/talos-home/.ignore/olcwave-production/` holds policy before/after,
private subscription URL/body, scoped API token, controller credentials, node
registry/tunnel units, runtime proof and image archive. Files0600, directory0700.
A plaintext secret archive is NOT safe to share. Origin CA is long-lived; origin
leaf was renewed for365 days, renewal is an operator action before expiry.
