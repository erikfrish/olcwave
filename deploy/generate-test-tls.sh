#!/bin/sh
# Run on the dedicated test VM. Never install this CA on production clients.
set -eu
HOST_IP=${HOST_IP:?set the test VM address}
ORIGIN_SERVER_NAME=${ORIGIN_SERVER_NAME:?set the certificate DNS name}
cd "$(dirname "$0")"
umask 077
mkdir -p tls
if [ -e tls/server.key ] || [ -e tls/ca.key ]; then
    echo 'TLS identity already exists; refusing to overwrite it' >&2
    exit 1
fi
openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days 30 \
    -keyout tls/ca.key -out tls/ca.crt -subj '/CN=OLCWave Test CA' \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -addext 'subjectKeyIdentifier=hash'
openssl req -new -newkey rsa:2048 -nodes -sha256 \
    -keyout tls/server.key -out tls/server.csr -subj '/CN=olcwave-test'
printf '%s\n' "subjectAltName=IP:${HOST_IP},DNS:${ORIGIN_SERVER_NAME}" \
    'basicConstraints=critical,CA:FALSE' 'extendedKeyUsage=serverAuth' \
    'subjectKeyIdentifier=hash' 'authorityKeyIdentifier=keyid,issuer' > tls/server.ext
openssl x509 -req -sha256 -days 30 -in tls/server.csr \
    -CA tls/ca.crt -CAkey tls/ca.key -CAcreateserial -out tls/server.crt \
    -extfile tls/server.ext
openssl verify -CAfile tls/ca.crt tls/server.crt
chmod 600 tls/*
echo 'Use tls/ca.crt as the trust anchor on isolated Ghostlane test clients.'
