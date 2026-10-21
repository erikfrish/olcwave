#!/bin/sh
# Install a dedicated unprivileged transport daemon, never touch host Docker.
set -eu
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y rootlesskit slirp4netns uidmap dbus-user-session
if ! id olcwave-node >/dev/null 2>&1; then
    useradd --create-home --shell /bin/bash olcwave-node
fi
# useradd normally allocates subordinate IDs; fail closed if the host did not.
test -n "$(getsubids olcwave-node)"
test -n "$(getsubids -g olcwave-node)"
NODE_UID=$(id -u olcwave-node)
loginctl enable-linger olcwave-node
systemctl start "user@${NODE_UID}.service"
install -d -m 755 /usr/local/lib/olcwave
curl -fsSL https://raw.githubusercontent.com/moby/moby/v26.1.5/contrib/dockerd-rootless.sh -o /usr/local/lib/olcwave/dockerd-rootless.sh
printf '%s  %s\n' 'f2e515d1e635a110c95d939a9ef115725317ea2c3e752469ea4ca0e3dcc12ea1' /usr/local/lib/olcwave/dockerd-rootless.sh | sha256sum -c -
chmod 755 /usr/local/lib/olcwave/dockerd-rootless.sh
cat > /etc/systemd/system/olcwave-node-docker.service <<'UNIT'
[Unit]
Description=Isolated rootless OLCWave transport daemon
After=network-online.target
Wants=network-online.target
[Service]
User=olcwave-node
Group=olcwave-node
Environment=HOME=/home/olcwave-node
Environment=XDG_RUNTIME_DIR=/run/olcwave-node-docker
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_NET=slirp4netns
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_MTU=1500
Environment=DOCKERD_ROOTLESS_ROOTLESSKIT_DISABLE_HOST_LOOPBACK=true
EnvironmentFile=/etc/olcwave-node-docker.env
RuntimeDirectory=olcwave-node-docker
RuntimeDirectoryMode=0700
Delegate=yes
Type=simple
ExecStart=/usr/local/lib/olcwave/dockerd-rootless.sh --host=unix:///run/olcwave-node-docker/docker.sock --data-root=/home/olcwave-node/docker --exec-root=/run/olcwave-node-docker/exec --pidfile=/run/olcwave-node-docker/docker.pid --storage-driver=overlay2
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
TasksMax=512
MemoryMax=256M
[Install]
WantedBy=multi-user.target
UNIT
printf 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%s/bus\n' "$NODE_UID" > /etc/olcwave-node-docker.env
systemctl daemon-reload
systemctl enable --now olcwave-node-docker.service
systemctl is-active olcwave-node-docker.service
