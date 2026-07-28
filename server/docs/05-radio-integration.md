# Radio: Percepta against Remote-Radio's handover

Evaluated 2026-07-28 against `Remote-Radio/HANDOVER.md` (commit 894bcc8).

Percepta's radio is a *simulation* plus a control surface. Remote-Radio is the
real receiver. This is what the handover changes about the simulation, and what
it obliges the real integration to do.

---

## Fixed as a result

**Turning AUTO off must freeze the threshold where it was.** The handover is
explicit: "turning AUTO off freezes the threshold where it was". Percepta left
`manual_threshold_db` unset, so the gate carried on riding the noise floor with
AUTO showing off — the control appeared to do nothing. Now freezes.

**Default gain is fixed, not `auto`.** The handover warns the tuner's own AGC
desenses badly near strong transmitters, to the point that a stronger signal
reads *lower*. A ground station's mast-mounted antenna is exactly where that
bites, so the simulated receiver now starts at a fixed 37.2 dB. The UI already
carried the warning; the default contradicted it.

---

## Correct already

- **8 dB auto-squelch margin** matches.
- **Dragging the threshold leaves AUTO** matches.
- **Per-channel noise floors**, same table, so squelch behaves differently on a
  quiet rural channel than a noisy urban one.
- **Broadcasts keyed by frequency in the filename**, looping with a gap, audible
  only near their channel and fading across it.
- **Broadcasts advance whether or not anyone is listening**, so tuning in joins
  a transmission in progress.
- **ADS-B as a separate device.** The handover notes it needs a second dongle —
  118 MHz and 1090 MHz cannot share a tuner — which is how Percepta already
  models it.

---

## Deliberate divergences, and why they stand

**No calibrate control.** Removed on request as "just for testing with a crappy
SDR", and the handover supports that: the ±600 ppm swings are *this dongle*
coming up mis-programmed per initialisation, and its own recommendation 3 is
better hardware with a TCXO, which "calibrate once and forget" eliminates. The
ppm field remains for a one-off commissioning trim.

Conditional, though: if a ground station ever ships with an RTL2832U, calibrate
comes back — and with it the handover's rule that *a wrong frequency means
suspect a bad init and power-cycle, rather than reaching for the ppm value*.

**No spectrum display.** Removed on request. Note this is a *display* decision
only — see the integration requirement below, because the spectrum is what the
noise floor is measured from.

---

## Obligations on the real integration

**1. Measure the noise floor outside the channel.** The handover calls this "the
whole trick": the floor is the median of spectrum bins 15–50 kHz either side of
the channel, converted to in-channel power by a measured constant (14.81 dB). No
carrier, however strong or long, can bias it, and it is correct on the first
block after a retune.

It replaced an in-channel tracker with a nasty failure: a weak signal arriving
while the estimate was stale-high was treated as noise, the floor drifted up
toward it, and the gate latched shut permanently. **That is the regression to
test**, and Percepta's simulation cannot exercise it — the simulator is told the
floor rather than measuring it. So this correctness lives entirely in the
station-side adapter, untested by anything here.

**2. The onboard computer must stop the radio server gracefully.** The dongle
wedges if hard-killed mid-transfer and needs a *physical replug* — a USB reset
is not enough. On an unattended site, hours away, that is not a recoverable
fault. Whatever supervises the radio process must use its `/shutdown` endpoint
and never `SIGKILL`.

**3. Persist receiver settings station-side.** Remote-Radio keeps gain, ppm and
frequency in `state.json` across restarts. Percepta holds them in memory in the
simulator, and presets in the browser's `localStorage` — so nothing survives a
restart and presets do not follow an operator between machines. Both need a home
in the station and the database respectively.

**4. Budget for the second dongle.** ADS-B and airband cannot share one.

---

## The transmit hazard, which is worse here

The handover's warning is short and the most important line in it:

> the hazard to design for first is **stuck PTT** — a jammed transmitter blocks
> a frequency across its whole coverage area. Use a hardware watchdog, and fail
> *released* on link loss.

**This is more dangerous in Percepta than in Remote-Radio.** Remote-Radio runs on
a LAN with the operator in the building. Percepta's entire premise is unattended
sites on Starlink, where **link loss is routine, not exceptional** — every
obstruction dropout is a moment when a held PTT loses its operator. A design that
holds transmit while the link is down would jam an aeronautical frequency across
a whole region, unattended, until someone drives to the site.

Requirements, before `radio.transmit` is ever grantable:

- **Fail released.** Loss of the operator's connection releases PTT immediately.
  Not on a timeout measured in seconds — on the socket closing, and on the
  station's own link-loss detection independently.
- **Hardware watchdog** on the keying line, so a hung process cannot hold it.
- **Maximum transmission time** enforced at the station, independent of anything
  the console does.
- **Station-side enforcement.** None of the above may depend on the cloud, which
  is by definition unreachable in the case that matters.

`radio.transmit` remains in `UNGRANTABLE_CAPABILITIES` until all four exist, plus
the certified transceiver and the operator licensing that already gate it.
