#!/bin/sh
set -eu

: "${CONFIG:?CONFIG env is required}"

printf '%s\n' "$CONFIG" > /app/config.json

/app/xray run -c /app/config.json