# XrayCore routing image, built with checksum-pinned upstream release.
# OLCWave's routing service runs the container named olcwave-xraycore from the
# image alias `xraycore` (backend/src/xraycore/sdk.py), so the alias must exist
# on the daemon: docker build -f deploy/xraycore/Dockerfile -t olcwave/xraycore:v26.3.27 deploy/xraycore
#   docker tag olcwave/xraycore:v26.3.27 xraycore
FROM alpine:3.20@sha256:c64c687cbea9300178b30c95835354e34c4e4febc4badfe27102879de0483b5e

# Pinned Xray-core v26.3.27 release zip, SHA-256 verified at build.
ARG XRAY_SHA256=23cd9af937744d97776ee35ecad4972cf4b2109d1e0fe6be9930467608f7c8ae

RUN apk add --no-cache \
    ca-certificates \
    bash wget unzip

WORKDIR /app

RUN wget -q https://github.com/XTLS/Xray-core/releases/download/v26.3.27/Xray-linux-64.zip \
    && echo "${XRAY_SHA256}  Xray-linux-64.zip" | sha256sum -c - \
    && unzip Xray-linux-64.zip && rm Xray-linux-64.zip

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["bash", "/app/entrypoint.sh"]
