#!/usr/bin/env bash
#
# Install the Percepta ground station agent on Raspberry Pi OS.
#
#   sudo ./deploy/install.sh [--ca /path/to/platform-ca.pem] [--offline]
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
CA_SOURCE=""
OFFLINE=0
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
    --ca)      CA_SOURCE="${2:-}"; shift 2 ;;
    --offline) OFFLINE=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *)         die "unknown option: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "run this with sudo."

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

python3 -c 'import venv' 2>/dev/null || die \
  "python3-venv is missing. Install it: apt install python3-venv"

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

if [ -n "$CA_SOURCE" ]; then
  [ -f "$CA_SOURCE" ] || die "no such CA file: $CA_SOURCE"
  grep -q "BEGIN CERTIFICATE" "$CA_SOURCE" || die "$CA_SOURCE is not a PEM certificate."
  install -m 0640 -o root -g "$SERVICE_USER" "$CA_SOURCE" "$ETC/platform-ca.pem"
  info "installed the platform CA:"
  openssl x509 -in "$ETC/platform-ca.pem" -noout -fingerprint -sha256 2>/dev/null \
    | sed 's/^/      /' || true
  info "  Check that fingerprint against the platform before enrolling. It is"
  info "  the one thing here that a person has to verify by eye."
elif [ -f "$ETC/platform-ca.pem" ]; then
  info "$ETC/platform-ca.pem exists — left alone."
else
  info "NO PLATFORM CA INSTALLED."
  info "  The first enrolment call happens before any CA has been pinned, so it"
  info "  can only be verified against one installed out of band. Without it the"
  info "  agent will refuse to enrol over https rather than trusting whatever"
  info "  answers. Re-run with: sudo $0 --ca /path/to/ca.crt"
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
say "systemd unit"
install -m 0644 "$SRC/deploy/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null
info "installed and enabled $UNIT (not started — see below)."

# --- done ------------------------------------------------------------------
cat <<EOF

$(printf '\033[1m==> Installed. Three things left, in order:\033[0m')

  1. Edit the addresses and check the CA fingerprint:
       sudo nano $ETC/gsu.env

  2. Check everything that must be true before it can work:
       sudo -u $SERVICE_USER $PREFIX/.venv/bin/python -m gsu preflight --probe
     Run this from $PREFIX with the env file loaded — DEPLOYMENT.md §5 has the
     exact line. Every FAIL is something that will not work; fix them first.

  3. Start it, then enrol it with a code from an admin:
       sudo systemctl start $UNIT
       journalctl -u $UNIT -f

     The setup page is on 127.0.0.1:8088 by default, so from your laptop:
       ssh -L 8088:127.0.0.1:8088 <this-box>
     then open http://127.0.0.1:8088 and type the code.

  Nothing is published until it is enrolled, and nothing is published over an
  unverified connection at all. Both of those are visible on the setup page.

EOF
