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
# TWO THINGS ARE BUILT FROM SOURCE, in a throwaway builder stage:
#
#   * The rtl-sdr-blog fork of librtlsdr. Debian's mainline librtlsdr0 will not
#     initialise the RTL-SDR Blog V4's R828D tuner — the V4 needs this fork — so
#     the image carries it instead of the apt package. A V3 works on either.
#
#   * whisper.cpp and a ladder of models, for on-box airband transcription. Built
#     static so one self-contained `whisper-cli` binary drops into the runtime,
#     and the models are baked in. WHISPER_MODELS is the ladder (small.en +
#     base.en by default, ~600 MB together); the station benchmarks them at
#     start-up and keeps the largest this board can keep up with, stepping down to
#     a smaller one — or off — on a slow box (see radio/transcribe.py). Transcription
#     is still OFF by default — the setup page's radio tab is the switch, and it
#     only lights up because these files are present.
#
# The versions are ARGs so a build can pin them; the defaults track upstream's
# default branch. Pinning to a release for a reproducible image is the follow-up
# the base-image digest pin below already commits this file to.
#
# amd64 and arm64 only. 3.11 is the floor, not a preference: the agent uses
# datetime.UTC. **The digest pin stays** — it is the multi-arch index, so it
# selects the right manifest for whichever of the two architectures is building.

ARG BASE=python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba
ARG WHISPER_REF=master
ARG RTLSDR_REF=master
# The ladder baked into the image, and the preferred ceiling the env points at.
# The station picks the largest of the ladder it can run; WHISPER_MODEL caps it.
ARG WHISPER_MODELS="small.en base.en"
ARG WHISPER_MODEL=small.en

# The station software version, stamped into the image so the box reports what it
# is running and the remote updater can compare against a target. CI passes the
# release tag; a local build leaves it "dev".
ARG GSU_VERSION=dev

# =========================================================================
# Builder: the rtl-sdr-blog driver, whisper.cpp, and the model. Discarded —
# none of its build tools reach the runtime image.
# =========================================================================
FROM ${BASE} AS builder

