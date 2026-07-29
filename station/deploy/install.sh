#!/usr/bin/env bash
#
# Install the Percepta ground station agent on Raspberry Pi OS.
#
#   sudo ./deploy/install.sh [--path docker|systemd] [--broker-ca FILE]
#                            [--api-ca FILE] [--offline]
#
# --path docker   (default) the container, with the health-gated updater and
#                 its timer. This is the deployment path: an update is atomic
#                 and a rollback is a tag already on the disk, which is what
#                 matters on a station that is hard to reach.
# --path systemd  the agent as a plain systemd service, no Docker. The
#                 documented alternative; DEPLOYMENT.md Appendix B.
#
# Two CAs, because the station verifies two things against two roots:
#   --broker-ca  the broker's private CA. Optional: it normally arrives in the
#                enrolment response. Pre-provision it to check the broker
#                address before enrolling.
#   --api-ca     pins the platform API. Needed while the platform serves its
#                own certificate; NOT needed once it is behind a proxy with a
#                public certificate, which is the direction of travel.
#
# Idempotent: safe to re-run to upgrade an existing install. It never
# overwrites /etc/percepta/gsu.env, the state directory, or an existing device
# inventory — those hold decisions somebody made about this site.
#
# What it does NOT do, deliberately:
#   * enrol the station. That needs a code from an admin and is done from the
#     setup page or `gsu enrol` (contract/enrolment.md §5).
#   * fetch or update itself. There is no answer yet to the software update
#     path (§9.5), and an update mechanism is the same trust root as enrolment;
#     improvised, it is worse than none.
#
# See DEPLOYMENT.md for the runbook this script is step 3 of.

set -euo pipefail

PREFIX=/opt/percepta/station
ETC=/etc/percepta
STATE=/var/lib/percepta-gsu
SERVICE_USER=gsu
UNIT=gsu.service
BROKER_CA=""
API_CA=""
OFFLINE=0
DEPLOY_PATH=docker
# Python 3.11 or newer: the code uses datetime.UTC, which arrived in 3.11.
# Raspberry Pi OS Bookworm ships 3.11.2. Bullseye ships 3.9 and will not run
# this — that is an OS upgrade, not a patch, so it is checked loudly.
PY_MIN_MAJOR=3
PY_MIN_MINOR=11

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mERROR: %s\033[0m\n\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --broker-ca) BROKER_CA="${2:-}"; shift 2 ;;
    --api-ca)    API_CA="${2:-}"; shift 2 ;;
    # The old spelling, from when one CA was used for both. Kept so a written
    # note or a shell history entry does not silently install the wrong thing.
    --ca)        die "--ca is ambiguous now that the broker and the API have
   separate trust roots. Use --broker-ca (the broker's private CA, usually
   delivered by enrolment) or --api-ca (pins the platform API). See
   DEPLOYMENT.md §4." ;;
    --path)      DEPLOY_PATH="${2:-}"; shift 2 ;;
    --offline)   OFFLINE=1; shift ;;
    -h|--help)   sed -n '2,48p' "$0"; exit 0 ;;
    *)           die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run this with sudo."
case "$DEPLOY_PATH" in
  docker|systemd) ;;
  *) die "--path must be 'docker' (default) or 'systemd', not '$DEPLOY_PATH'." ;;
esac

# --- 1. what is this box ---------------------------------------------------
say "Checking the machine"
ARCH="$(uname -m)"
info "architecture: $ARCH"
case "$ARCH" in
  armv7l|armv6l|aarch64) info "ARM — the target hardware." ;;
  *) info "not ARM. This will still install; it is only what the docs assume." ;;
esac

command -v python3 >/dev/null || die "python3 is not installed."
PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 - "$PY_MIN_MAJOR" "$PY_MIN_MINOR" <<'PY' || die \
  "Python $PY_VER is too old. The agent needs 3.11+ (it uses datetime.UTC).
   Raspberry Pi OS Bookworm ships 3.11; Bullseye ships 3.9 and will not run this.
   Upgrade the OS rather than trying to patch around it — an unattended box on an
   unsupported release is a second problem waiting."
