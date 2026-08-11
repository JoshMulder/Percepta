# 07. Remote update: secure image distribution to the fleet

**Status: Accepted 2026-08-11; not yet implemented.** Refines DECISIONS.md item 48, which is implemented on the
station and host-updater side (the marker handoff, `gsu-update.sh`, the
publish-gate and rollback) but assumed a distribution shape that does not fit the
security bar below. Nothing here is built yet; this note is the thing to react to
before code moves.

---

## The problem

The station and host-updater halves already exist and are reviewed and
branch-tested: a `system.update` command names an image digest, a sibling
`updater` container pulls it, verifies a signature, recreates the agent, gates on
it publishing, and rolls back if it does not. What is *not* settled is where the
image lives and how a station is allowed to pull it. The constraints:

- **Nothing public.** No public registry, and no public signing infrastructure
  (which rules out cosign *keyless*, whose verify step contacts Sigstore's public
  Fulcio/Rekor and whose transparency log is public).
- **No standing shared secret.** One registry token baked into every box is the
  anti-pattern: one compromised station leaks it, and rotating means touching
  every box. Whatever authenticates a pull must be **per-station and
  revocable**.
- **Bandwidth.** Stations are on metered Starlink. Only a real layer-deduplicated
  `docker pull` is viable — anything that ships the whole image each update (a
  `docker save` tarball; the station image carries the whisper models) is a
  non-starter.
- **Outbound only.** Stations are behind CGNAT; the platform can never initiate a
  connection to one. Everything is a station-initiated pull over a channel the
  station already trusts.
- **It has to scale to N stations** with no per-box manual credential handling.

## What item 48 assumed, and what changed

Item 48 reached for the cloud-native defaults: push to GHCR, sign with cosign
**keyless**. That is a sensible *first* shape, but each default fails a
constraint above once examined:

- GHCR (even private) is an external dependency in the trust path, and it does
  not cleanly mint short-lived, per-station, pull-scoped credentials.
- Keyless signing derives its entire value from a public identity provider and a
  public transparency log — exactly the "public" we are removing.
- Building the signed artifact on GitHub's hosted runners means our **signing key
  and registry push credential live as secrets on infrastructure we do not own**,
  and the release image is built on machines we do not control. For a design
  whose whole point is a tight, self-hosted trust boundary, the release must not
  run there.

The station/updater mechanism does not care where the image was built or which
registry it came from — it pulls a signed digest and verifies it against a
trusted key. So distribution can change without disturbing the reviewed
mechanism.

## Decision

Reuse the trust root we already built at enrolment; do not invent a second one.

1. **A private registry beside the platform.** One `registry:2` (CNCF
   Distribution) container, blobs on the platform's disk, exposed on a registry
   vhost behind the **same reverse proxy and TLS the platform already
   terminates**. Not Harbor: its UI, RBAC, database and scanner duplicate what
   the platform already is — the platform is our control plane and identity
   authority.

