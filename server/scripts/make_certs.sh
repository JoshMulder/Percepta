#!/usr/bin/env bash
# Create the private CA that ground stations pin, plus server certificates for
# the broker and the API.
#
# Why a private CA rather than Let's Encrypt: a station is unattended kit on a
# link the platform can never initiate a connection over, and it should trust
# exactly one issuer - ours - rather than every public CA on earth. That is what
# `contract/enrolment.md` §4 means by "ca_pem: pinned; the station verifies the
# platform", and it is stronger here than public PKI, not weaker. Public certs
# are the right answer for the browser console on a real domain; they are not
# the right answer for machine-to-machine on a fixed pair of endpoints.
#
# Idempotent: existing files are left alone, so re-running never invalidates a
# CA that stations have already pinned. Delete certs/ deliberately to rotate,
# and understand that every enrolled station must be re-enrolled afterwards.
#
#   ./scripts/make_certs.sh                 # localhost only
#   PERCEPTA_HOSTS=percepta.example.com,203.0.113.10 ./scripts/make_certs.sh
#
# The CA key never leaves this directory and is never served. certs/ is
# gitignored; back the CA up somewhere else, because losing it means re-enrolling
# every station in the field.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/certs"
DAYS_CA=3650
DAYS_LEAF=825   # Longer is refused by modern TLS stacks; renew before this.

mkdir -p "$DIR"
chmod 700 "$DIR"

# Every name or address a station might legitimately connect to. A certificate
# that omits the address actually used fails verification, and the failure is
# deliberately not recoverable by turning verification off.
HOSTS="${PERCEPTA_HOSTS:-localhost}"
ALT="DNS:localhost,IP:127.0.0.1"
IFS=',' read -ra ENTRIES <<< "$HOSTS"
for h in "${ENTRIES[@]}"; do
  h="$(echo "$h" | xargs)"
  [ -z "$h" ] && continue
  if [[ "$h" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    ALT="$ALT,IP:$h"
  else
    ALT="$ALT,DNS:$h"
  fi
done

if [ ! -f "$DIR/ca.key" ]; then
  echo "Creating the Percepta CA (valid ${DAYS_CA} days)."
  openssl genrsa -out "$DIR/ca.key" 4096 2>/dev/null
  chmod 600 "$DIR/ca.key"
  # basicConstraints and keyUsage are not optional. Without them OpenSSL's
  # command line will happily use the certificate and Python's ssl module will
  # refuse it - "CA cert does not include key usage extension" - which is a
  # confusing failure to hit for the first time on a device in a field.
  openssl req -x509 -new -nodes -key "$DIR/ca.key" -sha256 -days "$DAYS_CA" \
    -subj "/O=Percepta/CN=Percepta Station CA" -out "$DIR/ca.crt" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" 2>/dev/null
else
  echo "CA already exists; leaving it alone (stations have pinned it)."
fi

issue() {
  local name="$1"
  if [ -f "$DIR/$name.crt" ]; then
    echo "$name certificate exists; leaving it alone."
    return
  fi
  echo "Issuing $name certificate for: $ALT"
  openssl genrsa -out "$DIR/$name.key" 2048 2>/dev/null
  chmod 600 "$DIR/$name.key"
  openssl req -new -key "$DIR/$name.key" -subj "/O=Percepta/CN=$name" \
    -out "$DIR/$name.csr" 2>/dev/null
  openssl x509 -req -in "$DIR/$name.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" \
    -CAcreateserial -out "$DIR/$name.crt" -days "$DAYS_LEAF" -sha256 \
    -extfile <(printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n" "$ALT") \
    2>/dev/null
  rm -f "$DIR/$name.csr"
}

issue redis
issue api

# Each private key is readable only by the container that needs it, by group.
# The alternative people usually reach for is 644, and a world-readable private
# key is one that leaves with any careless backup or stray archive. Group ids
# are the ones inside the images: redis:7-alpine runs as gid 1000, and the API
# image runs as uid/gid 10001.
#
# Certificates are public by definition and stay 644. The directory has to be
# traversable, or the container cannot reach the files at all.
REDIS_GID="${PERCEPTA_REDIS_GID:-1000}"
APP_GID="${PERCEPTA_APP_GID:-10001}"

chmod 755 "$DIR"
chmod 644 "$DIR"/*.crt
chmod 640 "$DIR"/redis.key "$DIR"/api.key "$DIR"/ca.key 2>/dev/null || true
chgrp "$REDIS_GID" "$DIR/redis.key" 2>/dev/null || \
  echo "  note: could not chgrp redis.key to $REDIS_GID - Redis may not read it"
chgrp "$APP_GID" "$DIR/api.key" 2>/dev/null || \
  echo "  note: could not chgrp api.key to $APP_GID - the API may not read it"
# The CA private key is used by nothing at runtime. It signs certificates and
# otherwise belongs in a safe.
chmod 600 "$DIR/ca.key"

echo
echo "Done. certs/ contains:"
ls -1 "$DIR"
echo
echo "The CA fingerprint stations will pin:"
openssl x509 -in "$DIR/ca.crt" -noout -fingerprint -sha256
echo
echo "Back up ca.key somewhere other than this host. Losing it means"
echo "re-enrolling every station in the field."
