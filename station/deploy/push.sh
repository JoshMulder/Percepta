#!/usr/bin/env bash
#
# Push the working tree's station agent to a bench Pi and restart it.
#
#   station/deploy/push.sh                   (the Pi 5)
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
# One box:
#
#   192.168.2.133  PerceptaGSU  Pi 5   docker compose, RTSP camera
#
# The Pi 2B (192.168.2.132, systemd unit, host venv, CSI camera) was dropped
# from the bench, and the systemd path with it: it existed because a CSI
# camera cannot stream from the container image, and CSI is no longer a
# supported camera. One box, one path.
#
# WHAT TRAVELS, AND WHAT DOES NOT
#
# gsu/, plus deploy/Dockerfile and the compose files — those are build input:
# `docker compose build` reads them off the Pi, so a dependency added to the
# Dockerfile here reaches nothing until they go too. That is not a
# hypothetical. librtlsdr and numpy were installed on the host when the first
# SDR was brought up, which made the radio work from a shell there and not at
# all from the containerised agent, and no amount of pushing gsu/ was ever
# going to fix it.
#
# The udev rules stay put. That is system state install.sh owns, and copying
# it in behind the installer is how a box reaches a configuration no installer
# would produce.
# ---------------------------------------------------------------------------

set -euo pipefail

PI5=192.168.2.133
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

# The Pi 5's build inputs, staged separately so the gsu/ sync stays a clean
# mirror of one directory.
stage_build() {   # $1 = host
  rsync -az -e "ssh ${SSHOPT[*]}" \
    deploy/Dockerfile deploy/docker-compose.yml deploy/docker-compose.lan.yml \
    "pi@$1:/tmp/gsu-build/"
}

# --delete, so a file deleted in the repo is deleted on the box. Without it a
# module that was removed keeps being importable there and the Pi runs code
# that no longer exists anywhere else.
push_pi5() {
  stage "$PI5"
  ssh -n "${SSHOPT[@]}" "pi@$PI5" 'mkdir -p /tmp/gsu-build'
  stage_build "$PI5"
  info "installing on the Pi 5 (docker)"
  ssh -n "${SSHOPT[@]}" "pi@$PI5" '
    sudo rsync -a --delete --exclude __pycache__ /tmp/gsu-new/ \
      /opt/percepta/station/gsu/ && rm -rf /tmp/gsu-new
    sudo rsync -a /tmp/gsu-build/ /opt/percepta/station/deploy/ && rm -rf /tmp/gsu-build
    cd /opt/percepta/station/deploy
    sudo docker compose build 2>&1 | tail -1
    sudo docker compose up -d 2>&1 | tail -1'
}

# Answering on :8088 is the only check worth making from here. "The service is
# active" is what a container says while publishing nothing.
#
# Given up to 40s, because a fresh container takes a few seconds to bind and
# asking once immediately after `up -d` reports a failure that is only the
# question being early — which is worse than no check at all, since it teaches
# you to ignore it.
verify() {   # $1 = host, $2 = label
  local code
  for _ in $(seq 20); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://$1:8088/" || true)
    case "$code" in
      200|401|403) info "$2 console answering ($code)"; return ;;
    esac
    sleep 2
  done
  info "$2 console NOT answering after 40s (${code:-no response})"
}

case "${1:-all}" in
  pi5|all) run_tests; push_pi5; verify "$PI5" "Pi 5" ;;
  *)       die "usage: push.sh [pi5]" ;;
esac
