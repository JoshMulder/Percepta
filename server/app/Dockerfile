# --- console build -----------------------------------------------------------
# Node lives only in this stage. The runtime image has no toolchain, and
# `docker compose up` still needs nothing installed on the host.
FROM node:22-slim AS web

WORKDIR /web
COPY web/package.json web/package-lock.json* ./
# `npm ci` when a lockfile is present (reproducible), `npm install` on the very
# first build before one exists.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY web/ ./
RUN npm run build

# --- runtime -----------------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# libopus, for the station simulator only. Contract 2.0 carries airband audio
# as Opus, and `contract/NOTES.md` designates the simulator the reference
# implementation — so it publishing base64 PCM would be teaching the mistake
# rather than merely making it. Bound through ctypes in
# `backend/scripts/_opus.py`, which is a second binding by design: neither side
# of the contract reads the other's code, and two independent implementations
# of the same four functions is what keeps the simulator honest.
#
# The library, not the tools. Nothing here shells out to opusenc.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libopus0 \
 && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/alembic.ini .
COPY app/backend backend
# Demo airband broadcasts. A 16-bit WAV named <freq_hz>.wav goes on air at that
# frequency - see services/airband_demo.py.
COPY app/assets assets
COPY --from=web /web/dist /app/static

# Run as an unprivileged user (uid 10001) rather than root. This process
# terminates data from ground stations, which are physically unattended and
# outside our control - the one place in the system where untrusted input
# arrives - so it should never hold root inside the container.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

# The tile cache is a named volume. Docker seeds a *new, empty* volume from the
# image's directory, ownership included - so this directory has to exist and be
# owned by the runtime user here, or the volume mounts in root-owned and the
# unprivileged process cannot write a single tile.
RUN mkdir -p /tiles && chown 10001:10001 /tiles

USER 10001

EXPOSE 8000

# A bare uvicorn, for anyone running this image without compose.
#
# The WebSocket ping timings deliberately are NOT here. The real deployment
# overrides this CMD with `python -m backend.scripts.start_app`, so flags set
# here never run — and the first version of this put them here, where they
# looked authoritative and did nothing. They live in that script, next to the
# frame cap, which is the only place they take effect.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
