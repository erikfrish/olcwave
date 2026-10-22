# OLCRTC engine image - pinned upstream source, alias `olcrtc`.
#
# Built from the vendored ghostlane-project/olcrtc source at
# bac09891b3b4d3d4d9a48a1cb8eb61e674752dd1 (backend/olcrtc/olcrtc-pinned/).
# OLCWave's runtime SDK (backend/src/olcrtc/sdk.py) builds/runs the image
# under the alias `olcrtc`, so the compose engine build MUST keep that tag:
#   docker build -f deploy/engine/Dockerfile -t olcwave/olcrtc:<ver> deploy/engine
#   docker tag olcwave/olcrtc:<ver> olcrtc        # runtime alias
# On the dedicated VM the parent may `docker load` a prebuilt tar instead;
# either way the `olcrtc` alias must exist on that daemon before first use.
#
# Provenance note: this build (Go 1.26, -trimpath) reproduces the vendored
# source but NOT the released Ghostlane binary byte-for-byte (build flags
# differ upstream). Same source commit, no byte-identical claim.

FROM golang:1.26@sha256:3eca740eb349175cc59f462d409fc324a8239ab3ffdf73d8e63175a041c08b6b AS builder

RUN apt-get update && \
    apt-get install -y git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build/olcrtc

COPY olcrtc-pinned /build/olcrtc

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

FROM alpine:3.20@sha256:c64c687cbea9300178b30c95835354e34c4e4febc4badfe27102879de0483b5e

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
