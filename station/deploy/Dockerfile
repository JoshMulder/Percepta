# The ground station agent as a container image. **This is the deployment path,
# and now the only one.**
#
#   cd station/deploy && docker compose up -d --build
#
# Build context is station/, not deploy/ — the package is what goes in.
#
# Containers won on the constraint that actually matters here: once these
# stations are installed they are difficult to physically access. An update is
# `git pull` and a restart; a rollback is `git checkout` of a tag already on the
# disk, which needs no download over the link that may be the reason you are
# rolling back.
#
# ---------------------------------------------------------------------------
# WHAT WENT WITH THE PI 2B
#
# This file used to pin the base by its multi-arch *index* digest so that
# `--platform linux/arm/v7` still selected the right manifest, and then spent a
# paragraph explaining that numpy publishes no 32-bit ARM wheels — so the
# recommendation was to run the agent under systemd against the distribution's
# numpy instead. That produced two deployment paths that could fail
# differently, and nobody debugging one knew which they were in.
#
# amd64 and arm64 only now. Every dependency is a wheel and nothing compiles.
#
# **The digest pin stays.** Only `--platform=$TARGETPLATFORM` went with armv7.
# The digest is still the multi-arch *index*, so it selects the right manifest
# for whichever of the two remaining architectures is building — dropping it
# would have traded a reproducible build for nothing at all, which is the kind
# of thing that goes unnoticed when it rides along with an unrelated change.
#
# 3.11 is the floor, not a preference: the agent uses datetime.UTC.
FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

# PYTHONDONTWRITEBYTECODE: no .pyc files. The container's writable layer sits on
# whatever storage the box has, and at a remote site that is the most likely
# hardware failure. Every write avoided is worth having.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GSU_HOME=/var/lib/percepta-gsu

# A fixed uid/gid so a bind-mounted state directory has predictable ownership on
# the host. Device access does NOT come from this user's own groups — the host's
# dialout/plugdev gids differ per distribution — it comes from `group_add` in
# docker-compose.yml, which is where the numbers can be checked against the box.
# ffmpeg, which is the whole of the network-camera path: one frame per
# snapshot and a copy-codec remux for the live stream. Installed here rather
# than left to the camera-specific image because an RTSP camera needs no
# libcamera, no /dev, and no Raspberry Pi archive - it is a URL - so making it
# depend on the heavyweight variant would be an accident of packaging. It is
# ~1 MB of binary against a base already carrying Python; ffmpeg's own
# dependency tail is the cost, measured in the build log rather than guessed.
#
# Placed above the code COPY so a code update still ships one small layer.
#
# librtlsdr0 is the radio slot, and it belongs here for a reason worth
# recording: it was installed on the *host* when the first SDR was brought up,
# which made the radio work from a shell on that box and not at all from the
# agent, because on this hardware the agent is in a container and the host's
# packages are invisible to it. The symptom is a receiver reporting itself
# absent while `rtl_test` on the same machine is perfectly happy, and no
# restart or rebuild touches it, because nothing about it is wrong except
# where the package is.
#
# The library only, not the `rtl-sdr` tools: the station drives the device
# through libusb via ctypes and never shells out to rtl_fm or rtl_tcp.
#
# libopus0 is the airband codec. Contract 2.0 fixes audio as Opus, and the
# station binds this library through ctypes for the same reason as the SDR —
# carrying a pip package to wrap four C functions buys nothing, and the apt
# dependency exists either way.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      librtlsdr0 \
      libopus0 \
 && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 gsu \
 && useradd --system --uid 10001 --gid gsu --no-create-home --shell /usr/sbin/nologin gsu \
 && mkdir -p /var/lib/percepta-gsu \
 && chown gsu:gsu /var/lib/percepta-gsu \
 && chmod 0700 /var/lib/percepta-gsu

WORKDIR /opt/percepta/station

# Requirements first, so a code change does not re-resolve them.
#
# numpy is the radio's other half — the receiver refuses to start without it,
# and says so, because 240 000 samples a second is not something CPython can
# filter in real time. It comes from pip and NOT from apt's python3-numpy: this
# base is python:3.11-slim, whose interpreter lives in /usr/local and does not
# read the distribution's /usr/lib/python3/dist-packages. An apt numpy installs
# perfectly and is then invisible to the only Python in the image, which is the
# same class of mistake as installing it on the host.
#
# On arm64 and amd64 this is a wheel and nothing compiles.
#
# `redis` was installed here and is gone with the Redis transport: contract 2.0
# defines one transport, and the station speaks it over a WebSocket it writes
# out itself. The agent's only runtime dependency is now numpy, and only when a
# receiver is fitted.
COPY requirements.txt ./
RUN pip install --no-cache-dir "numpy>=1.24"

# Docs before code, so that the last layer in the image is the one that changes
# most often. A code-only update then ships **one 91 KB layer** and nothing
# else: the base is unchanged, the pip layer is unchanged, and the docs layer is
# unchanged. On a metered Starlink link that is the difference between an update
# being routine and an update being a decision. Measured, not assumed —
# DECISIONS.md item 40 has the numbers and how they were taken.
# Demo airband broadcasts: a 16-bit WAV named `<freq_hz>.wav` goes on air at
# that frequency. Same convention as the platform's own simulator, and the
# reason a demo station tuned to 127.200 now says something instead of
# producing voice-shaped noise. Above the code COPY because it changes far
# less often than the package does.
COPY assets/ ./assets/

COPY README.md DECISIONS.md DEPLOYMENT.md HARDWARE.md CONTRACT-QUESTIONS.md ./
COPY gsu/ ./gsu/

# Root-owned and not writable by the agent: a compromised agent should not be
# able to rewrite the thing that restarts it.
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
