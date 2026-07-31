#!/usr/bin/env bash
#
# Stand up a ground station from a fresh clone on a fresh box. One command.
#
#   sudo station/deploy/bootstrap.sh --platform 192.168.2.49
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS
#
# Everything below was already possible and already documented, across sixteen
# sections of DEPLOYMENT.md, five compose files, an installer with flags, a
# separate password command, a hand-edited environment file and one `.env` that
# was written up nowhere at all. Each piece had a reason. The sum was a day's
# work to repeat and a list nobody could hold in their head, which is its own
# kind of defect: a deployment you get wrong quietly is worse than one that is
# hard to start.
#
# So this is the sequence, executable. It does nothing the documentation did
# not already say to do — read `install.sh` for the part that actually installs
# — and it is idempotent, so running it again after changing your mind about a
# flag is the supported way to change your mind.
#
# What it will not do is guess about the platform. There is no default for
# `--platform`, because a station pointed at the wrong one enrols against the
# wrong one and looks entirely healthy doing it.
# ---------------------------------------------------------------------------

set -euo pipefail

PLATFORM=""
CA_FILE=""
DEMO=0
LAN=1
DEPLOY_PATH=""
SETUP_PASSWORD=""
BLACKLIST_SDR=""
ASSUME_YES=0

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)   # …/station/deploy
SRC=$(cd "$HERE/.." && pwd)                          # …/station
PREFIX=/opt/percepta/station
ETC=/etc/percepta
SERVICE_USER=gsu

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
warn() { printf '   \033[33m! %s\033[0m\n' "$*" >&2; }
die()  { printf '\n\033[31mbootstrap: %s\033[0m\n' "$*" >&2; exit 1; }

# --- the three things that differ between the two paths ---------------------
#
# Kept together rather than scattered through the script, so "what does the
# systemd path do differently" has one answer you can read in one place.

compose() {
  docker compose --project-directory "$PREFIX/deploy" "$@"
}

# The password, hashed by the agent's own code so the algorithm cannot drift
# apart from the one that checks it. Piped, never in argv.
hash_password() {   # password on stdin
  if [ "$DEPLOY_PATH" = docker ]; then
    compose run --rm -T gsu setup-password --stdin
  else
    sudo -u "$SERVICE_USER" "$PREFIX/.venv/bin/python" -m gsu setup-password --stdin
  fi
}

start_station() {
  if [ "$DEPLOY_PATH" = docker ]; then
    compose up -d
  else
    systemctl enable --now gsu.service
    systemctl restart gsu.service
  fi
}

preflight() {
  if [ "$DEPLOY_PATH" = docker ]; then
    compose run --rm -T gsu preflight || true
  else
    sudo -u "$SERVICE_USER" env "$(grep -v '^#' "$ETC/gsu.env" | xargs)" \
      "$PREFIX/.venv/bin/python" -m gsu preflight || true
  fi
}

enrol_hint() {
  if [ "$DEPLOY_PATH" = docker ]; then
    echo "cd $PREFIX/deploy && sudo docker compose run --rm gsu enrol --token XXXX-XXXX-XXXX"
  else
    echo "sudo -u $SERVICE_USER $PREFIX/.venv/bin/python -m gsu enrol --token XXXX-XXXX-XXXX"
  fi
}

usage() {
  sed -n '3,6p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  cat <<'EOF'

  --platform HOST     the platform's hostname or IP. Required.
  --ca FILE           the platform's CA certificate, to pin. Strongly advised
                      while the platform serves its own certificate.
  --demo              provision as a demo box: every slot simulated.
  --loopback          keep the setup page on 127.0.0.1. Default is the LAN,
                      which is what a box on a bench is for.
  --path docker|systemd
                      default docker. A station with a CSI camera must use
                      systemd — see DEPLOYMENT.md §3 — and this detects one
                      and refuses to give you the wrong answer silently.
  --password PASS     the setup page's password. Generated and printed if
                      you do not choose one.
  --sdr / --no-sdr    blacklist the kernel's DVB driver, which grabs RTL2832U
                      dongles on sight. Detected if you say neither.
  --yes               do not ask anything.

EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --platform)  PLATFORM="${2:-}"; shift 2 ;;
    --ca)        CA_FILE="${2:-}"; shift 2 ;;
    --demo)      DEMO=1; shift ;;
    --loopback)  LAN=0; shift ;;
    --lan)       LAN=1; shift ;;
    --path)      DEPLOY_PATH="${2:-}"; shift 2 ;;
    --password)  SETUP_PASSWORD="${2:-}"; shift 2 ;;
    --sdr)       BLACKLIST_SDR=1; shift ;;
    --no-sdr)    BLACKLIST_SDR=0; shift ;;
    --yes|-y)    ASSUME_YES=1; shift ;;
    -h|--help)   usage; exit 0 ;;
    *)           usage; die "unknown option $1" ;;
  esac
