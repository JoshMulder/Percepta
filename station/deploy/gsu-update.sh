#!/usr/bin/env bash
#
# The station updater. Re-homes DECISIONS.md item 39's mechanism onto Docker
# Compose and adds what item 48 decided: it pulls a SIGNED image from a private
# registry and verifies the signature before running it.
#
# WHERE THIS RUNS (item 48, and the choice confirmed at build time)
# ----------------------------------------------------------------
# In its own small container — the `updater` service in docker-compose.yml — with
# the docker socket, the compose files and the handoff directory mounted. NOT in
# the agent container (which has no socket, drops every capability and is
# read-only, so it cannot replace itself — and must not, or the signature check
# and the gate below would be decorative). A separate updater keeps the host
# Docker-only: nothing is installed on the box, and the updater ships as an image
# like everything else. It recreates the AGENT service, never itself.
#
# WHAT IT DOES, IN ORDER
# ----------------------
#   1. Read the target the agent recorded (image, sha256 digest, tag).
#   2. Skip if already on that digest, or if it was rejected before (unless
#      --force): a bad image must not be re-pulled every cycle.
#   3. Log in to the registry, pull the pinned digest. `docker pull` is atomic at
#      the image level, so a dropped link cannot leave a half-built image.
#   4. VERIFY THE SIGNATURE (cosign, against the pinned public key from the
#      handoff) before running it. The pin proves the bytes did not change; the
#      signature proves we signed them. A failed verification is recorded and NOT
#      deployed.
#   5. Recreate the agent service on the new ref (docker compose up -d gsu).
#   6. Gate on it PUBLISHING within the window — up, uplink up, and its published
#      counter rising. Read over the agent's own loopback via `docker exec`, the
#      auth-exempt path the update gate is allowed (console setup_access). A
#      container that starts and publishes nothing is the failure a plain
#      "did it start?" check misses.
#   7. On any gate failure, roll back to the previous ref (already on disk, no
#      network needed) and gate that too, so "the rollback worked" is a fact. If
#      the OLD image also fails to gate, say so specifically — not an update
#      fault, and sending someone after the update wastes the trip.
#
# NOT VERIFIED. No Docker/cosign here to run it against. The branching follows
# item 39's (stub-tested to 21 scenarios, and even then never drove a real
# container); the cosign flags, the Compose recreate, the docker-exec gate and
# the registry login are new. Run `--check` first — it is side-effect-free and
# reports both whether this box *can* update (docker, cosign, the compose mount)
# and exactly what it *would* do with the current request — then `--status`,
# then once by hand, before --watch is trusted. Lines marked VERIFY are the ones
# to watch.
set -euo pipefail

# --- configuration (from the updater service's environment) ---------------
COMPOSE_DIR="${GSU_COMPOSE_DIR:-/workspace}"          # holds docker-compose.yml + .env
ENV_FILE="${GSU_ENV_FILE:-${COMPOSE_DIR}/.env}"
HANDOFF="${GSU_UPDATE_HANDOFF:-/handoff}"             # shared with the agent
STATE="${GSU_UPDATE_STATE:-/var/lib/updater}"        # updater-only: rejects, previous ref
IMAGE="${GSU_IMAGE:-ghcr.io/joshmulder/percepta-gsu}"
AGENT_CONTAINER="${GSU_AGENT_CONTAINER:-percepta-gsu}"
AGENT_SERVICE="${GSU_AGENT_SERVICE:-gsu}"
GATE_SECONDS="${GSU_UPDATE_GATE_S:-180}"
POLL_SECONDS="${GSU_UPDATE_POLL_S:-30}"
REGISTRY="${IMAGE%%/*}"

REQUEST="${HANDOFF}/update-request.json"
STATUS="${HANDOFF}/update-status.json"
# Written into the handoff by the agent at enrolment and every renewal: the
# cosign public key(s) to verify against (one or more during a rotation overlap),
# and the station's own credential to pull with. Both refresh with the station's
# identity, so nothing update-specific is stored on the box. See
# server/docs/07-remote-update-distribution.md.
SIGNING_KEYS="${HANDOFF}/signing-keys"
REGISTRY_CRED="${HANDOFF}/registry-credential.json"
REJECTS="${STATE}/rejected-digests"
PREVIOUS="${STATE}/previous-ref"

