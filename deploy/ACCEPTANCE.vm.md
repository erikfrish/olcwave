# Changes in this fork

Upstream: [invdevv/olcwave](https://github.com/invdevv/olcwave), base commit `80958c462cf04a4a825e9438fe4f36f82898e368`. Original authors and AGPL-3.0 license preserved. Vendored transport keeps its Apache-2.0 license.

## Access control
- Authoritative Remnawave ACTIVE status, expiry and squad checks.
- Background revocation removes cached Ghostlane credentials without a subscription refresh.
- Full subscription revoke and password-only rotation invalidate old keys while allowing current-link recovery.
- Serialized per-user provisioning/revocation and label-based container ownership.
- Persisted bounded panel-outage grace (120 seconds by default); only existing configuration can be served during grace.

## Multi-node and accounting
- Dedicated Docker daemons reached through local Unix sockets; exact profile-tag routing, no silent local fallback.
- Node health and node-specific containers visible in the GUI.
- Durable aggregate Ghostlane accounting with `(node, container_id)` cursors and transactional usage updates.
- First samples count usage; restart/recreation does not reset a quota.
- Selected-user monthly calendar reset helper with timezone-aware schedule and idempotent month journal.

## Client and deployment
- Ghostlane user-agent receives URI-only output rather than OLCBox comment lines shown as unsupported profiles.
- Subscription headers expose provider, expiry and Ghostlane-only quota.
- Pinned `ghostlane-project/olcrtc` source `bac09891b3b4d3d4d9a48a1cb8eb61e674752dd1`, compatible with Ghostlane 1.0.466.
- Digest-pinned deployment images, frozen Python dependencies, trusted upstream CA and supplied HTTPS certificates.

## Verification and limits
Real integration was exercised with Remnawave 3.4.5 and Ghostlane CLI 1.0.466: HTTPS, GUI disable/enable, expiry, squad removal, full/password-only revoke, restart stability, panel outage and three-node quota teardown. Thirty behavior regressions passed. Mobile/desktop Ghostlane GUI and whitelist-restricted networks were not tested.

This is a separate companion panel, not a native Remnawave plugin. Ghostlane quota is not combined with Remnawave VLESS traffic. No Ghostlane hardware attestation/HWID enforcement is claimed. Polling can overshoot limits. An unreachable node cannot be confirmed revoked until it returns; failures are retried. Reset while a node is unreachable can conservatively re-count its usage. Frontend dependency advisories remain; perform your own security review before exposing the service.

Never grant this application a shared production Docker socket. A read-only socket mount does not make the Docker API read-only. Use dedicated VMs or dedicated unprivileged rootless daemons. Earlier development revealed unsafe name-prefix ownership; the corrected implementation uses management labels and dedicated-daemon isolation.
