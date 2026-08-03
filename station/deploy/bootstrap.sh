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

# The setup page needs a password, and the station is right to insist.
#
# It binds 0.0.0.0 *inside the container* — the container's network namespace
# is the boundary, and compose publishes the port to the host's loopback only.
# But the agent cannot see that from inside, so it applies the rule it can
# check: an unauthenticated form must never be offered on a routable
# interface. Without a password it demotes itself to the container's own
# loopback, which Docker's port forward cannot reach, and the page is then
# unreachable from anywhere at all.
#
# So this is not optional hardening; it is what makes the page exist.
# **Before the first `docker compose` call that loads the project.**
#
# docker-compose.yml declares `env_file: - .env`, and compose treats a missing
# env file as a hard error rather than an empty one. The password hashing below
# is a `docker compose run`, so on a fresh clone — where this file does not
# exist yet — compose refused before the container started, `set -e` ended
# bootstrap on the spot, and the redirect that was there to hide build noise hid
# the reason as well. What that looked like: answer three questions, type a
# password, and get back a prompt with no .env, no container and nothing said.
#
# It survived because a *re-run* has a .env by then and skips the block
# entirely, which is the path anybody debugging this would take.
#
# Created empty rather than written early: the answers this script is still
# collecting are what belong in it, and it is rewritten in full below.
[ -f "$ENV_FILE" ] || : > "$ENV_FILE"

if [ -z "${GSU_SETUP_PASSWORD_HASH:-}" ]; then
  echo
  echo "The setup page needs a password. It is published to this host's"
  echo "loopback only, so reaching it remotely still needs an SSH tunnel."
  read -r -s -p "Setup page password: " SETUP_PW || true
  echo
  if [ -n "${SETUP_PW:-}" ]; then
    # stderr is deliberately not swallowed. The first run builds the image,
    # which is minutes on a Pi, and hiding it left somebody watching a terminal
    # that looked hung. The hash is the whole of stdout — `--stdin` prints no
    # banner — so build progress on stderr cannot contaminate it.
    echo "Hashing it. The first run builds the image, which takes a few minutes."
    GSU_SETUP_PASSWORD_HASH=$(printf '%s' "$SETUP_PW" \
      | docker compose run --rm -T gsu setup-password --stdin | tail -1) \
      || die "Could not hash the setup password; the error above is docker
compose's. Nothing was written."
    unset SETUP_PW
    [ -n "$GSU_SETUP_PASSWORD_HASH" ] || die "The hasher produced nothing.
Without a hash the agent demotes the setup page to the container's own
loopback, which the published port cannot reach — so the page would be
unreachable from anywhere at all. Stopping rather than writing a file that
looks complete."
  fi
fi
GSU_SETUP_PASSWORD_HASH="${GSU_SETUP_PASSWORD_HASH:-}"

umask 077
cat > "$ENV_FILE" <<EOF
# Written by bootstrap.sh. Gitignored on purpose: this holds a site's settings
# and \`git pull\` must never clobber it. gsu.env.example documents every key
# this station understands.
GSU_PLATFORM_URL=$GSU_PLATFORM_URL
GSU_SITE_NAME=$GSU_SITE_NAME
GSU_SETUP_PASSWORD_HASH=$GSU_SETUP_PASSWORD_HASH
EOF

printf '\nWrote %s.\n\n' "$ENV_FILE"

docker compose up -d --build

cat <<'NEXT'

Running.

  Enrol it        docker compose run --rm gsu enrol --token XXXX-XXXX-XXXX
  Check hardware  docker compose run --rm gsu preflight --probe
  Setup page      http://127.0.0.1:8088   (this host's loopback only; from
                  elsewhere: ssh -L 8088:127.0.0.1:8088 <this host>)
  Logs            docker compose logs -f
  Update          git pull && docker compose up -d --build

The enrolment token comes from the platform, is single-use and short-lived.
Until this station is enrolled it senses, records and alerts locally and
publishes nothing, which is correct behaviour rather than a failure.
NEXT