log() { printf '%s gsu-update: %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "ERROR: $*"; return 1; }
mkdir -p "${STATE}"

# The marker is written by us (update.py), one flat JSON object, so a targeted
# sed reads a field without dragging a JSON parser into the updater image.
json_field() { sed -n "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"\\([^\"]*\\)\".*/\\1/p" "$1" | head -1; }

running_ref() { docker inspect --format '{{.Config.Image}}' "${AGENT_CONTAINER}" 2>/dev/null || true; }

# Read the agent's /status.json over its OWN loopback (auth-exempt for the update
# gate) by exec-ing curl-less Python that already ships in the agent image.
read_status() {
    docker exec "${AGENT_CONTAINER}" python3 -c \
"import urllib.request,sys;sys.stdout.write(urllib.request.urlopen('http://127.0.0.1:8088/status.json',timeout=5).read().decode())" \
        2>/dev/null || true
}

set_env_ref() {  # rewrite GSU_IMAGE_REF in .env atomically
    local ref="$1" tmp; tmp="$(mktemp)"
    if [ -f "${ENV_FILE}" ] && grep -q '^GSU_IMAGE_REF=' "${ENV_FILE}"; then
        sed "s#^GSU_IMAGE_REF=.*#GSU_IMAGE_REF=${ref}#" "${ENV_FILE}" >"${tmp}"
    else
        { [ -f "${ENV_FILE}" ] && cat "${ENV_FILE}"; echo "GSU_IMAGE_REF=${ref}"; } >"${tmp}"
    fi
    cat "${tmp}" >"${ENV_FILE}"; rm -f "${tmp}"   # in place: .env is a bind mount
}

write_status() {  # the agent reports last_result/last_version in telemetry
    mkdir -p "${HANDOFF}"
    printf '{"last_result":"%s","last_version":"%s","at":"%s"}\n' \
        "$1" "$2" "$(date -u +%FT%TZ)" >"${STATUS}.tmp" && mv "${STATUS}.tmp" "${STATUS}"
}

recreate() { ( cd "${COMPOSE_DIR}" && docker compose up -d --no-build "${AGENT_SERVICE}" ); } # VERIFY

# Verify the pulled image against the pinned cosign key(s). More than one can be
# present during a rotation overlap, so any that verifies is enough; none
# present, or none that verifies, is a refusal. `--key`, never keyless: nothing
# here reaches a public transparency log.
verify_signature() {
    local image="$1" key
    local keys=("${SIGNING_KEYS}"/*.pub)
    if [ ! -e "${keys[0]}" ]; then
        log "no cosign keys in the handoff; cannot verify (enrolment/renewal writes them)."
        return 1
    fi
    for key in "${keys[@]}"; do
        cosign verify --key "${key}" "${image}" >/dev/null 2>&1 && return 0
    done
    return 1
}

# The publish-gate: up, uplink up, and the published counter rising across two
# reads inside the window — the one condition a "did it start?" check cannot fake.
gate() {
    local deadline=$((SECONDS + GATE_SECONDS)) first="" now pub
    while [ "${SECONDS}" -lt "${deadline}" ]; do
        now="$(read_status)"
        if printf '%s' "${now}" | grep -q '"link"[[:space:]]*:[[:space:]]*true'; then
            pub="$(printf '%s' "${now}" | sed -n 's/.*"published"[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' | head -1)"
            if [ -z "${first}" ]; then first="${pub:-0}"
            elif [ -n "${pub}" ] && [ "${pub}" -gt "${first}" ]; then return 0; fi
        fi
        sleep 10
    done
    return 1
}

reconcile() {
    local force="${1:-}"
    [ -f "${REQUEST}" ] || { log "no update requested; nothing to do."; return 0; }
    local digest tag req_image target label current
    digest="$(json_field "${REQUEST}" digest)"
    tag="$(json_field "${REQUEST}" tag)"
    req_image="$(json_field "${REQUEST}" image)"
    [ -n "${digest}" ] || { log "request has no digest; ignoring."; return 0; }
    [ "${req_image:-${IMAGE}}" = "${IMAGE}" ] || log "WARNING: request image ${req_image} != configured ${IMAGE}; using configured."
    target="${IMAGE}@${digest}"
    label="${tag:-${digest:0:16}}"

    current="$(running_ref)"
    if [ "${current}" = "${target}" ] && [ -z "${force}" ]; then
        [ -n "${CHECK:-}" ] && log "already on ${label}; nothing to do."
        return 0
    fi
    if [ -z "${force}" ] && [ -f "${REJECTS}" ] && grep -qxF "${digest}" "${REJECTS}"; then
        log "${label} was rejected before; skipping (--force overrides)."; return 0
    fi

    # --check stops here, before the first side effect (the PREVIOUS write): it
    # has read the request and made every decision, and now says what it would
    # do instead of doing it. That is how the pull/verify/recreate/rollback
    # chain gets exercised on a real box without touching the running container.
    if [ -n "${CHECK:-}" ]; then
        log "check: would update ${current:-<none>} -> ${label}"
        log "  pull     ${target}"
        log "  verify   against the pinned cosign key(s) in ${SIGNING_KEYS}"
        log "  recreate ${AGENT_SERVICE}, then gate on it publishing within ${GATE_SECONDS}s"
        log "  rollback to ${current:-<none>} and re-gate if that gate fails"
        log "check: nothing was changed."
        return 0
    fi

    log "updating to ${label} (${target})"
    echo "${current}" >"${PREVIOUS}"

    # Log in with the station's own credential from the handoff. The platform's
    # registry token endpoint accepts the same bearer secret the broker does, so
    # the pull is authorised as this station and nothing else — no update-specific
    # registry secret exists on the box.
    if [ -f "${REGISTRY_CRED}" ]; then
        cred_user="$(json_field "${REGISTRY_CRED}" username)"
        cred_secret="$(json_field "${REGISTRY_CRED}" secret)"
        printf '%s' "${cred_secret}" | docker login "${REGISTRY}" \
            -u "${cred_user:-station}" --password-stdin >/dev/null 2>&1 \
            || log "WARNING: docker login failed; relying on any existing credentials."
    else
        log "WARNING: no registry credential in the handoff yet (enrolment/renewal writes it); the pull may be refused."
    fi
    if ! docker pull "${target}"; then
        log "pull failed; leaving the running container untouched."; return 0
    fi

    if ! verify_signature "${target}"; then   # VERIFY: --key against the pinned keys
        log "SIGNATURE VERIFICATION FAILED for ${label}. Refusing to run it."
        echo "${digest}" >>"${REJECTS}"; write_status "signature_rejected" "${label}"; return 1
    fi
    log "signature verified."

    set_env_ref "${target}"; recreate
    if gate; then
        log "updated to ${label} and publishing."
        write_status "updated" "${label}"; rm -f "${REQUEST}"; return 0
    fi

    log "new image did not come up publishing within ${GATE_SECONDS}s; rolling back."
    echo "${digest}" >>"${REJECTS}"
    local prev; prev="$(cat "${PREVIOUS}" 2>/dev/null || true)"
    [ -n "${prev}" ] || { write_status "rollback_impossible" "${label}"; die "no previous image to roll back to; the site needs attention."; return 2; }
    set_env_ref "${prev}"; recreate
    if gate; then
        log "rolled back to ${prev} and publishing."
        write_status "rolled_back" "${label}"; rm -f "${REQUEST}"; return 1
    fi
    write_status "rollback_failed" "${label}"
    die "the ROLLBACK also failed to publish — not an update fault; the site needs attention."; return 2
}

# A side-effect-free readiness check: can this box actually carry out an update?
# The council's worry about this path is that it has never driven a real
# container; this lets an operator confirm the tools and mounts are in place
# before the first real update, rather than discovering a gap mid-rollback.
# Returns non-zero if anything an update needs is missing.
preflight() {
    local ok=0
    if command -v docker >/dev/null 2>&1; then log "  docker: present"
    else log "  docker: MISSING"; ok=1; fi
    if docker info >/dev/null 2>&1; then log "  docker daemon: reachable"
    else log "  docker daemon: NOT reachable (is the socket mounted?)"; ok=1; fi
    if command -v cosign >/dev/null 2>&1; then log "  cosign: present"
    else log "  cosign: MISSING - the signature could not be verified, so no update would run"; ok=1; fi
    if [ -f "${COMPOSE_DIR}/docker-compose.yml" ] || [ -f "${COMPOSE_DIR}/docker-compose.yaml" ]; then
        log "  compose files: ${COMPOSE_DIR}"
    else
        log "  compose files: none under ${COMPOSE_DIR} - recreate would fail"; ok=1
    fi
    if [ -f "${ENV_FILE}" ]; then log "  env file: ${ENV_FILE}"
    else log "  env file: ${ENV_FILE} absent (created on first update)"; fi
    if docker inspect "${AGENT_CONTAINER}" >/dev/null 2>&1; then
        log "  agent container ${AGENT_CONTAINER}: present"
    else
        log "  agent container ${AGENT_CONTAINER}: not created yet (the publish-gate reads it, so an update needs it running)"
    fi
    return "${ok}"
}

case "${1:-once}" in
    --status)
        log "image ${IMAGE}"; log "running: $(running_ref)"
        log "request: $( [ -f "${REQUEST}" ] && cat "${REQUEST}" || echo none )"
        log "last status: $( [ -f "${STATUS}" ] && cat "${STATUS}" || echo none )"
        ;;
    --check)
        log "preflight for ${IMAGE}:"
        preflight || log "preflight found problems above; an update would likely fail on this box."
        log "plan:"
        CHECK=1 reconcile ""
        ;;
    --watch)
        log "watching ${REQUEST} every ${POLL_SECONDS}s."
        while true; do reconcile "" || true; sleep "${POLL_SECONDS}"; done
        ;;
    # `|| exit` puts reconcile in a context where set -e is suppressed inside it,
    # exactly as `--watch` does with `|| true`. Without it a hand-run has set -e
    # live inside reconcile and aborts on the first non-zero — a failed recreate,
    # or even the no-op "already on this digest" path — skipping the gate and
    # rollback that make it safe, so `once` would not behave like the `--watch`
    # it is meant to rehearse. The final exit code still reaches the operator.
    --force) reconcile 1 || exit "$?" ;;
    *) reconcile "" || exit "$?" ;;
esac
