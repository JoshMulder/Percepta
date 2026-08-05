# The station updater's image (DECISIONS.md item 48): docker CLI + compose +
# cosign, small and stable. Built locally and trusted as part of the deployment
# — only the AGENT image is pulled from a registry and signature-verified. The
# whole job is gsu-update.sh, watching the handoff and reconciling the agent to a
# signed target. The gate reads the agent's status over the agent's own loopback
# (docker exec), so this image needs no python of its own.
FROM docker:27-cli

# The compose plugin (`docker compose …`), cosign for signature verification, and
# bash for the script. No jq: the marker is our own flat JSON, read with sed.
RUN apk add --no-cache docker-cli-compose bash curl

# cosign from its release, pinned. VERIFY on the first build: bump the version if
# needed and confirm the download for the build arch.
ARG COSIGN_VERSION=2.4.1
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64)  arch=amd64 ;; \
      aarch64) arch=arm64 ;; \
      *) echo "unsupported arch $(uname -m)"; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/cosign \
      "https://github.com/sigstore/cosign/releases/download/v${COSIGN_VERSION}/cosign-linux-${arch}"; \
    chmod +x /usr/local/bin/cosign

COPY deploy/gsu-update.sh /usr/local/bin/gsu-update.sh
RUN chmod +x /usr/local/bin/gsu-update.sh

# Watch the handoff and reconcile continuously. `docker compose run --rm updater
# --status` (or --force) overrides this for a one-shot on the first box.
ENTRYPOINT ["/usr/local/bin/gsu-update.sh"]
CMD ["--watch"]
