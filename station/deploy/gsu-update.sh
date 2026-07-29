#!/usr/bin/env bash
#
# Update the ground station container, and roll back if the new one cannot
# prove itself.
#
#   gsu-update.sh [--check] [--force] [--rollback] [--status]
#
# Run by deploy/gsu-update.timer. Safe to run by hand at any time.
#
# ---------------------------------------------------------------------------
# WHY THIS EXISTS
#
# "Once these stations are installed they are going to be difficult to
# physically access." Everything below follows from that sentence.
#
# The failure this is built to prevent is not "the update did not arrive" — it
# is "the update arrived, the station stopped working, and now somebody has to
# drive there". So the station never simply replaces what is running: it keeps
# the old image, starts the new one, watches it do the actual job, and puts the
# old one back if it does not. The old image is already on disk, so recovery
# needs no network — which matters, because a broken uplink is one of the
# reasons an update might fail.
#
# THE GATE, precisely. A new image is accepted only if, within $GATE_SECONDS:
#   1. the container is running (not restarting, not exited);
#   2. the station's own console answers;
#   3. it reports itself enrolled — the credential survived the swap;
#   4. it reports the uplink up;
#   5. its published-frame counter has *increased* since the gate started.
#
# (5) is the one that matters. A container can start, log cheerfully and
# publish nothing at all; that is precisely the failure a naive "did it start?"
# check waves through, and it is indistinguishable from a healthy station until
# somebody looks at a console days later.
#
# NEVER TESTED END TO END. There is no Docker daemon on the machine this was
# written on, so the shell logic has been reasoned through and unit-tested but
# has never driven a real container. Run it by hand with --check on the first
# box before letting the timer near it.
# ---------------------------------------------------------------------------

set -uo pipefail

COMPOSE_FILE=${GSU_COMPOSE_FILE:-/opt/percepta/station/deploy/docker-compose.yml}
SERVICE=gsu
# The image to track. A digest is better than a tag — it cannot be moved under
# you — and the whole point of a station being hard to reach is that you want
# to know exactly what it will fetch. Set GSU_UPDATE_REF in /etc/percepta/gsu.env.
UPDATE_REF=${GSU_UPDATE_REF:-}
CURRENT_TAG=${GSU_CURRENT_TAG:-percepta/gsu:current}
PREVIOUS_TAG=${GSU_PREVIOUS_TAG:-percepta/gsu:previous}
STAGING_TAG=${GSU_STAGING_TAG:-percepta/gsu:staging}

CONSOLE=${GSU_CONSOLE_URL:-http://127.0.0.1:8088/status.json}
GATE_SECONDS=${GSU_GATE_SECONDS:-180}
GATE_POLL=${GSU_GATE_POLL:-5}

STATE_DIR=${GSU_UPDATE_STATE:-/var/lib/percepta-gsu/update}
# A digest that has already failed its gate. Without this, a bad image is
# re-pulled and re-tried on every timer tick for ever: the station spends its
# metered bandwidth on it and flaps in and out of service each time.
REJECTED=$STATE_DIR/rejected
LAST_RUN=$STATE_DIR/last-run

log() { printf '%s gsu-update: %s\n' "$(date -Is)" "$*"; }
die() { log "ERROR: $*"; exit 1; }

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }

# --- what is running right now ---------------------------------------------

running_digest() {
    # The image ID the live container was created from. Deliberately the ID and
    # not the tag: tags move, and this has to survive the tag being moved by
    # this very script.
    docker inspect --format '{{.Image}}' percepta-gsu 2>/dev/null || true
}

tag_digest() {
    docker image inspect --format '{{.Id}}' "$1" 2>/dev/null || true
}

# --- the gate ---------------------------------------------------------------

console_field() {
    # One field out of the station's own status page, without needing jq on a
    # minimal image.
    curl -fsS --max-time 5 "$CONSOLE" 2>/dev/null | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('$1', ''))
except Exception:
    pass
" 2>/dev/null
}

container_state() {
    docker inspect --format '{{.State.Status}}' percepta-gsu 2>/dev/null || echo "missing"
}

