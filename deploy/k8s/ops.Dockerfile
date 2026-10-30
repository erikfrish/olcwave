# Kubernetes companion image for the OLCWave API pod and jobs:
#   - node relays: socat UNIX socket -> ssh forced `docker system dial-stdio`
#   - metrics exporter (olcwave-metrics.py, snapshot run next to the API)
#   - monthly quota reset
# Build context: repository root.
#   docker build -f deploy/k8s/ops.Dockerfile .
FROM alpine:3.22@sha256:2e0b81d60597f449baa56dc25d355367869d2dca4cd57e866f1e2b52af3eb55f
RUN apk add --no-cache python3 socat openssh-client-default netcat-openbsd tzdata \
 && addgroup -g 10001 olcwave \
 && adduser -D -H -u 10001 -G olcwave -h /nonexistent -s /sbin/nologin olcwave
COPY deploy/olcwave-metrics.py deploy/olcwave-metrics-snapshot.py deploy/monthly-quota-reset.py /opt/olcwave/
USER 10001:10001
