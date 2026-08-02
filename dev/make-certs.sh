#!/usr/bin/env bash
# Generate the dev Prosody certificate.
#
# One self-signed cert with SANs for every host the dev server serves. Clients trust it
# explicitly (ca_certs), so STARTTLS is genuinely exercised rather than disabled -- which
# matters, because slixmpp's reaction to an untrusted chain is to leave the stream hanging
# with no authentication and no error, which is a miserable thing to debug.
set -euo pipefail

CERT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/prosody/certs"
mkdir -p "$CERT_DIR"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$CERT_DIR/localhost.key" \
  -out "$CERT_DIR/localhost.crt" \
  -days 3650 \
  -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,DNS:conference.localhost,DNS:telegram.localhost,DNS:signal.localhost,IP:127.0.0.1" \
  2>/dev/null

chmod 600 "$CERT_DIR/localhost.key"
echo "wrote $CERT_DIR/localhost.{crt,key}"