import sys
need = (int(sys.argv[1]), int(sys.argv[2]))
sys.exit(0 if sys.version_info[:2] >= need else 1)
PY
info "python: $PY_VER — ok"

if [ "$DEPLOY_PATH" = "docker" ]; then
  command -v docker >/dev/null || die \
    "docker is not installed. Either:
     sudo apt install -y docker.io docker-compose-v2
   or install the systemd path instead: sudo $0 --path systemd"
  docker compose version >/dev/null 2>&1 || die \
    "the docker compose plugin is missing. Install docker-compose-v2."
  info "docker: $(docker --version 2>/dev/null | head -1)"
else
  python3 -c 'import venv' 2>/dev/null || die \
    "python3-venv is missing. Install it: apt install python3-venv"
fi

if [ ! -f /sys/class/rtc/rtc0/name ]; then
  info "no hardware RTC: this box boots with no idea of the time."
  info "  It will refuse to enrol until NTP answers, which is correct but is"
  info "  also a class of unattended failure. See HARDWARE.md §4."
fi

# --- 2. the service account ------------------------------------------------
say "Service account"
if id "$SERVICE_USER" >/dev/null 2>&1; then
  info "user $SERVICE_USER exists."
else
  # System account, no login shell, no home directory: it owns a state
  # directory and two device nodes and has no other business on this box.
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  info "created system user $SERVICE_USER."
fi
for group in dialout video plugdev; do
  if getent group "$group" >/dev/null; then
    usermod -aG "$group" "$SERVICE_USER"
    info "$SERVICE_USER added to $group."
  fi
done

# --- 3. the code -----------------------------------------------------------
say "Installing to $PREFIX"
install -d -m 0755 "$PREFIX"
# Copy the package and the docs, not the developer's state directory: var/
# holds a credential and a device inventory belonging to whichever box it came
# from, and shipping one box's identity onto another is the failure enrolment
# exists to prevent.
for item in gsu requirements.txt README.md DECISIONS.md DEPLOYMENT.md HARDWARE.md \
            CONTRACT-QUESTIONS.md deploy; do
  [ -e "$SRC/$item" ] && cp -r "$SRC/$item" "$PREFIX/"
done
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
chown -R root:root "$PREFIX"
chmod -R go-w "$PREFIX"
info "code owned by root and not writable by the service user: a compromised"
info "agent cannot rewrite the agent."

chmod +x "$PREFIX/deploy/gsu-update.sh" 2>/dev/null || true

if [ "$DEPLOY_PATH" = "systemd" ]; then
  say "Python environment"
  if [ ! -x "$PREFIX/.venv/bin/python" ]; then
    python3 -m venv "$PREFIX/.venv"
    info "created $PREFIX/.venv"
  fi
  PIP_ARGS=(--disable-pip-version-check)
  if [ "$OFFLINE" -eq 1 ]; then
    [ -d "$SRC/deploy/wheels" ] || die \
      "--offline needs pre-downloaded wheels in deploy/wheels. On a machine with
   a network: pip download -r requirements.txt -d deploy/wheels"
    PIP_ARGS+=(--no-index --find-links "$SRC/deploy/wheels")
  fi
  # One runtime dependency, and it is pure Python — there is nothing to compile
  # on ARMv7 and no toolchain needed.
  "$PREFIX/.venv/bin/pip" install "${PIP_ARGS[@]}" -q redis'>=5.0' \
    || die "could not install the redis client. With no network, re-run with --offline."
  info "redis client: $("$PREFIX/.venv/bin/python" -c 'import redis; print(redis.__version__)')"
else
  info "no host venv needed: the image carries its own interpreter and dependency."
fi

# --- 4. configuration ------------------------------------------------------
say "Configuration"
install -d -m 0750 -o root -g "$SERVICE_USER" "$ETC"
if [ -f "$ETC/gsu.env" ]; then
  info "$ETC/gsu.env exists — left alone. Compare it against"
  info "  $PREFIX/deploy/gsu.env.example if this is an upgrade."
