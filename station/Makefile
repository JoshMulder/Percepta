# Release the station image — build multi-arch, push to the private registry,
# and sign it with the cosign key we hold. Run on a box INSIDE the trust
# boundary (server/docs/07-remote-update-distribution.md), from a checkout of the
# tagged commit. Deliberately NOT GitHub: the signing key and the push credential
# stay ours, and native-arm hardware builds arm64 without emulation.
#
#   make release VERSION=v0.1.0
#
# Needs docker (with buildx) and cosign, and in the environment:
#   REGISTRY_USER, REGISTRY_TOKEN   the push robot credential (token via stdin)
#   COSIGN_PASSWORD                 the passphrase for the cosign private key
# and, if not the defaults below: REGISTRY, COSIGN_KEY.

VERSION    ?=
REGISTRY   ?= registry.percepta.aeronavics.com
IMAGE      := $(REGISTRY)/percepta-gsu
PLATFORMS  ?= linux/amd64,linux/arm64
COSIGN_KEY ?= cosign.key

# One shell per recipe, strict, so the multi-step release either completes or
# stops — a half-signed or unsigned push must not look like a success.
.ONESHELL:
.SHELLFLAGS := -eu -o pipefail -c
SHELL := bash
.PHONY: release cosign-keygen

release:
	@[ -n "$(VERSION)" ] || { echo "VERSION is required, e.g. make release VERSION=v0.1.0"; exit 2; }
	command -v cosign >/dev/null 2>&1 || { echo "cosign is not installed"; exit 1; }
	[ -f "$(COSIGN_KEY)" ] || { echo "$(COSIGN_KEY) not found — run: make cosign-keygen"; exit 1; }
	# Release the tag, cleanly. A dirty tree or a HEAD that is not the tag means
	# the artifact would not match the version stamped into it.
	[ -z "$$(git status --porcelain)" ] || { echo "working tree is dirty; release from a clean checkout of the tag"; exit 1; }
	[ "$$(git rev-parse HEAD)" = "$$(git rev-parse '$(VERSION)^{commit}' 2>/dev/null)" ] || { echo "HEAD is not at tag $(VERSION); check the tag out first"; exit 1; }
	# Push credential — token on stdin, never on the command line.
	printf '%s' "$$REGISTRY_TOKEN" | docker login "$(REGISTRY)" -u "$$REGISTRY_USER" --password-stdin
	# Multi-arch build straight to the registry.
	docker buildx build \
	  --platform "$(PLATFORMS)" \
	  --file deploy/Dockerfile \
	  --build-arg GSU_VERSION="$(VERSION)" \
	  --tag "$(IMAGE):$(VERSION)" \
	  --push .
	# The manifest-list digest is the immutable thing a station pulls and
	# verifies — read it back from the registry rather than trusting the tag.
	DIGEST="$$(docker buildx imagetools inspect "$(IMAGE):$(VERSION)" --format '{{.Manifest.Digest}}')"
	[ -n "$$DIGEST" ] || { echo "could not resolve the pushed digest"; exit 1; }
	# Sign by digest with our key, then verify with its public half exactly as a
	# station will — a key mismatch fails the release here, not in the field.
	cosign sign --yes --key "$(COSIGN_KEY)" "$(IMAGE)@$$DIGEST"
	cosign verify --key "$(COSIGN_KEY).pub" "$(IMAGE)@$$DIGEST" >/dev/null
	echo
	echo "released $(IMAGE):$(VERSION)"
	echo "  digest  $$DIGEST"
	echo "  update:  POST /api/stations/<id>/update  {\"image\":\"$(IMAGE)\",\"digest\":\"$$DIGEST\"}"

# One-time: the release signing keypair. Guard cosign.key — it is the whole trust
# root for updates — and commit nothing (see .gitignore). cosign.pub is pinned on
# stations via the enrolment response (piece A5).
cosign-keygen:
	cosign generate-key-pair
	echo "Generated cosign.key (GUARD THIS) and cosign.pub (pin on stations)."
