#!/usr/bin/env bash
#
# Stand up a ground station. Writes `.env`, then starts the container.
#
#   cd station && ./bootstrap.sh
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

# The daemon, not just the client. Neither check above connects to it —
# `command -v docker` finds the binary and `docker compose version` reports the
# plugin — so a box where this user cannot reach /var/run/docker.sock passed
# both and failed minutes later, inside the password loop, where "permission
# denied on the socket" read as the password being rejected. Fail here instead,
# before a single question, and fix the usual cause: the docker group.
if ! docker_diag=$(docker info 2>&1); then
  case "$docker_diag" in
    *"permission denied"*)
      me=$(id -un)
      if id -nG "$me" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
        die "You are in the 'docker' group, but this login session began before
that and cannot see it yet. Log out and back in — or run 'newgrp docker' in
this terminal — and run this again."
      fi
      echo "You cannot reach the Docker daemon, and '$me' is not in the 'docker'"
      echo "group that grants it. Adding you needs sudo, and only takes effect in"
      echo "a new login session."
      read -r -p "Add $me to the docker group now? [Y/n]: " reply || true
      case "${reply:-Y}" in
        [Nn]*) die "Not added. Do it by hand, then re-run this:
    sudo usermod -aG docker $me" ;;
      esac
      command -v sudo >/dev/null 2>&1 \
        || die "sudo is not available. As root, run:
    usermod -aG docker $me
then have $me log out and back in, and re-run this."
      sudo usermod -aG docker "$me" \
        || die "Could not add $me to the docker group. As root, run:
    usermod -aG docker $me"
      die "Added $me to the 'docker' group. **Log out and back in** — or run
'newgrp docker' in this terminal — so it takes effect, then run this script
again. Group membership only applies to a new session, so this one still
cannot reach the daemon."
      ;;
    *)
      die "Docker is installed but its daemon did not answer:

$docker_diag

If it is not running, start it (on most systems: sudo systemctl start docker)
and run this again." ;;
  esac
fi

# Existing values become the defaults, so re-running is safe and changing one
# answer does not mean retyping the rest.
#
# `-s`, not `-f`: an empty one exists after a run that got as far as creating it
# and no further, and announcing that answers were read from a file with none in
# it is a small lie that costs somebody a puzzled minute.
# **Read, never sourced.**
#
# `.` runs the file as shell, and a pbkdf2 hash is full of dollars:
# `pbkdf2_sha256$120000$salt$hash` expands `$120000` to positional parameter 1,
# which under `set -u` is an error that ends the script. So re-running bootstrap
# — offered at the top of this file as the way to change your mind — died on
# every box that had a password set, and only on those. A value in here is data
# and must never be evaluated.
#
# The last variable takes the rest of the line, so a value containing `=` or `$`
# survives intact, and the GSU_ guard stops a stray line setting anything else.
if [ -s "$ENV_FILE" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      GSU_*) printf -v "$key" '%s' "$value" ;;
    esac
  done < "$ENV_FILE"
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
is documented in deploy/gsu.env.example.

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
  echo "At least 10 characters. Ctrl-C to give up."

  # **Asked again rather than aborted.**
  #
  # The agent is the authority on what it will accept — it is the thing that
  # checks this password later — so the rule is not restated here, where it
  # would drift. What was wrong was the consequence: a password one character
  # short ended the whole run, after three answers and a five-minute image
  # build, with a message that blamed docker compose for what was actually the
  # agent saying "too short". For somebody standing at an enclosure that is a
  # long way to go to retype one word.
  #
  # Bounded, so a docker fault that is nothing to do with the password cannot
  # turn into an endless prompt.
  attempts=0
  while [ -z "${GSU_SETUP_PASSWORD_HASH:-}" ]; do
    attempts=$((attempts + 1))
    if [ "$attempts" -gt 3 ]; then
      die "Three attempts, no hash. If the message above is not about the
password then the fault is not here: run

    docker compose run --rm gsu setup-password

by hand, and put the GSU_SETUP_PASSWORD_HASH line it prints into .env."
    fi
    read -r -s -p "Setup page password: " SETUP_PW || die "Nothing read."
    echo
    if [ -z "$SETUP_PW" ]; then
      # An empty one is not "no password wanted". Without a hash the agent
      # demotes the page to the container's own loopback, which the published
      # port cannot reach, so the page ends up unreachable from anywhere.
      echo "  Nothing typed. The page cannot be served without one."
      continue
    fi
    # stderr is deliberately not swallowed: the first run builds the image,
    # which is minutes on a Pi, and hiding it left somebody watching a terminal
    # that looked hung. It also carries the agent's own reason for refusing a
    # password, which is the sentence the operator needs. The hash is the whole
    # of stdout — `--stdin` prints no banner — so neither can contaminate it.
    echo "Hashing it. The first run builds the image, which takes a few minutes."
    GSU_SETUP_PASSWORD_HASH=$(printf '%s' "$SETUP_PW" \
      | docker compose run --rm -T gsu setup-password --stdin | tail -1) || true
    unset SETUP_PW
    [ -n "${GSU_SETUP_PASSWORD_HASH:-}" ] || echo "  Not accepted — see above."
  done
fi
GSU_SETUP_PASSWORD_HASH="${GSU_SETUP_PASSWORD_HASH:-}"

umask 077
cat > "$ENV_FILE" <<EOF
# Written by bootstrap.sh. Gitignored on purpose: this holds a site's settings
# and \`git pull\` must never clobber it. deploy/gsu.env.example documents
# every key this station understands.
GSU_PLATFORM_URL=$GSU_PLATFORM_URL
GSU_SITE_NAME=$GSU_SITE_NAME
GSU_SETUP_PASSWORD_HASH=$GSU_SETUP_PASSWORD_HASH
EOF

printf '\nWrote %s.\n\n' "$ENV_FILE"

# The build is the longest and likeliest step to fail — it fetches Debian
# packages and clones whisper.cpp and the rtl-sdr driver from GitHub — and
# `set -e` would end the script on it with only docker's raw error, no sense of
# what to do with a half-finished deploy. Catch it, and say.
if ! docker compose up -d --build; then
  die "The image build or container start FAILED — the error is above.

Your answers are saved in $ENV_FILE, so re-running asks nothing again. The
build needs the network, and the usual causes on a fresh box are:

  * No or limited internet. The build pulls Debian packages and clones
    whisper.cpp and the rtl-sdr driver — it cannot run fully offline.
  * A wrong system clock. apt and TLS reject repositories as 'not valid yet'
    when the date is off. Check with 'date'; if it is wrong, fix it
    (e.g. 'sudo timedatectl set-ntp true') and try again.
  * A transient Debian-mirror or GitHub hiccup — just run this again.

When the cause is fixed, re-run:   ./bootstrap.sh
Or build on its own to see it:     docker compose build"
fi

cat <<'NEXT'

Running.

  Enrol it        docker compose run --rm gsu enrol --token XXXX-XXXX-XXXX
  Check hardware  docker compose run --rm gsu preflight --probe
  Setup page      http://<this box>/      (port 80, on the site LAN. The
                  password you just set is what protects it)
  Logs            docker compose logs -f
  Update          git pull && docker compose up -d --build

The enrolment token comes from the platform, is single-use and short-lived.
Until this station is enrolled it senses, records and alerts locally and
publishes nothing, which is correct behaviour rather than a failure.
NEXT