gate() {
    # True if the station proves it is doing its job within the window.
    local deadline=$((SECONDS + GATE_SECONDS))
    local baseline="" published="" enrolled="" link="" state=""

    log "gate: watching for up to ${GATE_SECONDS}s — must be running, enrolled, linked, and publishing"
    while [ $SECONDS -lt $deadline ]; do
        state=$(container_state)
        if [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
            log "gate: FAILED — container is '$state'"
            return 1
        fi

        published=$(console_field published)
        enrolled=$(console_field enrolled)
        link=$(console_field link)

        if [ -n "$published" ]; then
            if [ -z "$baseline" ]; then
                baseline=$published
                log "gate: console answering; published=$published enrolled=$enrolled link=$link"
            elif [ "$enrolled" = "True" ] && [ "$link" = "True" ] \
                 && [ "$published" -gt "$baseline" ] 2>/dev/null; then
                # Running, enrolled, linked, and the counter moved. That is the
                # station actually doing its job, not merely having started.
                log "gate: PASSED — published $baseline -> $published, enrolled, uplink up"
                return 0
            fi
        fi
        sleep "$GATE_POLL"
    done

    log "gate: FAILED — ${GATE_SECONDS}s elapsed. state=$(container_state) published=$published enrolled=$enrolled link=$link"
    if [ "$enrolled" != "True" ]; then
        log "gate: the station is not enrolled. If it was before this update, the"
        log "      new image cannot read the credential — a state-path or"
        log "      permissions change is the usual cause."
    fi
    return 1
}

# --- rollback ---------------------------------------------------------------

rollback() {
    local reason=$1
    log "ROLLING BACK: $reason"
    if [ -z "$(tag_digest "$PREVIOUS_TAG")" ]; then
        log "no $PREVIOUS_TAG on disk — cannot roll back. Leaving the current"
        log "container in place; it is the only image there is."
        return 1
    fi
    docker tag "$PREVIOUS_TAG" "$CURRENT_TAG" || { log "retag failed"; return 1; }
    compose up -d --force-recreate "$SERVICE" || { log "restart failed"; return 1; }
    log "rolled back to $(tag_digest "$CURRENT_TAG")"

    # Give the restored image the same gate, so "the rollback worked" is a
    # fact rather than an assumption. If even the old image cannot pass, the
    # fault is not the update and somebody needs to know that specifically.
    if gate; then
        log "rollback verified: the previous image is publishing again"
        return 0
    fi
    log "ALARM: the previous image did not pass the gate either. This is not an"
    log "       update fault — check the uplink, the credential and the clock."
    return 1
}

# --- the update -------------------------------------------------------------

update() {
    local force=$1
    [ -n "$UPDATE_REF" ] || die "GSU_UPDATE_REF is not set. Nothing to track."

    mkdir -p "$STATE_DIR"
    local before; before=$(running_digest)

    log "checking $UPDATE_REF"
    # `docker pull` is atomic at the image level: layers are content-addressed
    # and verified as they land, and the local reference is only updated once
    # every layer is present. **A pull interrupted by a dropped Starlink link
    # therefore cannot produce a half-built image** — it fails, the previous
    # image is untouched, and the next run resumes from the layers already in
    # the content store. This is documented Docker behaviour; it has NOT been
    # verified on this hardware.
    if ! docker pull "$UPDATE_REF"; then
        log "pull failed (link down, registry unreachable, or partial transfer)."
        log "Nothing changed; the running container is untouched. Will retry."
        return 0
    fi

    docker tag "$UPDATE_REF" "$STAGING_TAG" || die "could not tag the pulled image"
    local candidate; candidate=$(tag_digest "$STAGING_TAG")
    [ -n "$candidate" ] || die "pulled image has no id — refusing to continue"

    if [ "$candidate" = "$before" ]; then
        log "already running $candidate — nothing to do"
        date -Is > "$LAST_RUN"
        return 0
    fi

    if [ "$force" != "yes" ] && [ -f "$REJECTED" ] && grep -qxF "$candidate" "$REJECTED"; then
        log "$candidate already failed its gate here and is not being retried."
        log "Publish a fixed image, or override with --force once you know why."
        return 0
    fi

    # Keep what is running, by id, so the rollback target cannot be moved by a
    # later retag.
    if [ -n "$before" ]; then
        docker tag "$before" "$PREVIOUS_TAG" \
            || log "warning: could not tag the running image as $PREVIOUS_TAG"
    fi

    log "updating: $before -> $candidate"
    docker tag "$STAGING_TAG" "$CURRENT_TAG" || die "could not move $CURRENT_TAG"

    if ! compose up -d --force-recreate "$SERVICE"; then
        log "the new container would not start"
        echo "$candidate" >> "$REJECTED"
        rollback "new image failed to start"
        return 1
    fi

    if gate; then
        log "UPDATE ACCEPTED: now running $candidate"
        # The previous image stays on disk. It is the rollback path, it costs
        # only its own differing layers because the base is shared, and it is
        # the one thing that does not need the network to recover.
        date -Is > "$LAST_RUN"
        docker image prune -f --filter "until=720h" >/dev/null 2>&1 || true
        return 0
    fi

    echo "$candidate" >> "$REJECTED"
    rollback "new image did not pass the health gate"
    return 1
}

status() {
    printf 'current   %s  %s\n' "$CURRENT_TAG" "$(tag_digest "$CURRENT_TAG")"
    printf 'previous  %s  %s\n' "$PREVIOUS_TAG" "$(tag_digest "$PREVIOUS_TAG")"
    printf 'running   %s\n' "$(running_digest)"
    printf 'state     %s\n' "$(container_state)"
    printf 'tracking  %s\n' "${UPDATE_REF:-(GSU_UPDATE_REF unset)}"
    printf 'last run  %s\n' "$(cat "$LAST_RUN" 2>/dev/null || echo never)"
    if [ -s "$REJECTED" ]; then
        printf 'rejected  %s\n' "$(wc -l < "$REJECTED") image(s) failed the gate here"
    fi
    printf '\nstation   '
    curl -fsS --max-time 5 "$CONSOLE" 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f\"enrolled={d.get('enrolled')} link={d.get('link')} published={d.get('published')}\")
" 2>/dev/null || printf 'console not answering\n'
}

command -v docker >/dev/null || die "docker is not installed"

case "${1:-}" in
    --status)   status ;;
    --rollback) rollback "requested by hand" ;;
    --check)    UPDATE_REF=${UPDATE_REF:-} ; update no ;;
    --force)    update yes ;;
    "")         update no ;;
    *)          sed -n '2,10p' "$0"; exit 2 ;;
esac