else
  install -m 0640 -o root -g "$SERVICE_USER" "$SRC/deploy/gsu.env.example" "$ETC/gsu.env"
  info "wrote $ETC/gsu.env — EDIT IT before starting: it points at example.net."
fi

install_ca() {   # source, destination, description
  [ -f "$1" ] || die "no such CA file: $1"
  grep -q "BEGIN CERTIFICATE" "$1" || die "$1 is not a PEM certificate."
  install -m 0640 -o root -g "$SERVICE_USER" "$1" "$2"
  info "installed the $3:"
  openssl x509 -in "$2" -noout -fingerprint -sha256 2>/dev/null \
    | sed 's/^/      /' || true
}

if [ -n "$BROKER_CA" ]; then
  install_ca "$BROKER_CA" "$ETC/broker-ca.pem" "broker CA"
  info "  Check that fingerprint against the platform. This is one of the two"
  info "  things here a person has to verify by eye."
else
  info "no broker CA pre-provisioned — normal."
  info "  It arrives in the enrolment response and is pinned from then on."
  info "  Pre-provision it with --broker-ca only if you want to check the broker"
  info "  address with 'gsu preflight --probe' before enrolling."
fi

if [ -n "$API_CA" ]; then
  install_ca "$API_CA" "$ETC/platform-api-ca.pem" "platform API CA"
  info "  Check this fingerprint by eye too."
elif [ -f "$ETC/platform-api-ca.pem" ]; then
  info "$ETC/platform-api-ca.pem exists — left alone."
else
  info "NO PLATFORM API CA INSTALLED."
  info "  The API is then verified against the system CA bundle, which is right"
  info "  once it is behind a reverse proxy with a public certificate — and"
  info "  WRONG while the platform serves its own. If enrolment fails with a"
  info "  certificate error, that is this: re-run with --api-ca /path/to/ca.crt"
  info "  and comment GSU_API_CA_FILE back in."
fi

# --- 5. state --------------------------------------------------------------
say "State directory"
# systemd's StateDirectory= creates this too; doing it here as well means
# `gsu preflight` works before the service has ever been started.
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE"
info "$STATE (0700, $SERVICE_USER)"

if [ -f "$STATE/devices.json" ]; then
  info "device inventory exists — left alone. It records decisions about this site."
else
  install -m 0600 -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$SRC/deploy/devices.pi.json" "$STATE/devices.json"
  info "installed the Pi inventory: ping RX Pro, Airmar 110WX, RTL-SDR airband,"
  info "  Pi camera. The two serial ports are EMPTY and must be set on the setup"
  info "  page — only this box knows its own /dev/serial/by-id/ names."
fi

# --- 6. devices ------------------------------------------------------------
say "Device access"
install -m 0644 "$SRC/deploy/99-percepta-sdr.rules" /etc/udev/rules.d/
udevadm control --reload >/dev/null 2>&1 || true
udevadm trigger >/dev/null 2>&1 || true
info "udev rule installed: the SDR is readable by $SERVICE_USER via plugdev."

if [ -d /dev/serial/by-id ]; then
  info "serial ports present:"
  ls -1 /dev/serial/by-id/ | sed 's/^/      \/dev\/serial\/by-id\//'
else
  info "no /dev/serial/by-id — neither USB-UART is plugged in, or neither has"
  info "  enumerated. Check the leads and 'dmesg | tail'."
fi

# --- 7. time ---------------------------------------------------------------
say "Time"
# A wrong clock is the failure that strands a remote site: it cannot
# authenticate, and if it believes its credential expired it cannot renew
# either (contract/enrolment.md §6). NTP at boot is the minimum.
if systemctl list-unit-files | grep -q '^systemd-timesyncd'; then
  systemctl enable --now systemd-timesyncd >/dev/null 2>&1 || true
  info "systemd-timesyncd enabled."
