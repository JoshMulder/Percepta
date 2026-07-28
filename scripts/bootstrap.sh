#!/usr/bin/env bash
#
# Bring the stack up from a clean checkout. Safe to re-run: an existing .env is
# left alone, so re-running only rebuilds and restarts.

set -euo pipefail

cd "$(dirname "$0")/.."

fail() { echo "ERROR: $*" >&2; exit 1; }

# --- Host prerequisites -----------------------------------------------------
command -v docker >/dev/null 2>&1 || fail "docker is not installed."

if ! docker info >/dev/null 2>&1; then
    cat >&2 <<'EOF'
ERROR: cannot talk to the Docker daemon.

Almost always this means your user is not in the 'docker' group. Fix it with:

    sudo usermod -aG docker "$USER"

then start a NEW login shell (log out and back in, or run 'newgrp docker')
before re-running this script.
EOF
    exit 1
fi

docker compose version >/dev/null 2>&1 \
    || fail "Docker Compose v2 is not available ('docker compose version' failed)."

# --- Secrets ----------------------------------------------------------------
if [ -f .env ]; then
    echo "==> .env already exists, leaving it alone."
else
    echo "==> Generating .env with fresh secrets."
    [ -f .env.example ] || fail ".env.example is missing."
    cp .env.example .env

    gen() { python3 -c "import secrets;print(secrets.token_urlsafe($1))"; }
    fernet() {
        docker run --rm python:3.12-slim sh -c \
            "pip install -q cryptography >/dev/null 2>&1 && python -c \
            'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())'"
    }

    # Replace in place. Values are generated locally and never leave the host.
    sed -i \
        -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(gen 24)|" \
        -e "s|^APP_DB_PASSWORD=.*|APP_DB_PASSWORD=$(gen 24)|" \
        -e "s|^SECRET_KEY=.*|SECRET_KEY=$(gen 48)|" \
        -e "s|^POSTGRES_HOST=.*|POSTGRES_HOST=postgres|" \
        -e "s|^REDIS_URL=.*|REDIS_URL=redis://redis:6379/0|" \
        .env

    if key=$(fernet 2>/dev/null) && [ -n "$key" ]; then
        sed -i "s|^SECRETS_ENCRYPTION_KEY=.*|SECRETS_ENCRYPTION_KEY=${key}|" .env
    else
        echo "WARNING: could not generate SECRETS_ENCRYPTION_KEY. TOTP secrets" >&2
        echo "         will be stored in plaintext until you set one." >&2
    fi

    echo "==> Wrote .env. Back up SECRETS_ENCRYPTION_KEY separately from the"
    echo "    database - losing it makes the encrypted columns unrecoverable."
fi

# --- Stale volume warning ---------------------------------------------------
# Postgres data lives in a named volume outside this checkout, so deleting and
# re-cloning the repo does NOT reset the database. If an old volume survives
# with a different password than the new .env, startup fails with a confusing
# authentication error - warn before that happens.
if docker volume ls -q | grep -qx "percepta_postgres_data"; then
    echo "==> NOTE: an existing percepta_postgres_data volume was found."
    echo "    If this is a fresh start, wipe it first: docker compose down -v"
fi

# --- Up ---------------------------------------------------------------------
echo "==> Building and starting."
docker compose up -d --build

echo
echo "==> Up. Health: http://localhost:${APP_HOST_PORT:-8000}/api/health"