2. **Stations pull with their enrolment credential; the platform is the
   registry's token-auth service.** A station holds a **bearer credential** from
   enrolment (`credentials.py`, `type: "bearer"`; it presents `Authorization:
   Bearer …` to the API — it is *not* an mTLS client cert; the pinned CA only
   verifies the platform's *server* certificate). The registry uses the
   Distribution token-auth flow with the **platform** as the auth service:

   - The updater does `docker login <registry> -u <station-id> -p <bearer>` using
     the credential already in the state volume.
   - On pull, the registry answers `401` with a `WWW-Authenticate: Bearer
     realm=<platform token endpoint>` challenge.
   - Docker presents the station's credential to that platform endpoint, which
     validates it **exactly as it already validates that credential for the API**
     and mints a **short-lived, pull-only, single-repository JWT** signed with a
     key `registry:2` trusts.
   - Docker pulls with the JWT; the JWT expires in minutes.

   No new secret exists on the box, the pull credential *is* the enrolment
   identity, the minted token is ephemeral, and **revocation is the de-authorise
   operation we already have** — a de-authed station stops pulling exactly as it
   stops publishing telemetry. The `system.update` command carries no secret and
   is unchanged (image + digest).

3. **Sign with a private key, pinned on the station, off Sigstore.** The release
   signs with `cosign sign --key <our private key>`; the station runs `cosign
   verify --key <pinned public key>`. The public key is pinned on the station —
   delivered in the enrolment response alongside the broker CA, or baked into the
   updater image — so verification needs nothing off-box.

### The release process is ours, not GitHub

There is no "CI" in the trust path. What we need is a **repeatable release
step**, run on a box inside the boundary: from a clean checkout of the tagged
commit → buildx multi-arch → `cosign sign --key` → push to the private registry.
A `make release VERSION=vX.Y.Z` and a git tag is auditable enough for this
cadence; if releases become frequent, a **self-hosted** runner (Forgejo/Woodpecker)
automates it later, still inside the boundary.

- Building on our own arm64 box is **faster**, not slower — GitHub's x86 runners
  build the Pi image under QEMU emulation.
- Keep GitHub for **test** CI if wanted (the pytest/console suites expose no
  secrets and do no signing). It is only the **signed release artifact** that
  comes home.

## End-to-end flow

```
release box (ours)                platform (.49)                 station (field)
──────────────────                ──────────────                 ───────────────
make release vX.Y.Z
  buildx multi-arch
  cosign sign --key ───► push ──► registry:2  (blobs on disk)
                                  token-auth endpoint ◄── validates bearer,
                                                          mints short-lived
                                                          pull JWT
operator triggers ────────────►  system.update{image,digest}
                                  (over the broker; no secret) ─► updater:
                                                                   docker login
                                                                     (enrolment
                                                                      credential)
                                                                   pull @digest
                                                                     (layers only)
                                                                   cosign verify
                                                                     --key <pinned>
                                                                   recreate → gate
                                                                     → rollback
```

## What this touches

- **`server/docker-compose.yml`**: add the `registry:2` service (persistent
  volume for blobs).
- **Reverse proxy**: a registry vhost, and a route to the platform token-auth
  endpoint; push path gated by a CI/robot credential, pull path by the token
  flow.
- **Platform (`server/app`)**: a token-auth endpoint that validates a station's
  bearer credential and issues a scoped, short-lived registry JWT (reuses the
  existing credential validation and JWT machinery).
- **`.github/workflows/release.yml`** → **`make release`** on a box we own
  (build + `cosign sign --key` + push). GitHub keeps only the test workflows, if
  any.
- **`station/deploy/gsu-update.sh`**: point at the platform registry; `docker
  login` with the enrolment credential from the state volume; verify with
  `cosign verify --key <pinned>` instead of the keyless identity flags.
- **`station/docker-compose.yml` / updater**: mount the enrolment credential and
  the pinned public key into the updater; drop the keyless cosign config.
- **Enrolment**: include the cosign public key in the response (or bake it into
  the updater image), with a rotation story.
- **DECISIONS.md item 48**: superseded on distribution, signing and build; the
  handoff/gate/rollback mechanism it describes stands.

## Alternatives considered

- **GHCR-private + platform-minted tokens.** Keeps an external dependency in the
  path and does not mint short-lived per-pull scoped tokens cleanly.
- **Platform serves image bundles (`docker save`/`docker load`).** Cleanest trust
  story (reuses the enrolment channel, zero new infra) but ships the whole image
  every update — dies on Starlink. Rejected on bandwidth.
- **Harbor.** A whole platform (DB, UI, RBAC, scanner) for what is one small
  registry plus the identity authority we already run.
- **mTLS client-cert pull.** Elegant, but the station has a *bearer* credential,
  not a client cert; adopting mTLS would mean issuing and renewing a second
  identity. Reuse the one that exists.
- **Keep the signed build on GitHub CI.** Puts the signing key and push
  credential on infrastructure we do not own and builds on machines we do not
  control — against the whole point.

## Decisions (2026-08-11), and the detail left to implementation

Resolved:

- **Push-path exposure — auth-gated internet endpoint.** The registry's push path
  is internet-reachable behind a robot credential, as the platform API already
  is; the release box publishes from anywhere. Auth-required is not public.
- **cosign key custody — on the release box, encrypted at rest.** The private key
  never leaves the one release box inside the boundary and is decrypted only for
  a release. Rotation generates a new key and re-pins its public half (below).
- **Public key delivery — in the enrolment/renewal response.** Shipped like the
  broker CA. A rotation delivers the new public key on the next renewal, with the
  station accepting old+new during an overlap window, so a key change reaches
  already-enrolled boxes without a chicken-and-egg update.
- **Registry retention — keep the last ~5 digests**, garbage-collected on a
  schedule. The updater also keeps the immediate previous ref on the box, so
  rollback never depends solely on the registry.

Left to settle at implementation time, not blocking:

- **Token TTL and scope** — minutes, `pull` only, single repository; the exact
  TTL.
- **Release build environment** — a native-arm box for a fast, un-emulated arm64
  build vs buildx emulation on x86, and whether to build each arch natively and
  stitch a manifest.