done

[ "$(id -u)" = 0 ] || die "run this with sudo."
[ -n "$PLATFORM" ] || { usage; die "--platform is required."; }
[ -f "$SRC/deploy/install.sh" ] || die "run this from a checkout: $SRC does not look like station/."

# --- what this box is -------------------------------------------------------

say "Looking at this box"

# Python 3.11 is the floor: the agent uses datetime.UTC. Bullseye ships 3.9,
# and that is an OS upgrade rather than something to work around.
PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)' 2>/dev/null || echo 0)
[ "$PY_OK" = 1 ] || die "this box has Python $(python3 -V 2>&1 | cut -d' ' -f2); the agent needs 3.11 or later. On Raspberry Pi OS that means Bookworm or Trixie, not Bullseye."

MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "unknown hardware")
info "$MODEL"
info "$(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") on $(uname -m)"

# A CSI camera decides the deployment path, and it is a measurement rather than
# a preference: rpicam-vid takes a bus error inside the container image and the
# live stream never produces a frame. DEPLOYMENT.md §3 has the numbers.
HAS_CSI=0
if [ -e /dev/video0 ] && command -v rpicam-jpeg >/dev/null 2>&1; then HAS_CSI=1; fi
if [ -z "$DEPLOY_PATH" ]; then
  if [ "$HAS_CSI" = 1 ]; then
    DEPLOY_PATH=systemd
    info "CSI camera present, so taking the systemd path (DEPLOYMENT.md §3)."
  else
    DEPLOY_PATH=docker
  fi
elif [ "$DEPLOY_PATH" = docker ] && [ "$HAS_CSI" = 1 ]; then
  warn "a CSI camera is fitted and you asked for the container path."
  warn "Stills may work; the live stream will not — rpicam-vid bus-errors in"
  warn "that image. DEPLOYMENT.md §3. Continuing because you were explicit."
fi
info "deployment path: $DEPLOY_PATH"

if [ -z "$BLACKLIST_SDR" ]; then
  BLACKLIST_SDR=0
  if lsusb 2>/dev/null | grep -qiE "0bda:283[89]|rtl28|realtek.*dvb"; then
    BLACKLIST_SDR=1
    info "RTL-SDR on the bus; will blacklist the kernel's DVB driver."
  fi
fi

if [ "$ASSUME_YES" = 0 ]; then
  printf '\n   Install to %s, pointing at %s? [Y/n] ' "$PREFIX" "$PLATFORM"
  read -r reply </dev/tty || reply=y
  case "$reply" in [nN]*) die "nothing done." ;; esac
fi

# --- packages ---------------------------------------------------------------

say "Packages"
export DEBIAN_FRONTEND=noninteractive
# chrony even though timesyncd is present: a GPS time source plugs into chrony,
# so installing it now makes that upgrade a config file rather than a change of
# daemon. DEPLOYMENT.md §11.
NEED="chrony rsync"
if [ "$DEPLOY_PATH" = docker ]; then
  NEED="$NEED docker.io"
  # Compose v2 is spelled differently depending on where it comes from, and
  # hardcoding one name is why this failed on Bookworm with "unable to locate
  # package docker-compose-v2" — that name is Trixie's and Ubuntu 24.04's.
  # Debian 12 has it in backports, and Docker's own repository calls it
  # docker-compose-plugin. Ask apt what it actually has.
  #
  # v1 (`docker-compose`, the Python one) is deliberately not a candidate:
  # everything here invokes `docker compose` as a subcommand, which v1 does
  # not provide, and installing it would satisfy the check and fail later.
  COMPOSE_PKG=""
  apt-get update -qq
  for candidate in docker-compose-v2 docker-compose-plugin; do
    if apt-cache show "$candidate" >/dev/null 2>&1; then
      COMPOSE_PKG="$candidate"; break
    fi
  done
  if [ -n "$COMPOSE_PKG" ]; then
    NEED="$NEED $COMPOSE_PKG"
  elif docker compose version >/dev/null 2>&1; then
    info "compose v2 already present, not from apt"
  else
    die "no Compose v2 package in this box's apt sources, and \`docker compose\`
   does not work. On Debian 12 / Raspberry Pi OS Bookworm either enable
   backports:

     echo 'deb http://deb.debian.org/debian bookworm-backports main' \\
       | sudo tee /etc/apt/sources.list.d/backports.list
     sudo apt update && sudo apt install -y -t bookworm-backports docker-compose-v2

   or install Docker's own packages from https://get.docker.com, which bring
   docker-compose-plugin. Then run this again.

   Or take the other path, which needs no Docker at all:
     sudo $0 --platform $PLATFORM --path systemd"
  fi
