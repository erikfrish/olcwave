# OLCRTC transport image for OLCWave.
#
# The pinned engine build: ghostlane-project/olcrtc @ bac09891b3b4d3d4d9a48a1cb8eb61e674752dd1
# (the exact engine Ghostlane 1.0.466 links: engine v0.0.0-20261012005350-bac09891b3b4).
# The source is vendored in olcrtc-pinned/ (Apache-2.0, see olcrtc-pinned/LICENSE
# and NOTICE) so the build is reproducible and never floats with upstream HEAD.
#
# Rebuild verbatim:  docker build -t olcrtc .
# Pinned binary == `go build -trimpath ./cmd/olcrtc` of that commit (verified
# byte-identical to the released engine binary; see docs/ghostlane-integration.md).

FROM golang:1.26 AS builder

RUN apt-get update && \
    apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY olcrtc-pinned /build/olcrtc

WORKDIR /build/olcrtc

RUN go mod download

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath \
    -o /build/olcrtc-linux-amd64 ./cmd/olcrtc

WORKDIR /build/proxy

COPY proxy.go go.mod go.sum ./

RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build \
    -trimpath \
    -ldflags="-s -w" \
    -o /proxy \
    proxy.go


# =======================================================
# Runtime
# =======================================================

FROM alpine:3.20

RUN apk add --no-cache \
    ca-certificates \
    bash

WORKDIR /app

COPY --from=builder /build/olcrtc-linux-amd64 ./olcrtc
COPY --from=builder /proxy ./proxy

COPY entrypoint.sh .

RUN chmod +x \
    /app/olcrtc \
    /app/proxy \
    /app/entrypoint.sh

RUN mkdir -p \
    /var/lib/olcwave \
    /tmp/olcwave \
    /tmp/olcwave/data

COPY --from=builder /build/olcrtc/internal/names/data /tmp/olcwave/data

ENV PROXY_BIND_ADDR=127.0.0.1
ENV PROXY_PORT=1080
ENV STATS_FILE=/tmp/olcwave/stats.json

ENTRYPOINT ["/app/entrypoint.sh"]
