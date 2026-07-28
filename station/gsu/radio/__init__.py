"""The airband receiver: measurement, the squelch gate, and the audio uplink.

Three things in here are load-bearing rather than incidental:

**The noise floor is measured outside the channel** (`contract/README.md` rule
3, `server/docs/05-radio-integration.md` obligation 1). That correctness lives
entirely station-side — the platform's simulator is *told* the floor, so nothing
on that side can catch the failure it prevents. `dsp.py` implements it and
`tests/test_radio_dsp.py` is the regression.

**Receiver settings persist across restarts** (obligation 3). Frequency, gain
and ppm are the receiver's state, not the console's memory.

**There is no transmit.** Not a stub, not a disabled branch, nothing. Until the
fail-released design in `05-radio-integration.md` exists — hardware watchdog,
maximum transmission time, release on link loss enforced at the station —
there is nothing here to hold a PTT line, which is the only guarantee worth
anything on an unattended site. `tx_capable` is reported False and the console
disables PTT from it.
"""
