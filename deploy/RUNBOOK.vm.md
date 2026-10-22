# Public deployment guide

Use a dedicated Linux VM with Docker Compose. Do not use a workstation/shared production daemon. The API intentionally controls the dedicated daemon through its Unix socket.

1. Clone this fork and inspect `deploy/docker-compose.vm.yaml`.
2. Set `HOST_IP` to your dedicated VM LAN address and `ORIGIN_SERVER_NAME` to its certificate DNS name.
3. Copy `deploy/backend.env.example` to `deploy/backend.env`; generate private database/admin/JWT secrets. Configure Remnawave URL, a least-privilege API token and required squad. Mount its trusted root CA as `deploy/remnawave-ca.pem` (`RW_CA_BUNDLE=/certs/ca.pem`).
4. Supply HTTPS certificate and private key in `deploy/tls/server.crt` and `server.key`. For isolated tests only, `deploy/generate-test-tls.sh` creates a private test CA; trust that CA on test clients. Released Ghostlane refuses HTTP subscriptions.
5. Build the pinned engine and alias it `olcrtc` before provisioning:

```sh
docker build -f deploy/engine/Dockerfile -t olcwave/olcrtc:bac0989 deploy/engine
docker tag olcwave/olcrtc:bac0989 olcrtc
docker compose -f deploy/docker-compose.vm.yaml build
docker compose -f deploy/docker-compose.vm.yaml up -d
```

Admin GUI uses HTTPS8080, user page HTTPS8081. Keep administration LAN-only or behind independently reviewed authentication. Public reverse proxies must verify origin TLS. No deployment-specific ingress/domain is bundled.

For multiple servers, install a dedicated rootless daemon on each transport host (`deploy/install-rootless-node.sh`). Preload the pinned image. Relay only that unprivileged daemon with a dedicated SSH key forced to `docker -H unix:///run/olcwave-node-docker/docker.sock system dial-stdio`; never relay host root Docker. `deploy/olcwave-node-relay@.service` provides controller-local sockets. Configure `OLCWAVE_NODES_JSON` mapping node names, socket URLs, exact profile tags and image alias. Set `PUBLIC_SUB_URL_TEMPLATE` if using the production override. Credentials and host-key trust must be configured by the operator.

Selected calendar resets use `monthly-quota-reset.py`, root-only config/journal, and the example systemd timer. Review timezone and selected user IDs; do not place tokens in Git.

Smoke: create a test user, create a fresh Jitsi profile, import the HTTPS URL in Ghostlane, verify HTTPS through SOCKS, then disable/expire/remove squad/revoke in Remnawave. Cached keys must fail without refresh; the valid current link must recover. Restart API and verify key/quota stability. Run `cd backend && uv sync --frozen --group dev && uv run pytest tests/backend -q`.

Rollback: revoke only users owned by this deployment, observe engines removed, then stop this dedicated stack. Dynamic transport containers are not removed by Compose down alone. Preserve database and private keys before destructive teardown; never broad-prune another daemon.
