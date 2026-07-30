"""The airband receiver: measurement, the squelch gate, and the audio uplink.

Two front ends, one controller. `simulated.SimulatedFrontEnd` synthesises a
spectrum; `rtlsdr.RtlSdrFrontEnd` drives a real RTL2832U through `rtl2832.py`
and demodulates it with `am.py`. Everything that decides whether anyone *hears*
anything is in `receiver.RadioController`, above both of them, so the gate
cannot behave one way on the bench and another in the field.

**Demodulation happens here, at the station.** Audio goes up; IQ does not. A
240 ksps IQ stream is around 4 Mbit/s and the audio it reduces to is 384 kbit/s
before compression and nothing at all while the squelch is shut. On a metered
satellite link that is not an optimisation, it is the design.

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