else
  NEED="$NEED python3-venv"
fi
MISSING=""
for pkg in $NEED; do
  dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
done
if [ -n "$MISSING" ]; then
  # A half-configured package from an earlier interrupted install makes every
  # subsequent apt fail with "dpkg returned an error code (1)" and nothing
  # about the actual cause. Clearing it first is what the error would have told
  # you to do, three screens up.
  if dpkg --audit 2>/dev/null | grep -q .; then
    info "finishing an interrupted package install first"
    dpkg --configure -a || true
  fi

  # Disk, before rather than after. A Pi that fills up mid-unpack leaves dpkg
  # in exactly the state above, and the message it gives is about dpkg.
  FREE_MB=$(df -Pm / | awk 'NR==2 {print $4}')
  [ "${FREE_MB:-0}" -ge 1200 ] || die "only ${FREE_MB} MB free on /. Docker and its
   dependencies need more than that, and running out part-way leaves dpkg
   half-configured. Expand the filesystem (\`sudo raspi-config\`) or clear space."

  info "installing:$MISSING"
  [ "$DEPLOY_PATH" = docker ] || apt-get update -qq
  # NOT quiet. This used to be `-qq >/dev/null`, which turned a package
  # conflict into six words with the explanation discarded — the one thing you
  # need when an install fails is what apt said about it.
  # shellcheck disable=SC2086
  if ! apt-get install -y $MISSING; then
    printf '\n'
    warn "that install failed; the reason is in apt's output above."
    if dpkg -l docker-ce 2>/dev/null | grep -q '^ii'; then
      warn "docker-ce is installed. It conflicts with Debian's docker.io and"
      warn "with containerd — you already have Docker, so re-run with:"
      warn "  --path docker   (skipping docker.io: apt install -y chrony rsync)"
    fi
    die "packages not installed; nothing else was changed."
  fi
else
  info "already present"
fi
[ "$DEPLOY_PATH" = docker ] && systemctl enable --now docker >/dev/null 2>&1 || true

# The SD card is the most likely hardware failure at a remote site, and this box
# writes an event database and audio continuously. These are the largest
# incidental writers and none of them is load-bearing here.
systemctl disable --now man-db.timer apt-daily.timer apt-daily-upgrade.timer \
  >/dev/null 2>&1 || true

REBOOT_NEEDED=0
if [ "$BLACKLIST_SDR" = 1 ]; then
  say "RTL-SDR"
  # The kernel's DVB driver claims RTL2832U devices on sight and then nothing
  # else can open them. The symptom is "device busy" on a dongle nothing is
  # using, which reads as broken hardware.
  printf 'blacklist dvb_usb_rtl28xxu\nblacklist rtl2832\nblacklist rtl2830\n' \
    > /etc/modprobe.d/blacklist-rtlsdr.conf
  if lsmod | grep -qE "dvb_usb_rtl28xxu|rtl2832"; then
    REBOOT_NEEDED=1
    info "blacklisted; the driver is loaded now, so this needs a reboot."
  else
    info "blacklisted."
  fi
fi

# --- install ----------------------------------------------------------------

say "Installing"
INSTALL_ARGS=(--path "$DEPLOY_PATH")
if [ -n "$CA_FILE" ]; then
  # Nothing can fetch this for you. The platform sends only its leaf
  # certificate, not the chain, so the CA is not on the wire — and a CA pulled
  # from the thing it is meant to authenticate would not be worth pinning
  # anyway. It has to be copied from the platform host.
  [ -f "$CA_FILE" ] || die "no such CA file: $CA_FILE

   Copy it from the platform host first, then run this again:

     scp <you>@${PLATFORM}:~/percepta/server/certs/ca.crt $CA_FILE

   Check it is the right one before trusting it — this should match what
   the platform host prints for the same file:

     openssl x509 -in $CA_FILE -noout -subject -fingerprint -sha256"
  openssl x509 -in "$CA_FILE" -noout -subject >/dev/null 2>&1 \
    || die "$CA_FILE is not a PEM certificate."
  info "pinning $(openssl x509 -in "$CA_FILE" -noout -subject | sed 's/^subject=//')"
  info "  $(openssl x509 -in "$CA_FILE" -noout -fingerprint -sha256 | cut -d= -f2)"
  INSTALL_ARGS+=(--api-ca "$CA_FILE")
else
  warn "no --ca given, so this station trusts only the system CA store."
  warn "If the platform serves its own certificate — which it does today —"
  warn "every connection will fail TLS verification and the station will"
  warn "look enrolled and mute. Right only behind a publicly trusted cert."
fi
"$SRC/deploy/install.sh" "${INSTALL_ARGS[@]}"

# --- configuration ----------------------------------------------------------

say "Configuring"

# In place, by key. Appending would leave two of everything and the last one
# quietly winning, which is a bad way to find out your broker URL is stale.
set_env() {
  local key="$1" value="$2" file="$ETC/gsu.env"
  if grep -qE "^${key}=" "$file"; then
    # `value` can hold slashes and $; use a delimiter it cannot contain and let
    # awk do the substitution rather than sed's expression parsing.
    awk -v k="$key" -v v="$value" \
      'BEGIN{FS=OFS="="} $1==k {print k "=" v; next} {print}' \
      "$file" > "$file.tmp" && mv "$file.tmp" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
  chown root:gsu "$file"; chmod 0640 "$file"
}

set_env GSU_PLATFORM_URL "https://${PLATFORM}:8000"
set_env GSU_BROKER_URL   "rediss://${PLATFORM}:6380/0"
set_env GSU_DEMO         "$DEMO"
# On the container path this is the *container's* namespace, and what the
# outside world can reach is decided by the port mapping below — so it is
# always 0.0.0.0 and --loopback is expressed by dropping the LAN overlay. On
# the systemd path there is no mapping and no namespace: this binding is the
# whole of the exposure, so --loopback has to be said here instead.
if [ "$DEPLOY_PATH" = docker ] || [ "$LAN" = 1 ]; then
  set_env GSU_SETUP_HOST 0.0.0.0
else
  set_env GSU_SETUP_HOST 127.0.0.1
fi
[ -n "$CA_FILE" ] && set_env GSU_API_CA_FILE "$ETC/platform-api-ca.pem"
info "platform: https://${PLATFORM}:8000"
info "broker:   rediss://${PLATFORM}:6380/0"
[ "$DEMO" = 1 ] && info "demo box: every slot simulated"

# The setup page refuses to bind anywhere but loopback without a password hash,
# deliberately — a box nobody gave a password to must not serve an open form on
# a routable address. So this is not optional, and generating one is kinder
# than failing later with a page that exists and cannot be reached.
GENERATED=0
if ! grep -q "^GSU_SETUP_PASSWORD_HASH=." "$ETC/gsu.env"; then
  if [ -z "$SETUP_PASSWORD" ]; then
    SETUP_PASSWORD=$(tr -dc 'a-z2-9' </dev/urandom | head -c 16)
    GENERATED=1
  fi
  HASH=$(printf '%s' "$SETUP_PASSWORD" | hash_password | tail -1) || HASH=""
  case "$HASH" in
    pbkdf2_sha256:*) set_env GSU_SETUP_PASSWORD_HASH "$HASH" ;;
    *) die "could not hash the setup password. Run 'gsu setup-password' by hand and paste the line into $ETC/gsu.env." ;;
  esac
elif [ -n "$SETUP_PASSWORD" ]; then
  HASH=$(printf '%s' "$SETUP_PASSWORD" | hash_password | tail -1) || HASH=""
  case "$HASH" in pbkdf2_sha256:*) set_env GSU_SETUP_PASSWORD_HASH "$HASH" ;; esac
else
  info "setup password already set; left alone."
fi

if [ "$DEPLOY_PATH" = docker ]; then
  # Compose reads this from the project directory, so it survives every later
  # `docker compose up` without anybody having to remember a -f. Its absence
  # is what leaves a bench station's setup page bound to loopback and
  # unreachable, with nothing anywhere saying why.
  if [ "$LAN" = 1 ]; then
    printf 'COMPOSE_FILE=docker-compose.yml:docker-compose.lan.yml\n' \
      > "$PREFIX/deploy/.env"
    info "setup page published on the LAN"
  else
    rm -f "$PREFIX/deploy/.env"
    info "setup page on loopback only"
  fi
fi

# --- start ------------------------------------------------------------------

say "Starting"
if [ "$REBOOT_NEEDED" = 1 ]; then
  warn "not starting: the DVB driver is still loaded. Reboot, then re-run this."
  exit 0
fi
preflight
start_station
sleep 6

ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')
say "Done"
if [ "$LAN" = 1 ] && [ -n "$ADDR" ]; then
  info "Setup page:  http://${ADDR}:8088"
else
  info "Setup page:  http://127.0.0.1:8088  (loopback only)"
fi
if [ "$GENERATED" = 1 ]; then
  printf '   \033[1mSetup password: %s\033[0m\n' "$SETUP_PASSWORD"
  info "Written nowhere else. Note it down now."
fi
info ""
info "Enrol it from that page, or with a token:"
info "  $(enrol_hint)"
