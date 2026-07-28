# DITB Control & Monitoring Platform — Architecture Notes

Working document. Written before access to the DroneOps source, so everything about
auth/orgs/permissions here is a *placeholder to be reconciled* with the real DroneOps
patterns once that code is available.

**Superseded in part by `00-topology.md`**, which is now the canonical system definition.
This document predates it and uses "site" / "site controller" where the settled terminology
is **ground station (GSU)** / **onboard computer**. The reasoning below still holds; the
naming does not.

**Scope note:** the DJI Dock is being handled as a separate workstream. This document
covers the rest of the site: camera, ADS-B, VHF radio, weather station, floodlight, solar
array, Starlink. The dock is expected to land back in this platform later, so §7 records
the seams that need preserving for it.

## 1. The constraint that shapes everything

Remote site, Starlink backhaul, solar power. With the dock out of scope the driving
constraints narrow to two:

1. **The link will drop.** Starlink has obstruction dropouts and satellite handovers. The
   site must keep sensing, recording, and locally alerting through an outage, then
   reconcile when the link returns. Nothing about the site's core job should require the
   cloud to be reachable.
2. **Bandwidth is finite and possibly metered.** Continuous video to cloud is not viable.
   Video is pull-on-demand or event-triggered; telemetry is summarized at the edge.

**What changed when the dock left.** The original version of this document argued the edge
tier was needed for *flight safety* — that launch interlocks and return-to-home couldn't
depend on a cloud round trip. That argument no longer applies. The two-tier split is still
right, but it is now justified by continuity of recording/alerting and by bandwidth, which
is a weaker and more ordinary case. Worth being honest about: without the drone, this is a
remote sensor and security-site platform, and it should be designed as one rather than as a
flight system with the flying removed.

Power also changes character. Drone charging was the dominant load and the reason
state-of-charge gated operations. The remaining loads — compute, camera, radio, ADS-B
receiver, floodlight — are smaller and far steadier. Solar telemetry stays valuable, but as
site-health monitoring and floodlight/compute duty-cycling, not as a gate on operations.

## 2. Tier split

### Site controller (edge, one per site)
Talks to all local hardware over LAN/serial. Responsibilities:
- Protocol adapters for each subsystem (see §3)
- Local event detection and alerting; local recording buffer
- Store-and-forward telemetry buffer for backhaul outages
- Local video ingest; transcode/relay only on demand
- Continues sensing, recording, and alerting with zero cloud connectivity

### Cloud control plane
- Multi-org / multi-user / RBAC — **modeled on DroneOps**
- Fleet dashboard across sites, alerting, long-term telemetry, media archive
- Audit log (see §5)
- Operator video and radio session brokering

### Transport between tiers
MQTT over TLS with persistent sessions and QoS 1 remains a good fit — it survives
intermittent links and handles store-and-forward cleanly.

One caveat resolved: in the previous draft this choice was partly justified by the dock
already speaking MQTT via the DJI Cloud API, and I flagged that DroneOps might have a
competing device-transport convention worth deferring to. Having now read it — DroneOps has
no device connectivity layer at all (see `02-platform-reconciliation.md`). So MQTT is a free
choice standing on its own merits for lossy links.

Redis and S3-compatible object storage are already in the DroneOps stack, and both are
load-bearing here: Redis for control leases, live telemetry fan-out and operator presence;
object storage for the video and media archive.

Live video: **the WebRTC option offered here is now excluded** by topology rule 8 (nothing
flows directly from a GSU to a user). The shape is outbound push from the GSU to a server
ingest, with the server terminating and re-originating to viewers — which is also what
Starlink's CGNAT forces, since no inbound connection to a GSU is possible. See
`03-realtime-isolation.md` §7. Codec and ingest protocol remain open pending the camera
choice.

## 3. Subsystem integration surfaces

| Subsystem | Likely protocol | Notes / unknowns |
|---|---|---|
| Security camera | ONVIF for discovery + PTZ, RTSP for stream | Now the primary sensor and the main consumer of bandwidth. Vendor/model unknown. On-edge vs cloud analytics is an open decision, and matters more than it did — event detection at the edge is what keeps the site useful during an outage. |
| ADS-B in | dump1090 → SBS-1 BaseStation text or Beast binary over TCP | **Value drops without the drone.** Its strongest use was deconfliction against our own aircraft. What remains is airspace situational awareness and low-flying-traffic alerting — real, but a much thinner case. Worth confirming this is still wanted in phase one. |
| VHF radio | Remote-Radio: FastAPI WebSocket, JSON control + int16 PCM binary frames | Now read — see `02-platform-reconciliation.md` Part 2. It is **receive-only** and **airband-only** (108–137 MHz AM, hard-limited in code), has no authentication, is a single shared tuner across all listeners, and costs 384 kbit/s per listener uncompressed. Each of those is an integration problem. |
| Weather station | Modbus RTU/TCP, or vendor HTTP | No longer a launch interlock. Still useful for situational context, camera visibility conditions, and site-health alerting (icing, high wind on the mast, temperature extremes affecting battery). |
| Floodlight | Relay board, DMX, or Modbus | Should be slavable to camera events as well as manual control. With the drone gone this is now the site's main *actuator* — the primary thing an operator physically does, alongside PTZ and radio. |
| Solar array | Modbus TCP, commonly SunSpec, on the inverter/charge controller | State of charge, PV input, load draw. Site-health monitoring and duty-cycling policy. |
| Starlink | gRPC on the dish at 192.168.100.1:9200 | Exposes obstruction and outage statistics — useful for predicting link loss and for explaining gaps in the telemetry record after the fact. |

