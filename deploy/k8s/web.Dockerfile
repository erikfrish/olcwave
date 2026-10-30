# Kubernetes web image: admin panel + subscription page baked into Caddy.
# Build context: repository root.
#   docker build -f deploy/k8s/web.Dockerfile \
#     --build-arg VITE_SUB_URL_TEMPLATE='https://olcsub.example/{uuid}' .
# Same build steps and pinned bases as deploy/frontend and
# deploy/subscriptionPage; only the delivery differs (no shared volume).
FROM docker.io/library/node:22-alpine@sha256:b7ef7583bfe52302a002cc282f93fe15b8116688a7304bdc7b459b7cfcf29865 AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json frontend/tsconfig.json frontend/tsconfig.node.json frontend/tsconfig.app.json frontend/index.html frontend/vite.config.ts ./
RUN npm ci
COPY frontend/src ./src
COPY frontend/public ./public
ARG VITE_API_URL=/api
ARG VITE_SUB_URL_TEMPLATE
ENV VITE_API_URL=${VITE_API_URL} \
    VITE_SUB_URL_TEMPLATE=${VITE_SUB_URL_TEMPLATE}
RUN npm run build

FROM docker.io/library/node:22-alpine@sha256:b7ef7583bfe52302a002cc282f93fe15b8116688a7304bdc7b459b7cfcf29865 AS subscriptionpage
WORKDIR /build
COPY subscriptionPage/package.json subscriptionPage/package-lock.json subscriptionPage/tsconfig.json subscriptionPage/index.html subscriptionPage/vite.config.ts ./
RUN npm ci
COPY subscriptionPage/src ./src
COPY subscriptionPage/public ./public
RUN npm run build

FROM docker.io/library/caddy:2-alpine@sha256:173b26306d711395accaeba8b67afcaad2a085ccb1e6bf26010bfe8c095a5229
COPY deploy/k8s/Caddyfile /etc/caddy/Caddyfile
COPY --from=frontend /build/dist /srv/frontend/dist
COPY --from=subscriptionpage /build/dist /srv/subscriptionPage/dist
# Runs as any non-root uid; Caddy state goes to a writable emptyDir.
ENV XDG_CONFIG_HOME=/tmp/caddy XDG_DATA_HOME=/tmp/caddy
USER 10001:10001
EXPOSE 8080 8081
