#!/usr/bin/env bash
#
# Stand up a ground station. Writes `.env`, then starts the container.
#
#   cd station/deploy && ./bootstrap.sh
#
# ---------------------------------------------------------------------------
# WHAT THIS DELIBERATELY DOES NOT DO
#
# It installs nothing, configures no systemd unit, adds no udev rule and
# touches no host package. The version this replaces did all of those — five
# hundred lines of them — because the agent used to run on the host, and the
# CSI camera was the one thing that could not work in a container.
#
# The camera is out of scope and the agent runs in a container. The host needs
# Docker, and nothing else.
#
# Updating is:
#
#     git pull && docker compose up -d --build
#
# There is no updater daemon, no image registry and no rollback tooling: the
# checkout is the source of truth, and going back is `git checkout` of a tag
# already on the disk. On a link that may be the reason you are rolling back,
# not needing to download anything is the point.
#
# Running this again after changing your mind is the supported way to change
# your mind: it reads the existing `.env` for its defaults and rewrites it.
# ---------------------------------------------------------------------------

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
ENV_FILE=".env"

die() { printf '\n%s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 \
  || die "Docker is not installed. That is the only thing this box needs.
Install it (https://docs.docker.com/engine/install/) and run this again."

docker compose version >/dev/null 2>&1 \
  || die "Docker is installed but the compose plugin is not.
Install docker-compose-plugin and run this again."

# Existing values become the defaults, so re-running is safe and changing one
# answer does not mean retyping the rest.
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "./$ENV_FILE"
  set +a
  printf 'Reading your existing answers from %s.\n\n' "$ENV_FILE"
fi

ask() {
  local var="$1" prompt="$2" default="${3-}" current="${!1-}" answer
  default="${current:-$default}"
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " answer || true
    answer="${answer:-$default}"
  else
    read -r -p "$prompt: " answer || true
  fi
  printf -v "$var" '%s' "$answer"
}

cat <<'INTRO'
Percepta ground station
=======================

Two answers are needed. Everything else has a working default, and every key
is documented in gsu.env.example.

INTRO

# No default, ever. A station pointed at the wrong platform enrols against the
# wrong one and looks entirely healthy doing it.
ask GSU_PLATFORM_URL "Platform URL (https://...)"
[ -n "${GSU_PLATFORM_URL:-}" ] || die "The platform URL is required."

case "$GSU_PLATFORM_URL" in
  https://*) ;;
  http://127.0.0.1*|http://localhost*) ;;
  http://*)
    die "$GSU_PLATFORM_URL is plaintext.
Enrolment carries a single-use token and returns this station's credential;
the agent refuses to send either unencrypted. Use https://." ;;
  *) die "That does not look like a URL." ;;
esac

ask GSU_SITE_NAME "A name for this site (shown on the local setup page)" "ground station"

umask 077
cat > "$ENV_FILE" <<EOF
# Written by bootstrap.sh. Gitignored on purpose: this holds a site's settings
# and \`git pull\` must never clobber it. gsu.env.example documents every key
# this station understands.
GSU_PLATFORM_URL=$GSU_PLATFORM_URL
GSU_SITE_NAME=$GSU_SITE_NAME
EOF

printf '\nWrote %s.\n\n' "$ENV_FILE"

docker compose up -d --build

cat <<'NEXT'

Running.

  Enrol it        docker compose run --rm gsu enrol --token XXXX-XXXX-XXXX
  Check hardware  docker compose run --rm gsu preflight --probe
  Setup page      http://127.0.0.1:8088   (loopback only, no password --
                  physical presence or an SSH tunnel is the control)
  Logs            docker compose logs -f
  Update          git pull && docker compose up -d --build

The enrolment token comes from the platform, is single-use and short-lived.
Until this station is enrolled it senses, records and alerts locally and
publishes nothing, which is correct behaviour rather than a failure.
NEXT