elif systemctl list-unit-files | grep -q '^chrony'; then
  systemctl enable --now chrony >/dev/null 2>&1 || true
  info "chrony enabled."
else
  info "NEITHER systemd-timesyncd NOR chrony is installed. Nothing is keeping"
  info "  this clock. Install one before leaving site: apt install chrony"
fi
timedatectl 2>/dev/null | sed 's/^/      /' || true

# --- 8. the service --------------------------------------------------------
# Both unit files are installed either way, so switching paths later is one
# systemctl command rather than another install. **Only one is ever enabled**:
# two agents publishing independent worlds onto one channel makes the console
# alternate between them and looks like a platform bug.
say "Service"
install -m 0644 "$SRC/deploy/$UNIT" "/etc/systemd/system/$UNIT"
install -m 0644 "$SRC/deploy/gsu-update.service" /etc/systemd/system/
install -m 0644 "$SRC/deploy/gsu-update.timer" /etc/systemd/system/
systemctl daemon-reload

if [ "$DEPLOY_PATH" = "docker" ]; then
  systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
  info "container path: $UNIT installed but DISABLED (the alternative)."

  say "Building the image"
  if docker compose -f "$PREFIX/deploy/docker-compose.yml" build 2>&1 | tail -3; then
    # `current` is the tag compose runs and the updater moves. Point it at what
    # was just built so the first start has something to run.
    docker tag percepta/gsu:current percepta/gsu:previous 2>/dev/null || true
    info "built, tagged percepta/gsu:current"
  else
    info "BUILD FAILED. Fix it before starting: the container has nothing to run."
  fi

  say "Update timer"
  if grep -q '^GSU_UPDATE_REF=.\+' "$ETC/gsu.env" 2>/dev/null; then
    systemctl enable --now gsu-update.timer >/dev/null
    info "gsu-update.timer enabled — checks every 6h with up to 2h of jitter."
  else
    systemctl enable gsu-update.timer >/dev/null
    info "gsu-update.timer enabled but GSU_UPDATE_REF is unset, so it will do"
    info "  nothing. Set it in $ETC/gsu.env when there is a registry to track."
  fi
  info "A bad image is rolled back automatically: see 'gsu-update.sh --status'."
else
  systemctl disable --now gsu-update.timer >/dev/null 2>&1 || true
  systemctl enable "$UNIT" >/dev/null
  info "systemd path: $UNIT installed and enabled (not started — see below)."
  info "  The update timer is disabled: it updates a container, and there"
  info "  isn't one. Upgrade with rsync + re-running this script."
fi

# --- done ------------------------------------------------------------------
if [ "$DEPLOY_PATH" = "docker" ]; then
  COMPOSE="docker compose -f $PREFIX/deploy/docker-compose.yml"
  PREFLIGHT="sudo $COMPOSE run --rm gsu preflight --probe"
  START="sudo $COMPOSE up -d"
  LOGS="sudo $COMPOSE logs -f"
else
  PREFLIGHT="sudo -u $SERVICE_USER $PREFIX/.venv/bin/python -m gsu preflight --probe"
  START="sudo systemctl start $UNIT"
  LOGS="journalctl -u $UNIT -f"
fi

cat <<EOF

$(printf '\033[1m==> Installed (%s path). Three things left, in order:\033[0m' "$DEPLOY_PATH")

  1. Edit the addresses, and the trust settings if the platform is not yet
     behind a proxy with a public certificate:
       sudo nano $ETC/gsu.env

  2. Check everything that must be true before it can work:
       $PREFLIGHT
     Every FAIL is something that will not work; fix them first.

  3. Start it, then enrol it with a code from an admin:
       $START
       $LOGS

     The setup page is on 127.0.0.1:8088, so from your laptop:
       ssh -L 8088:127.0.0.1:8088 <this-box>
     then open http://127.0.0.1:8088 and type the code.

  Nothing is published until it is enrolled, and nothing is published over an
  unverified connection at all. Both of those are visible on the setup page.

EOF