Every one of these needs a vendor/model confirmation before adapter work starts. Listed as
open questions in §6.

## 4. Permissions — the shape it needs to take

DroneOps gives us multi-org and multi-user. What this platform adds is that permissions must
scope to **physical capability at a specific site**, not just to data visibility:

- Scope is (org → site → subsystem), not just org. A user may monitor five sites and be
  cleared to act at one. **This is the main gap versus DroneOps**, whose roles are org-wide
  and flat, with no expression of "this role, but only at this resource". Postgres RLS gives
  org isolation and nothing finer. Largest piece of new design in the platform layer.
- Distinct capabilities worth separating: view telemetry; view live video; review recorded
  media; PTZ the camera; transmit on VHF; control floodlight; change alert thresholds;
  change site configuration.
- **VHF transmit is gated on a licensed human.** That grant needs to reference a credential
  with an expiry, not a boolean role bit. This survives the dock's removal intact — it was
  never a drone-specific concern. **Already solved in DroneOps**: `CertificationType` +
  `UserCertification` give org-configurable certifications with computed expiry and
  valid/expiring/expired states. Adding a radio operator certificate is an admin action, not
  a code change.
- Single-operator-at-a-time constraints still apply to radio transmit and to PTZ control.
  That's a lease/lock concept rather than a permission, but it lives next door and is easy
  to conflate. **Superseded:** no lease was built — see `04-production-readiness.md`.

To be reconciled against the actual DroneOps role model.

## 5. Regulatory and audit weight

- **VHF transmit** is licensed and subject to rules on who may operate. Log every
  transmission with operator identity.
- **Camera coverage** in remote-but-not-empty locations carries privacy obligations that
  vary by jurisdiction — retention policy needs to be per-org configurable. With the camera
  now the primary sensor, this is the leading compliance concern rather than a secondary one.
- Audit log should be append-only and cover every command issued to physical hardware, who
  issued it, and the site state at the time.

BVLOS and remote ID obligations move to the dock workstream.

## 6. Open questions

Blocking adapter work:
1. Camera vendor/model; ONVIF conformance; how many per site.
2. ADS-B receiver hardware and output format — *and* whether ADS-B is still in phase one
   given its reduced value without the drone.
3. Weather station make/model and interface.
4. Solar charge controller / inverter make; SunSpec support?
5. Floodlight control interface (relay? DMX? networked driver?).
6. Site controller hardware — what compute is actually going in the box, and its OS.

Blocking platform work — *both source questions now answered, see
`02-platform-reconciliation.md`*:

7. ~~DroneOps stack~~ — FastAPI + SQLAlchemy 2 + Postgres (RLS) + Redis + S3-compatible
   storage + Docker Compose, vanilla-JS frontend.
8. ~~Remote radio integration surface~~ — WebSocket, JSON + PCM. Receive-only, airband-only.
9. ~~Which VHF band~~ — **decided: airband only** (108–137 MHz AM). Existing DSP chain is fit
   for purpose unchanged.
10. ~~Transmit in phase one?~~ — **decided: receive only for now**, certified transceiver to
    be interfaced later. `NullTransmitter` and the `Transmitter` seam stay as they are; the
    permission and lease model is built for it in advance (`03-realtime-isolation.md` §8).
11. ~~Site-scoped permissions~~ — **decided: `site_grants` table alongside the existing org
    roles**, not a replacement. See `03-realtime-isolation.md` §3.
12. ~~Frontend~~ — **decided: entirely new frontend.**
13. Expected scale: sites per org, orgs, concurrent operators.
14. Can one user hold live streams from two orgs simultaneously? Assuming no (org-pinned
    connection) — see `03-realtime-isolation.md` §10.

## 7. Seams to preserve for the dock

The dock returns later. Cheap now, expensive to retrofit:

- **Device abstraction.** Model subsystems as typed devices with adapters behind a common
  interface, so a dock is another device class rather than a special case.
- **Command + audit path.** Build the "operator issues command to physical hardware, with
  identity and site state recorded" path generically. The dock's commands are higher-stakes
  but structurally identical.
- **Credential-gated permissions.** The licensed-operator-with-expiry concept built for VHF
  is exactly what BVLOS authorization will need.
- **Interlock hooks.** Even though nothing currently gates on weather or power, leave the
  evaluation point in the command path so interlocks can be introduced without restructuring.
- **Telemetry schema.** Keep it device-agnostic with per-device-type extension rather than
  modeling columns around the current sensor set.
