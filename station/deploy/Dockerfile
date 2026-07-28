# ===========================================================================
#  DO NOT USE THIS FOR AN UNATTENDED STATION.
#
#  A container will not start if a device it maps is missing, so a reboot
#  after a USB adapter fails to enumerate takes down THE WHOLE STATION — not
#  just the affected sensor — and recovery needs somebody on site. The systemd
#  unit beside this file does not have that failure: a missing device is a
#  health condition the station reports and recovers from on its own.
#
#  The owner ruled on this. deploy/gsu.service is the deployment path.
#
#  Kept because it may suit a CO-LOCATED station — in a rack, in a building,
#  with someone who can reach it. DEPLOYMENT.md Appendix B has the reasoning,
#  including the mitigation that exists and why it is not taken; DECISIONS.md
#  item 35 has the full cost comparison.
# ===========================================================================
#
# The ground station agent as a container image.
#
#   docker buildx build --platform linux/arm/v7 -f deploy/Dockerfile -t percepta/gsu:0.1.0 .
#
# Build context is station/, not deploy/ — the package is what goes in.
#
# ---------------------------------------------------------------------------
# Base image: pinned by digest, and the digest is the multi-arch *index* so that
# `--platform linux/arm/v7` still selects the right manifest. Verified against
# the registry rather than assumed: python:3.11-slim-bookworm publishes
# linux/arm/v7 (manifest sha256:d2091b0d…, 39.9 MB compressed across 4 layers),
# which is the architecture a Pi 2B runs.
#
# 3.11 is the floor, not a preference: the agent uses datetime.UTC. Bookworm's
# own Python is 3.11.2, so the container and the systemd path run the same
# interpreter version — worth keeping true, because otherwise the two paths can
# fail differently and nobody knows which one they are debugging.
FROM --platform=$TARGETPLATFORM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

# PYTHONDONTWRITEBYTECODE: no .pyc files. On a Pi the container's writable layer
# is on the SD card, and the card is the most likely hardware failure at a
# remote site. Every write avoided is worth having.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GSU_HOME=/var/lib/percepta-gsu

# A fixed uid/gid so a bind-mounted state directory has predictable ownership on
# the host. Device access does NOT come from this user's own groups — the host's
# dialout/plugdev gids differ per distribution — it comes from `group_add` in
# docker-compose.yml, which is where the numbers can be checked against the box.
RUN groupadd --system --gid 10001 gsu \
 && useradd --system --uid 10001 --gid gsu --no-create-home --shell /usr/sbin/nologin gsu \
 && mkdir -p /var/lib/percepta-gsu \
 && chown gsu:gsu /var/lib/percepta-gsu \
 && chmod 0700 /var/lib/percepta-gsu

WORKDIR /opt/percepta/station

# Requirements first, so a code change does not re-resolve the dependency. One
# runtime dependency, pure Python — nothing compiles, so no build toolchain is
# needed and an ARMv7 build is not meaningfully slower than an x86 one.
COPY requirements.txt ./
RUN pip install --no-cache-dir "redis>=5.0"

COPY gsu/ ./gsu/
COPY README.md DECISIONS.md DEPLOYMENT.md HARDWARE.md CONTRACT-QUESTIONS.md ./

# Root-owned and not writable by the agent, matching the systemd path: a
# compromised agent should not be able to rewrite the thing that restarts it.
RUN chown -R root:root /opt/percepta/station && chmod -R go-w /opt/percepta/station

USER gsu

# No HEALTHCHECK, deliberately. The obvious one is `gsu preflight`, which fails
# when a sensor is absent — and a station with a missing weather head is a
# station that should keep publishing everything else, not one Docker should
# mark unhealthy and an orchestrator should replace. Liveness here is "the
# process is running", which `restart: unless-stopped` already covers, and
# health is a thing this station reports in telemetry rather than a thing the
# supervisor should be inferring.

# `gsu` as the entrypoint so the subcommands work as they do everywhere else:
#   docker compose run --rm gsu preflight --probe
#   docker compose run --rm gsu enrol --token XXXX-XXXX-XXXX
ENTRYPOINT ["python", "-m", "gsu"]
CMD ["run"]
