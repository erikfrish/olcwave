# Subscription page build image. Node 22 pinned; deps via npm ci from
# package-lock.json. The page talks same-origin relative paths only, so no
# VITE_* build args are needed (see deploy/docker-compose.vm.yaml notes).
FROM node:22-alpine@sha256:b7ef7583bfe52302a002cc282f93fe15b8116688a7304bdc7b459b7cfcf29865

WORKDIR /build

COPY package.json package-lock.json tsconfig.json index.html vite.config.ts ./
COPY src ./src
COPY public ./public

RUN npm ci && npm run build

# Exports /build/dist to the Caddy static volume
CMD ["cp", "-r", "/build/dist/.", "/out/"]
