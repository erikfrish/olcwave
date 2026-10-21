# OLCWave / Remnawave / Ghostlane — dedicated VM acceptance

## Verdict

The tested access-control scheme works on a dedicated VM. It is not a native
Ghostlane plugin inside Remnawave: user status, expiry, squad and revoke actions
are administered in Remnawave; transport profiles/containers remain in OLCWave.
No existing vm1/vm2/vm3 Ghostlane identity, workstation route or shared Docker
workload was changed in this acceptance run.

## Environment

- Proxmox `pve.home.arpa`, VM100 `olcwave-test`, Debian13 cloud image
  `20261021-2638`, 2 vCPU, 4 GiB RAM, 32 GiB disk, VLAN100 `10.69.1.100`.
- Own Docker daemon inside VM, `/var/lib/docker`. OLCWave has full control of
  that daemon through its socket; the VM, not Compose names or `:ro`, is the
  security boundary.
- OLCWave GUI: `https://10.69.1.100:8080`; subscription page on HTTPS8081.
  Dedicated 30-day test CA, no WAN exposure or public ACME. Released Ghostlane
  refuses plaintext HTTP subscriptions; the actual client trusted the test CA.
- Remnawave3.4.5 via trusted LAN HTTPS and a dedicated seven-day read-only API
  token; eligibility restricted to internal squad `olcwave-vm100-test`.
- Ghostlane1.0.466 CLI, engine source `bac09891b3b4d3d4d9a48a1cb8eb61e674752dd1`.
  VM engine binary SHA256 `691596f155632571c96be893a9218547b725b19af7355041398e0aed7620df3e`.
  This build is protocol-compatible, not byte-identical to the released binary.
- Reconcile interval10 seconds for this acceptance; outage grace120 seconds.

## Observed checks

| Scenario | Actual result |
| --- | --- |
| OLCWave GUI login and profile creation | Fresh Jitsi profile created using the GUI; displayed in profile list |
| OLCWave GUI container listing | Owned container, creation timestamp and local traffic shown |
| Remnawave GUI eligibility | Dedicated test user and squad only; ordinary users excluded |
| Ghostlane HTTPS subscription import | Actual released CLI imported one usable olcRTC profile |
| Ghostlane format | URI-only response for Ghostlane UA; no OLCBox metadata rows reported as bogus profiles |
| Real tunnel traffic | Certificate-verified HTTPS Example Domain response200 through loopback SOCKS proxy |
| GUI Disable | Container removed without subscription refresh; cached tunnel request and cached-key reconnect failed; download404 |
| GUI Enable | New Ghostlane key; client refresh/reconnect restored HTTPS200 |
| GUI expiry | Date/time saved in Remnawave; container removed after expiry; old tunnel denied and download404 |
| GUI squad removal | Container removed; download404; GUI re-add restored eligibility |
| GUI Full revoke | Subscription shortUuid changed; old link404, cached traffic denied; new link200 and HTTPS restored |
| GUI Passwords only revoke | URL stayed unchanged; cached key failed; refresh issued usable fresh key and HTTPS restored |
| GUI Delete | Test user removed; its current subscription returned404 and its engine container disappeared |
| TLS bootstrap | Dedicated certificate helper generated a chain accepted by strict OpenSSL verification |
| API restart | Valid current-link key and container ID stayed unchanged; HTTPS continued |
| Panel outage across API restart | Same cached body/container within persisted grace; after120 seconds container removed and download404; reconnecting panel restored download and HTTPS |
| Behavior regression suite | 14 tests passed using deterministic isolated fakes, no production DB or Docker access |

The GUI-disable removal was observed within approximately20 seconds of the GUI
action. This includes browser/action observation overhead; no sub-second SLA is
claimed. Default production interval is30 seconds. Panel API calls have bounded
request deadlines and the sweep is sequential, so large user sets need a separate
latency/capacity test.

## Limitations

- Ghostlane desktop/mobile GUI was not exercised; the released CLI was.
- The upstream subscription page still describes OLCBox. It renders, but is not
  a complete Ghostlane-specific installation guide.
- Traffic totals are local OLCWave SOCKS accounting, not a combined Remnawave
  VLESS + Ghostlane quota. HWID enforcement for Ghostlane is not claimed.
- One-user fresh-room proof is not a multi-VPS rollout or proof in a whitelist-
  restricted network. Existing Ghostlane users were not migrated.
- `api.ipify.org` and Cloudflare trace targets were refused by the isolated
  server DNS/upstream proxy path in this network; Example Domain HTTPS succeeded.
  Those failed origins are not silently counted as passing checks.
- npm ci reported high-severity advisories in upstream frontend dependency trees.
  This remains a private test VM, not a production security sign-off.
- Test certificates expire after30 days, and the read-only panel token after7 days.

## Evidence and custody

Private evidence (contains subscription keys — never publish it):
`/mnt/extssd/talos-home/.ignore/olcwave-vm100/`:
`vm-report.json`, `api-acceptance.log`, `proxmox-config.txt`, current setup and
original/updated subscription payloads. Files0600, directory0700.

The VM is retained for review; `onboot=0`. Credentials are private in
`deploy/backend.env` on the VM and the workstation's protected evidence directory.
The public test CA is `deploy/tls/ca.crt` inside the VM. Test access teardown is
recorded in the final operator report; a full VM deletion is not part of acceptance.
