# Dedicated VM deployment and rollback

Use a dedicated Debian13 VM with its own Docker daemon. Never give OLCWave the
workstation or shared-host Docker socket. Compose project names and socket `:ro`
are not privilege isolation. Current test VM100 is `10.69.1.100` on VLAN100.

## Provision

`bootstrap-test-vm.sh` runs on `pve.home.arpa` and creates only VM100 from the
checksum-pinned Debian13 cloud image. It refuses to overwrite an existing VMID.
Provide `/root/olcwave-test.pub` containing only the agent public SSH key. VM has
2 vCPU, 4 GiB RAM, 32 GiB disk, `onboot=0`, server VLAN100 gateway/DNS10.69.1.1.

Inside the new VM install `docker.io`, `docker-compose`, `qemu-guest-agent`,
`ca-certificates`, `curl`, `git` and enable Docker/guest-agent. Transfer this
repository to `/opt/olcwave`, excluding `.git`, `.venv`, `.rundata`, node_modules,
all `.env` files, private backend.env and TLS private keys.

## Build the engine and application

Run inside the VM only:

```sh
cd /opt/olcwave
export HOST_IP=10.69.1.100
docker build -f deploy/engine/Dockerfile -t olcwave/olcrtc:bac0989 deploy/engine
docker tag olcwave/olcrtc:bac0989 olcrtc
```

The runtime alias `olcrtc` is required by upstream's container SDK. The source
is vendored from the engine Ghostlane1.0.466 uses. Runtime compatibility must be
checked via actual tunnel traffic, not inferred from image build success.
Routing is optional and is not used in the acceptance; do not start Xray routing
unless you explicitly configure and test it.

## Private runtime credentials and TLS

Create `deploy/backend.env` from its example, permissions0600. Set a dedicated
Remnawave read-only token (`users:read`, `subscriptions:read`,
`subscription-settings:read`), trusted panel URL and required test internal squad.
Use separate generated database/admin/JWT secrets. No production DB is involved.
Copy the trusted Remnawave root CA to `deploy/remnawave-ca.pem`; inside API it is
mounted at `/certs/ca.pem` and `RW_CA_BUNDLE` must match.

Generate private test TLS on a fresh test identity:

```sh
cd /opt/olcwave
HOST_IP=10.69.1.100 deploy/generate-test-tls.sh
cp deploy/tls/ca.crt /usr/local/share/ca-certificates/olcwave-test.crt
update-ca-certificates
```

The script refuses to overwrite keys. Trust its CA only on isolated test clients.
Ghostlane refuses HTTP subscriptions; do not bypass that safeguard. Test CA/key
files must stay out of Git and outside image build contexts. Caddy uses supplied
certificates, no public ACME, and publishes only LAN HTTPS8080/8081.

```sh
cd /opt/olcwave
export HOST_IP=10.69.1.100
docker compose -f deploy/docker-compose.vm.yaml build
docker compose -f deploy/docker-compose.vm.yaml up -d
docker compose -f deploy/docker-compose.vm.yaml ps
curl --cacert deploy/tls/ca.crt -fsS https://${HOST_IP}:8080/api/health
```

Frontend builders use npm lockfiles and exit after exporting static assets.
Postgres and API healthchecks gate proxy startup. Image bases are digest-pinned.

## GUI and client acceptance

- OLCWave admin GUI: `https://10.69.1.100:8080`.
- Raw URL: `https://10.69.1.100:8080/sub/<short_uuid>`.
- Browser subscription page/client alternate origin: HTTPS8081 at `/<short_uuid>`.
- Create only a separate test user/squad in Remnawave. Add a fresh Jitsi transport
  profile in OLCWave; do not reuse any working user's room/key.
- Ghostlane1.0.466 daemon with separate state/socket in proxy mode only:

```sh
ghostlane run --state-dir /opt/olcwave-proof/cli-state \
  --socket /opt/olcwave-proof/cli.sock
ghostlane add 'https://10.69.1.100:8080/sub/<short_uuid>' \
  --socket /opt/olcwave-proof/cli.sock
ghostlane list --socket /opt/olcwave-proof/cli.sock
ghostlane connect 1 --proxy --socket /opt/olcwave-proof/cli.sock
```

Proxy address is controlled by `GHOSTLANE_PROXY_PORT`; current acceptance used
loopback19080. Observe `status` until handshake finishes before exercising HTTPS.
No TUN or workstation route modifications are needed. Use certificate-verified
`https://example.com/` traffic through SOCKS5 as a positive proof.

Disable, expire, remove squad membership and revoke from Remnawave GUI. Without
client refresh, owned container and previously downloaded key must stop working.
Re-enable/current new link must recover with a fresh key. Check password-only
revoke separately because the URL remains unchanged. Restart API: valid current
key/container must remain stable. Simulate panel outage only through a VM API
Compose override; observe cached-only grace120 seconds, then removal, then
recovery after restoring the original deployment.

Actual results and limitations: [ACCEPTANCE.vm.md](ACCEPTANCE.vm.md).

## Scoped rollback

Delete only the test user in Remnawave and disconnect the isolated Ghostlane
client. Stop the test API/engine on this dedicated VM if no longer needed.
Do not delete the whole VM without a separate request; it is retained for review.
For complete test-stack teardown inside this VM:

```sh
export HOST_IP=10.69.1.100
docker compose -f /opt/olcwave/deploy/docker-compose.vm.yaml down
```

`down` does not remove dynamically created olcRTC containers: list only containers
labelled `olcwave.managed=true` and remove this test's owner explicitly, or delete
the user first and observe reconciliation. Add `-v` only when intentionally
removing the VM's test database and static volumes. No broad image/container prune,
no shared daemon cleanup and no production database restore are required.
