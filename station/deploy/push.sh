#!/usr/bin/env bash
#
# Push the working tree's station agent to a bench Pi and restart it.
#
#   station/deploy/push.sh [pi5|2b|all]      (default: all)
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS
#
# gsu-update.sh is the field path: a station pulls an image from a registry on
# a six-hour timer and rolls back if the new one cannot prove itself. On the
# bench there is no registry — GSU_UPDATE_REF is unset — so that path does
# nothing at all, and every change had to be hand-rsynced from memory.
#
# The failure that produced this script is not a broken deploy. It is a silent
# one: work lands on main, nobody runs the rsync, and the box quietly stays
# behind. What that looks like from the console is a setting that "was never
# added" — which is indistinguishable from the work not having been done.
#
# The two boxes are deliberately different shapes, so there is one recipe each
# rather than one recipe pretending they match:
#
#   192.168.2.132  Station1     Pi 2B  systemd unit, host venv, CSI camera
#   192.168.2.133  PerceptaGSU  Pi 5   docker compose, RTSP camera
#
# Only gsu/ is pushed. Anything under deploy/ — the unit file, the Dockerfile,
# the udev rules — changes how the box is *built*, not what it runs, and
# copying those silently is how a box ends up in a state no installer would
# ever produce. Change one of those and run install.sh.
# ---------------------------------------------------------------------------

set -euo pipefail

PI5=192.168.2.133
PI2B=192.168.2.132
KEY=${GSU_DEPLOY_KEY:-$HOME/.ssh/percepta_deploy}
SSHOPT=(-o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=8
        -i "$KEY" -o IdentitiesOnly=yes)

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # …/station
cd "$HERE"

info() { printf '  %s\n' "$*"; }
die()  { printf 'push: %s\n' "$*" >&2; exit 1; }

[ -f "$KEY" ] || die "no deploy key at $KEY (set GSU_DEPLOY_KEY)"
[ -d gsu ]    || die "no gsu/ under $HERE"

# The tests are the gate, not a suggestion. Pushing a station agent that does
# not import is a drive to site, and these run in under a second.
run_tests() {
  local python=.venv/bin/python
  [ -x "$python" ] || python=python3
  info "tests…"
  "$python" -m unittest discover -s tests -q >/tmp/gsu-push-tests.log 2>&1 \
    || { tail -20 /tmp/gsu-push-tests.log; die "tests failed — nothing pushed"; }
}

stage() {   # $1 = host
  info "staging on $1"
  rsync -az --exclude __pycache__ --exclude '*.pyc' --exclude .venv \
    -e "ssh ${SSHOPT[*]}" gsu/ "pi@$1:/tmp/gsu-new/"
}

# --delete, so a file deleted in the repo is deleted on the box. Without it a
# module that was removed keeps being importable there and the Pi runs code
# that no longer exists anywhere else.
push_pi5() {
  stage "$PI5"
  info "installing on the Pi 5 (docker)"
  ssh -n "${SSHOPT[@]}" "pi@$PI5" '
    sudo rsync -a --delete --exclude __pycache__ /tmp/gsu-new/ \
      /opt/percepta/station/gsu/ && rm -rf /tmp/gsu-new
    cd /opt/percepta/station/deploy
    sudo docker compose build 2>&1 | tail -1
    sudo docker compose up -d 2>&1 | tail -1'
}

# --chown, because the 2B runs the agent as the unqualified `gsu` user rather
# than in a container, and files arriving owned by root are files the service
# cannot read.
push_2b() {
  stage "$PI2B"
  info "installing on the 2B (systemd)"
  ssh -n "${SSHOPT[@]}" "pi@$PI2B" '
    sudo rsync -a --delete --exclude __pycache__ --chown=gsu:gsu \
      /tmp/gsu-new/ /opt/percepta/station/gsu/ && rm -rf /tmp/gsu-new
    sudo systemctl restart gsu.service
    sleep 8
    printf "  gsu.service: %s\n" "$(systemctl is-active gsu.service)"'
}

# Answering on :8088 is the only check worth making from here. "The service is
# active" is what a container says while publishing nothing.
verify() {   # $1 = host, $2 = label
  local code
  code=$(curl -s -o /dev/null -m 6 -w '%{http_code}' "http://$1:8088/" || true)
  case "$code" in
    200|401|403) info "$2 console answering ($code)" ;;
    *)           info "$2 console NOT answering (${code:-no response})" ;;
  esac
}

case "${1:-all}" in
  pi5) run_tests; push_pi5; verify "$PI5"  "Pi 5" ;;
  2b)  run_tests; push_2b;  verify "$PI2B" "2B"   ;;
  all) run_tests; push_pi5; push_2b
       verify "$PI5" "Pi 5"; verify "$PI2B" "2B" ;;
  *)   die "usage: push.sh [pi5|2b|all]" ;;
esac