ARG WHISPER_REF
ARG RTLSDR_REF

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ca-certificates curl cmake build-essential pkg-config \
      libusb-1.0-0-dev \
 && rm -rf /var/lib/apt/lists/*

# The rtl-sdr-blog fork — installs librtlsdr.so.* into /usr/local/lib. No udev
# rules (device access is via docker-compose group_add, not this image), and
# detach-kernel-driver on so a host dvb module cannot hold the dongle.
RUN git clone --depth 1 --branch "${RTLSDR_REF}" \
      https://github.com/rtlsdrblog/rtl-sdr-blog.git /tmp/rtlsdr \
 && cmake -S /tmp/rtlsdr -B /tmp/rtlsdr/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DINSTALL_UDEV_RULES=OFF \
      -DDETACH_KERNEL_DRIVER=ON \
 && cmake --build /tmp/rtlsdr/build --target install -j "$(nproc)"

# whisper.cpp, built static (BUILD_SHARED_LIBS=OFF) so the CLI carries its own
# whisper/ggml and the runtime needs only libgomp and the C/C++ runtimes.
#
# **Copy the real CLI, not a deprecation shim.** Upstream renames this binary
# from time to time — `main`, then `whisper-cli`, and a master build was seen
# producing `whisper-whisper-cli` with `whisper-cli` left behind as a stub that
# prints "use whisper-whisper-cli" and exits 1. `-name whisper-cli | head -1`
# picked that stub, and transcription came up installed-but-silent: the self
# test caught an exit 1 with the reason on stdout. Rather than track the name,
# take the LARGEST `whisper*cli` the build produced — the real one is megabytes
# of static whisper/ggml, the stub is a few kilobytes of printf. It lands at
# whisper-cli regardless, which is what GSU_WHISPER_BIN points at.
RUN git clone --depth 1 --branch "${WHISPER_REF}" \
      https://github.com/ggerganov/whisper.cpp.git /tmp/whisper \
 && cmake -S /tmp/whisper -B /tmp/whisper/build \
      -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF \
 && cmake --build /tmp/whisper/build --config Release -j "$(nproc)" \
 && whisper_bin="$(find /tmp/whisper/build -type f -name 'whisper*cli' \
      -printf '%s\t%p\n' | sort -rn | head -1 | cut -f2)" \
 && test -n "${whisper_bin}" && test "$(stat -c%s "${whisper_bin}")" -gt 1000000 \
      || { echo "no real whisper-cli built (only a deprecation stub?)"; exit 1; } \
 && cp "${whisper_bin}" /usr/local/bin/whisper-cli

# The model ladder, pinned by name; the files are content-stable at these URLs.
# WHISPER_MODELS is declared here, below the whisper build, on purpose: changing
# the ladder then re-runs only this download and never rebuilds whisper.cpp above.
ARG WHISPER_MODELS
RUN mkdir -p /opt/models \
 && for m in ${WHISPER_MODELS}; do \
      curl -fSL -o "/opt/models/ggml-$m.bin" \
        "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$m.bin" \
      || { echo "failed to fetch ggml-$m.bin"; exit 1; }; \
    done

# =========================================================================
# Runtime: the slim base, plus only the shared libraries the built artefacts
# need at run time.
# =========================================================================
FROM ${BASE}

ARG WHISPER_MODEL
ARG GSU_VERSION

# PYTHONDONTWRITEBYTECODE: no .pyc files. The container's writable layer sits on
# whatever storage the box has, and at a remote site that is the most likely
# hardware failure. Every write avoided is worth having.
#
# GSU_WHISPER_* point at the artefacts copied below; GSU_RADIO_TRANSCRIBE is
# deliberately NOT set here — the tools being present is not the same as the
# feature being on, and the setup page is the switch.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GSU_HOME=/var/lib/percepta-gsu \
    GSU_WHISPER_BIN=whisper-cli \
    GSU_WHISPER_MODEL=/opt/percepta/models/ggml-${WHISPER_MODEL}.bin \
    GSU_VERSION=${GSU_VERSION}

# ffmpeg is the whole of the network-camera path (one frame per snapshot, a
# copy-codec remux for the live stream). libopus0 is the airband codec, bound
# through ctypes. libusb-1.0-0 is what the rtl-sdr-blog driver links against at
# run time, and libgomp1 is whisper-cli's OpenMP. **No librtlsdr0** — the
# rtl-sdr-blog driver copied from the builder replaces the apt package, because
# the apt package cannot drive a V4.
#
# Placed above the code COPY so a code update still ships one small layer.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libopus0 \
      libusb-1.0-0 \
      libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# The driver, the transcriber and the model ladder, out of the builder.
# `ldconfig` so the loader finds librtlsdr.so.0 in /usr/local/lib, which is where
# the station's ctypes `CDLL("librtlsdr.so.0")` looks it up by soname.
COPY --from=builder /usr/local/lib/librtlsdr.so* /usr/local/lib/
COPY --from=builder /usr/local/bin/whisper-cli /usr/local/bin/whisper-cli
COPY --from=builder /opt/models/ /opt/percepta/models/
RUN ldconfig

# A fixed uid/gid so a bind-mounted state directory has predictable ownership on
# the host. Device access does NOT come from this user's own groups — the host's
# dialout/plugdev gids differ per distribution — it comes from `group_add` in
# docker-compose.yml, which is where the numbers can be checked against the box.
RUN groupadd --system --gid 10001 gsu \
 && useradd --system --uid 10001 --gid gsu --no-create-home --shell /usr/sbin/nologin gsu \
 && mkdir -p /var/lib/percepta-gsu /var/lib/percepta-gsu-update \
 && chown gsu:gsu /var/lib/percepta-gsu /var/lib/percepta-gsu-update \
 && chmod 0700 /var/lib/percepta-gsu \
 && chmod 0770 /var/lib/percepta-gsu-update

WORKDIR /opt/percepta/station

# Requirements first, so a code change does not re-resolve them.
#
# numpy is the radio's other half — the receiver refuses to start without it,
# because 240 000 samples a second is not something CPython can filter in real
# time. From pip, NOT apt's python3-numpy: this base's interpreter lives in
# /usr/local and never reads the distribution's dist-packages.
#
# `--only-binary=:all:` is load-bearing, not tidiness. This runtime stage has no
# C toolchain — cc/gcc live only in the builder — so numpy has to arrive as a
# prebuilt wheel. Without the flag, pip downloads an *sdist* for any version that
# lacks a wheel for this box and tries to compile it, which fails with "Unknown
# compiler(s)" — exactly what a fresh numpy release with no amd64/arm64 cp311
# wheel yet did here. The flag makes pip choose the newest version that *does*
# have a matching wheel, and on a genuinely unsupported arch it fails fast with
# "no matching distribution" rather than a five-minute doomed build. amd64 and
# arm64 both have wheels; nothing else is a target.
COPY requirements.txt ./
RUN pip install --no-cache-dir --only-binary=:all: "numpy>=1.24"

# Docs before code, so the last layer in the image is the one that changes most
# often: a code-only update then ships one small layer and nothing else. Demo
# airband broadcasts (`<freq_hz>.wav`) sit here too, changing far less often than
# the package.
COPY assets/ ./assets/
COPY README.md DECISIONS.md DEPLOYMENT.md HARDWARE.md CONTRACT-QUESTIONS.md ./
COPY gsu/ ./gsu/

# Root-owned and not writable by the agent: a compromised agent should not be
# able to rewrite the thing that restarts it.
RUN chown -R root:root /opt/percepta/station && chmod -R go-w /opt/percepta/station

USER gsu

# No HEALTHCHECK, deliberately. The obvious one is `gsu preflight`, which fails
# when a sensor is absent — and a station with a missing weather head should keep
# publishing everything else, not be marked unhealthy. Liveness here is "the
# process is running", which `restart: unless-stopped` covers; health is
# reported in telemetry, not inferred by the supervisor.

# `gsu` as the entrypoint so the subcommands work as they do everywhere else:
#   docker compose run --rm gsu preflight --probe
#   docker compose run --rm gsu enrol --token XXXX-XXXX-XXXX
ENTRYPOINT ["python", "-m", "gsu"]
CMD ["run"]
