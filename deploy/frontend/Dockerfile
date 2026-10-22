# Frontend (admin panel) build image. Node 22 pinned; deps via npm ci from
# package-lock.json. VITE_* values are baked at build time via --build-arg
# (compose passes them from x-frontend-env); no env file, no secrets.
#
# VITE_API_URL: same-origin absolute form "/api" - axios baseURL is used for
# every panel request and Caddy strips the /api prefix before proxying to
# the API, so relative "/api" matches the serve origin on any LAN host/IP.
FROM node:22-alpine@sha256:b7ef7583bfe52302a002cc282f93fe15b8116688a7304bdc7b459b7cfcf29865

WORKDIR /build

# context is the frontend/ directory (see deploy/docker-compose.vm.yaml)
COPY package.json package-lock.json tsconfig.json tsconfig.node.json tsconfig.app.json index.html vite.config.ts ./
COPY src ./src
COPY public ./public

ARG VITE_API_URL=/api
ARG VITE_SUB_URL_TEMPLATE
# VITE_SUB_URL_TEMPLATE empty -> panel falls back to <origin>/sub/{uuid},
# which is exactly the raw-subscription route Caddy serves on :8080.
ENV VITE_API_URL=${VITE_API_URL} \
    VITE_SUB_URL_TEMPLATE=${VITE_SUB_URL_TEMPLATE}

RUN npm ci && npm run build

# Exports /build/dist to the Caddy static volume
CMD ["cp", "-r", "/build/dist/.", "/out/"]
