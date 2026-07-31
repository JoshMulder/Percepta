"""Entry point.

    python -m gsu run                 # the station (this is what runs in the field)
    python -m gsu preflight           # everything that has to be true before it works
    python -m gsu enrol --token …     # claim a code without the local console
    python -m gsu devices             # what is fitted, and what was actually found
    python -m gsu bench               # what a tick costs — run this on the target
    python -m gsu camera              # grab one snapshot and say what it cost
    python -m gsu radio --freq 118.7  # open the dongle, listen, write a WAV
    python -m gsu stream --seconds 20 # run the H.264 encoder and measure it
    python -m gsu status              # what the platform thinks of us
    python -m gsu whoami              # what this box thinks it is, offline
    python -m gsu setup-password      # hash a setup-page password for the .env

`run` is the only one a technician ever causes to happen; the rest are for
whoever is debugging a box, and `preflight`, `devices` and `bench` are the three
that work with no link at all.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import socket
import ssl
import sys
import tempfile
import time

from . import AGENT_VERSION, clock, tls
from .agent import Agent
from .config import AgentConfig
from .devices.serialio import list_ports


def _logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
    )


def _bench(agent) -> int:
    """Measure what one tick of this agent costs, here.

    Reported as CPU milliseconds per tick and as a percentage of one core at
    1 Hz, so the number means the same thing on a workstation and on a Pi. It
    measures *this software only* — dump1090, an airband demodulator and the
    camera encoder are separate processes and have to be measured separately on
    the target, which is the whole point of shipping this as a command rather
    than quoting a figure from a different machine.
    """
    import platform

    for _ in range(3):
        agent.step(1.0)

    def measure(label: str, fn, runs: int) -> None:
        start_cpu = time.process_time()
        start_wall = time.monotonic()
        for _ in range(runs):
            fn()
        cpu = (time.process_time() - start_cpu) / runs * 1000
        wall = (time.monotonic() - start_wall) / runs * 1000
        print(f"  {label:36} {cpu:7.2f} ms CPU  {wall:7.2f} ms wall  "
              f"{cpu / 10:5.1f}% of one core at 1 Hz")

    print(f"\n{platform.machine()} / {platform.processor() or 'unknown'} / "
          f"Python {platform.python_version()}\n")
    front_end = agent.radio.front_end if agent.radio is not None else None
    set_traffic = getattr(front_end, "set_traffic", None)
    if set_traffic:
        set_traffic("off", transmitting=False)
    measure("full tick, squelch closed", lambda: agent.step(1.0), 100)
    if agent.radio is not None:
        measure("radio tick, squelch closed", lambda: agent.radio.tick(1.0), 100)
        if set_traffic:
            set_traffic("busy", transmitting=True)
            measure("radio tick, squelch open (1 s audio)", lambda: agent.radio.tick(1.0), 30)
    if agent.adsb is not None:
        measure("ADS-B poll (MAVLink decode)", lambda: agent.adsb.poll(1.0), 100)
    if agent.weather is not None:
        measure("weather read", lambda: agent.weather.read(5.0), 100)
    if agent.power is not None:
        measure("power read", lambda: agent.power.read(1.0), 100)
    print(
        "\nThe simulated airband front end synthesises audio in Python, which no\n"
        "real station does — on hardware that work is the SDR pipeline's, in its\n"
        "own process. Measure that separately.\n"
    )
    agent.shutdown()
    return 0


def _refuse_while_the_service_runs(agent, command: str) -> bool:
    """Stop a CLI command from becoming a second opener of one sensor.

    The sensor lease in `camera/ownership.py` makes ownership unambiguous
    *within* the station process. It cannot see another process at all, and
    `gsu camera` or `gsu stream` on a box where the service is up is exactly
    that: two independent programs opening one CSI ribbon, with nothing in
    between them. On the hardware this produces a `rpicam-vid` that fails to
    acquire and a stream that runs for its whole lease delivering zero frames —
    which is precisely the symptom that has been read as a camera fault twice.

    Refused with the fix in the message rather than a return code, because the
    person running this is debugging a camera and needs to know that the tool
    they reached for is the thing in the way.
    """
    if not agent.another_agent_is_running():
        return False
    print(
        f"\nThe station service is running, so `{command}` would be a second "
        f"program opening the same camera.\n"
        f"The two cannot share it: one of them gets the sensor and the other "
        f"reports a fault that\nlooks like broken hardware.\n\n"
        f"  sudo systemctl stop percepta-station    # then run this again\n\n"
        f"Or read the running station instead, which needs nothing stopped — the "
        f"setup page's\ncamera tab shows the live preview and the capture path, "
        f"and the health frame carries\n`video.sensor`, which names whatever is "
        f"holding the camera right now.\n",
        file=sys.stderr,
    )
    agent.shutdown()
    return True


def _camera(agent, frames: int, size: str | None, out: str | None) -> int:
    """Take a few frames and say what each one cost to take.

    The other half of `stream`: one complete JPEG at a time, which is what the
    setup page's preview does. Nothing here is published — the periodic
    snapshot channel was removed — so the numbers are capture cost, not link
    cost.
    """
    if _refuse_while_the_service_runs(agent, "gsu camera"):
        return 1
    camera = agent.camera
    if camera is None:
        print("\nNo camera fitted, so there is nothing to capture.\n")
        agent.shutdown()
        return 1
    if size:
        from .camera import parse_resolution

        camera.width, camera.height = parse_resolution(size)
        columns = getattr(camera, "columns", None)
        if columns is not None:  # the synthetic test card redraws its grid
            camera.columns = (camera.width + 7) // 8
            camera.rows = (camera.height + 7) // 8

    print(f"\n{platform.machine()} / {camera.describe().detail}\n")
    sizes: list[int] = []
    last = None
    for index in range(max(1, frames)):
        started = time.monotonic()
        frame = camera.capture()
        elapsed = (time.monotonic() - started) * 1000
        if frame is None:
            print(f"  {index + 1}: no frame — {camera.unavailable_reason}")
            continue
        sizes.append(len(frame.jpeg))
        last = frame
        from .camera import iso

        print(f"  {index + 1}: {len(frame.jpeg) / 1024:6.1f} kB JPEG, "
              f"{frame.width}x{frame.height}, {elapsed:6.1f} ms, "
              f"captured {iso(frame.captured_at)}")
    if not sizes:
        print("\nNothing was captured. The reason above is the whole diagnosis.\n")
        agent.shutdown()
        return 1
    mean = sum(sizes) / len(sizes)
    # No sustained figure any more, and its absence is the point: these frames
    # are not published anywhere. The number that mattered on a metered link
    # was the snapshot channel's, and that channel is gone — what costs
    # bandwidth now is the live stream, which `gsu stream` measures.
    print(f"\n  {mean / 1024:.1f} kB per frame, mean over {len(sizes)} frame(s). "
          f"Nothing was published: these are preview captures.")
    if out and last is not None:
        with open(out, "wb") as handle:
            handle.write(last.jpeg)
        print(f"  last frame written to {out}")
    print()
    agent.shutdown()
    return 0


def _adsb(agent, seconds: float, out: str | None) -> int:
    """Dump decoded contacts, every field, exactly as they would be published.

    The point of this command is the *nulls*. A receiver on a bench with one
    aircraft overhead will populate almost everything; a field that comes back
    null here is either a validity flag the transmitting aircraft left clear or
    a field this station cannot source, and the difference is what somebody
    standing next to the hardware needs to see. So it prints the payload
    verbatim rather than a tidied summary, and states the correction's status
    alongside — a null `altitude_corrected_m` on every line has four possible
    causes and only one of them is about the aircraft.
    """
    import json

    if _refuse_while_the_service_runs(agent, "gsu adsb"):
        return 1
    if agent.adsb is None:
        print("\nNo ADS-B receiver fitted, so there is nothing to decode.\n")
        agent.shutdown()
        return 1

    print(f"\nListening for {max(1.0, seconds):.0f} s...")
    deadline = time.monotonic() + max(1.0, seconds)
    contacts: list = []
    while time.monotonic() < deadline:
        # The whole tick, not just the receiver: the barometer reaches the
        # correction through the weather slot, and polling the receiver alone
        # would report the correction as idle on a station where it works.
        agent.step(1.0, weather_due=True)
        contacts = agent.adsb.poll(1.0) or contacts
        time.sleep(0.5)

    # Everything below is reported *after* listening. A receiver that has been
    # constructed and not yet read from reports itself absent, and a header
    # printed up front would say so on a station that is working perfectly.
    print(f"\n{agent.adsb.describe().detail}")
    correction = agent.baro.state()
    print(
        "  altitude correction: "
        + ("active" if correction["active"]
           else f"idle — {correction['reason']}")
    )
    if correction["active"]:
        print(
            f"    {correction['station_pressure_hpa']} hPa measured at "
            f"{correction['station_elevation_m']} m -> "
            f"{correction['sea_level_pressure_hpa']} hPa at sea level"
        )
    print()

    payloads = [contact.to_payload() for contact in contacts]
    for payload in payloads:
        print(json.dumps(payload, sort_keys=True))
    print(f"\n  {len(payloads)} contact(s) with a decoded position.")
    if agent.adsb.positionless:
        print(
            f"  {agent.adsb.positionless} heard without one, not published — "
            "range and bearing are required and cannot be invented."
        )
    for line in agent.adsb.raw_sample():
        print(f"  {line}")
    if out:
        with open(out, "w") as handle:
            json.dump(payloads, handle, indent=2, sort_keys=True)
        print(f"  written to {out}")
    print()
    agent.shutdown()
    return 0


def _stream(agent, config: AgentConfig, seconds: float, out: str | None,
            size: str | None, fps: float | None, bitrate: int | None) -> int:
    """Run the live encoder for a few seconds and say exactly what it produced.

    **This is the first thing to run on the Pi**, before the platform, before
    the console, before anything else about video. It answers the largest open
    hardware question in this build — whether the VideoCore's H.264 encoder
    sustains 1080p30 on a Pi 2B — with frames, bytes and a file that can be
    played, rather than with arithmetic from a different machine.

    It needs no platform, no network and no enrolment. It does need the camera
    to itself — see `_refuse_while_the_service_runs`.
    """
    import dataclasses

    from .camera.h264 import StreamSettings

    if _refuse_while_the_service_runs(agent, "gsu stream"):
        return 1

    sink = out or os.path.join(
        "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir(),
        "gsu-stream.mp4",
    )
    agent.config = dataclasses.replace(config, stream_sink=sink)
    if size:
        width, _, height = size.lower().partition("x")
        agent.site.stream_width, agent.site.stream_height = int(width), int(height)
    if fps:
        agent.site.stream_fps = fps
    if bitrate:
        agent.site.stream_bitrate_kbps = bitrate

    from .camera.h264 import probe_encoders

    settings: StreamSettings = agent.stream.settings()
    print(f"\n{platform.machine()} / agent {AGENT_VERSION}")
    print("encoders on this box:")
    for probe in probe_encoders():
        print(f"  {'yes' if probe.available else 'no ':4} {probe.name:9} {probe.detail}")
    print(f"asking for {settings.width}x{settings.height} at {settings.fps} fps, "
          f"{settings.bitrate_kbps} kbit/s target, keyframe every "
          f"{settings.intra_period} frames\n")
    effect = agent.stream.start({"lease_s": seconds + 30, "viewers": 1})
    print(f"  {effect}")
    if agent.stream.state != "streaming":
        print("\nNothing was encoded. That message is the whole diagnosis.\n")
        agent.shutdown()
        return 1

    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            time.sleep(0.5)
            agent.stream.tick()
            state = agent.stream.state_payload()
            print(
                f"\r  {state['frames']:6d} frames  {state['bytes'] / 1e6:7.2f} MB  "
                f"{state['bitrate_bps'] / 1e6:5.2f} Mbit/s  "
                f"{state['fps_measured']:5.1f} fps  {state['dropped']} dropped",
                end="", flush=True,
            )
            if state["state"] != "streaming":
                break
    except KeyboardInterrupt:
        pass
    state = agent.stream.state_payload()
    agent.stream.stop("the stream test finished")

    elapsed = time.monotonic() - started
    print(f"\n\n{state['frames']} frames in {elapsed:.1f}s")
    print(f"  measured   {state['fps_measured']:.1f} fps, "
          f"{state['bitrate_bps'] / 1e6:.2f} Mbit/s "
          f"({state['bytes'] / max(1, state['frames']) / 1024:.1f} kB/frame)")
    print(f"  asked for  {settings.fps} fps, {settings.bitrate_kbps / 1000:.2f} Mbit/s")
    print(f"  encoder    {state['encoder_choice'] or state['encoder']}")
    # The single value that says whether the whole chain agreed with itself:
    # what the parameter sets in the stream turned into, which is what the
    # platform is told and what a browser is asked to decode. Empty means
    # frames arrived and nothing sendable came out of them — on HEVC that is a
    # parameter set that would not read, and `reason` says so.
    print(f"  codec      {state['codec'] or '(none — see reason below)'}")
    print(f"  written to {sink}")
    if state["reason"]:
        print(f"  reason     {state['reason']}")
    if state["frames"] and state["fps_measured"] < settings.fps * 0.9:
        print(
            "\n  The encoder did not keep up. That is a hardware answer, not a\n"
            "  setting to nudge: report the number rather than quietly dropping\n"
            "  to 720p (HARDWARE.md §9)."
        )
    print(
        "\nPlay it back to prove the picture is real:\n"
        f"  ffplay {sink}     # or copy it off and open it anywhere\n"
    )
    agent.shutdown()
    return 0


def _radio(agent, freq_mhz: float | None, seconds: float, out: str | None,
           monitor: bool, gain: str | None, ppm: int | None) -> int:
    """Prove the dongle enumerates, tunes and produces audio. Run this on the Pi.

    The airband equivalent of `stream`, and for the same reason: the receiver's
    DSP is unit-tested against synthetic IQ, but nothing in this repository can
    tell you whether a real RTL2838 comes up on the frequency it claims. That
    needs the hardware, and this is the shortest path to an answer that is a
    measurement rather than an opinion.

    It works whether or not a tuner has been assigned on the setup page: with no
    allocation it opens the dongle directly and says so, because "the receiver
    is misconfigured" and "the dongle is dead" want telling apart before
    anything else.

    Two things it produces. Per-second RSSI, noise floor, threshold and gate
    state — which answers whether the front end is measuring sanely — and a WAV
    file of everything the gate let through, which answers whether it is a
    receiver. Use `--monitor` on a quiet channel to hold the gate open and
    record the noise: hearing the band hiss is how you know the audio path is
    real before you go looking for traffic.
    """
    import base64
    import wave

    print(f"\n{platform.machine()} / agent {AGENT_VERSION}\n")

    print("RTL-SDR dongles on the USB bus (from sysfs, no library needed):")
    resources = agent.inventory.resources()
    if not resources:
        print("  none. Nothing else here can work — check the lead, then `dmesg`.\n")
        agent.shutdown()
        return 1
    for resource in resources:
        print(f"  {resource.id:32} {resource.model}"
              + (f"  — {resource.detail}" if resource.detail else ""))

    from .radio.audio import AUDIO_RATE
    from .radio.receiver import RadioController

    controller = agent.radio
    # A simulated front end must never answer a hardware question. It would
    # produce a plausible WAV full of synthesised speech and a meter that moves,
    # which is the most misleading possible outcome of a test whose entire
    # purpose is to find out whether the dongle works.
    simulated = controller is not None and controller.describe().simulated
    if controller is None or simulated:
        from .radio.rtlsdr import RtlSdrFrontEnd

        why = (
            "the configured receiver is the simulator, which cannot answer this"
            if simulated else
            "no receiver is configured in this box's inventory"
            + (f" ({agent.inventory.reasons['radio']})"
               if agent.inventory.reasons.get("radio") else "")
        )
        print(
            f"\nOpening {resources[0].id} directly: {why}."
            "\nSelect the RTL-SDR airband receiver on the setup page and assign "
            "this dongle to it to have the station use it for real."
        )
        if controller is not None:
            controller.shutdown()
            agent.radio = None
        front_end = RtlSdrFrontEnd(
            gain=gain if gain is not None else 37.2,
            ppm=ppm if ppm is not None else 0,
            resource=resources[0].id,
        )
        controller = RadioController(front_end, state_path=None)
    else:
        if gain is not None:
            controller.set_gain(gain)
        if ppm is not None:
            controller.set_ppm(ppm)
    if freq_mhz:
        controller.tune(int(round(freq_mhz * 1e6)))

    print(f"\nWaiting for the dongle to open…")
    opened = False
    for _ in range(20):  # the open runs on its own thread; give it 10 s
        controller.tick(0.5)
        described = controller.describe()
        if described.present:
            opened = True
            break
        time.sleep(0.5)
    if not opened:
        print(f"  it did not open: {controller.describe().detail}\n")
        print(
            "  If that mentions permissions, the udev rule in "
            "deploy/99-percepta-sdr.rules is not installed or the user is not\n"
            "  in plugdev. If it mentions the device being busy, the kernel DVB "
            "driver has it — blacklist dvb_usb_rtl28xxu (DEPLOYMENT.md).\n"
        )
        controller.shutdown()
        agent.shutdown()
        return 1

    print(f"  {controller.describe().detail}\n")
    if monitor:
        controller.set_monitor(True)
        print("MON held: the gate is forced open, so this records whatever the "
              "receiver hears, traffic or not.\n")
    print(f"{'time':>5}  {'rssi':>8}  {'floor':>8}  {'thresh':>8}  gate")

    pcm = bytearray()
    open_ticks = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < seconds:
            tick_started = time.monotonic()
            payload, audio = controller.tick(1.0)
            if audio is not None:
                pcm += base64.b64decode(audio["pcm"])
                open_ticks += 1
            print(
                f"{time.monotonic() - started:5.0f}  "
                f"{payload['rssi_db']:7.1f}dB  {payload['noise_floor_db']:7.1f}dB  "
                f"{payload['threshold_db']:7.1f}dB  "
                f"{'OPEN' if payload['squelch_open'] else '····'}"
            )
            # An absolute deadline, the same as the agent's own loop: sleeping
            # for a second after doing a second's work is how audio drifts.
            delay = 1.0 - (time.monotonic() - tick_started)
            if delay > 0:
                time.sleep(delay)
    except KeyboardInterrupt:
        pass

    elapsed = time.monotonic() - started
    controller.set_monitor(False)
    described = controller.describe()
    controller.shutdown()

    print(f"\n{elapsed:.0f}s observed, gate open for {open_ticks}s "
          f"({open_ticks / max(elapsed, 1) * 100:.0f}% of the time)")
    print(f"  {described.detail}")
    if pcm:
        sink = out or os.path.join(
            "/dev/shm" if os.path.isdir("/dev/shm") else tempfile.gettempdir(),
            "gsu-radio.wav",
        )
        with wave.open(sink, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(AUDIO_RATE)
            handle.writeframes(bytes(pcm))
        seconds_of_audio = len(pcm) / 2 / AUDIO_RATE
        print(f"  {seconds_of_audio:.1f}s of audio written to {sink}")
        print(f"  play it:  aplay {sink}     # or copy it off and open it anywhere")
        # What this would have cost the link, measured rather than estimated.
        print(
            f"  uplink:   {len(pcm) * 4 / 3 * 8 / max(seconds_of_audio, 1) / 1000:.0f} "
            "kbit/s while the gate was open (base64 PCM16, per "
            "contract/transport.md — Opus would be a twentieth of that)"
        )
        if abs(seconds_of_audio - open_ticks) > 0.05:
            print(
                f"\n  WRONG: {seconds_of_audio:.3f}s of audio for {open_ticks} "
                "open ticks. Exactly one second per second is the contract, and\n"
                "  the platform's player stutters on anything else."
            )
    else:
        print(
            "\n  No audio: the gate never opened. On airband that is the normal "
            "state of an empty channel — re-run with --monitor to hold it open\n"
            "  and prove the audio path, or point --freq at a busy tower "
            "frequency and wait for an over."
        )
    print()
    agent.shutdown()
    return 0


def _handshake(url: str, trust: tls.Trust, timeout: float = 8.0) -> tuple[bool, str]:
    """Open a TLS connection and verify the certificate. Nothing else.

    No HTTP, no RESP, no credentials — this answers only "would this station
    accept that server's certificate", which is the question that is hardest to
    answer from a log line and the one most likely to be wrong on the day. It
    sends no token and no secret, so it is safe to run before enrolment.
    """
    host = tls.host_of(url)
    tail = url.split("://", 1)[-1].split("/", 1)[0]
    port_text = tail.rsplit(":", 1)[-1] if ":" in tail and not tail.endswith("]") else ""
    default_port = 443 if url.startswith("https") else 6380
    try:
        port = int(port_text) if port_text.isdigit() else default_port
    except ValueError:
        port = default_port
    try:
        context = trust.context()
    except tls.Refusal as exc:
        return False, str(exc)
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as secure:
                cert = secure.getpeercert() or {}
        subject = dict(
            item for entry in cert.get("subject", ()) for item in entry
        ).get("commonName", "?")
        return True, (
            f"verified {host}:{port}, certificate CN={subject}, "
            f"expires {cert.get('notAfter', '?')}"
        )
    except ssl.SSLCertVerificationError as exc:
        return False, f"certificate rejected: {exc.verify_message or exc}"
    except (OSError, ssl.SSLError) as exc:
        return False, f"could not reach {host}:{port}: {exc}"


def _preflight(agent, config: AgentConfig, probe: bool) -> int:
    """Everything that has to be true before this box can work, checked here.

    Written to be run over SSH on a box that is not behaving, and to be the
    first step in DEPLOYMENT.md's commissioning list. It says PASS, WARN or FAIL
    per line and returns non-zero if anything failed, so it can also be the
    thing a person runs before driving away.
    """
    failures = warnings = 0

    def line(state: str, label: str, detail: str = "") -> None:
        nonlocal failures, warnings
        if state == "FAIL":
            failures += 1
        elif state == "WARN":
            warnings += 1
        print(f"  {state:4}  {label}")
        if detail:
            for part in str(detail).split("\n"):
                print(f"          {part}")

    print(f"\n{platform.machine()} / {platform.system()} "
          f"{platform.release()} / Python {platform.python_version()}")
    print(f"agent {AGENT_VERSION}, state in {config.home}\n")

    # Read the stored identity now rather than attaching to it: preflight must
    # be safe to run on a box that is already running the service, so it opens
    # no broker connection and starts no renewer.
    stored = None
    stored_error = None
    try:
        stored = agent.credentials.load()
    except ValueError as exc:
        stored_error = str(exc)
    broker_url = config.broker_url or (stored.broker.url if stored else None)

    # --- the clock, which is what strands a remote site (enrolment.md §6) ---
    print("Clock")
    reason = clock.implausible_reason()
    if reason:
        line("FAIL", "plausible", reason + " Enrolment will be refused until this is fixed.")
    else:
        line("PASS", "plausible", clock.now().isoformat())
    state = clock.discipline(force=True)
    if state.synchronised is True:
        line("PASS", f"disciplined by {state.source}", state.detail)
    elif state.synchronised is False:
        line("FAIL", "not disciplined", state.detail)
    else:
        line("WARN", "cannot tell what keeps this clock", state.detail)
    if state.rtc_present:
        line("PASS", "hardware RTC present", "time survives a power cut")
    else:
        line("WARN", "no hardware RTC",
             "This box boots with no idea of the time until NTP answers. "
             "See HARDWARE.md §4 — an RTC module is a few pounds.")

    # --- trust, before anything is sent anywhere ---
    # Two roots, reported separately: the broker is pinned to a private CA, the
    # API is verified against the system bundle unless pinned deliberately.
    print("\nTrust")
    if agent.trust.path is None:
        line("WARN", "broker: no CA pinned yet",
             "The broker's CA arrives in the enrolment response. Normal before "
             "the first enrolment; pre-provision it with GSU_CA_FILE if you "
             "want to check the address before enrolling.")
    else:
        line("PASS", f"broker: CA pinned from {agent.trust.source}",
             f"{agent.trust.path}\nSHA-256 {agent.trust.fingerprint}\n"
             "Compare with: openssl x509 -in ca.crt -noout -fingerprint -sha256")

    if agent.api_trust.mode == tls.TRUST_SYSTEM:
        line("PASS", "platform API: system CA bundle",
             "The expected setting for an API behind a reverse proxy with a "
             "public certificate. Set GSU_API_CA_FILE to pin it instead.")
    elif agent.api_trust.path is None:
        line("FAIL", "platform API: pinning was asked for and is not usable",
             "GSU_API_CA_FILE is set to something that could not be read.")
    else:
        line("PASS", "platform API: CA pinned",
             f"{agent.api_trust.path}\nSHA-256 {agent.api_trust.fingerprint}")

    for label, url, trust in (("platform API", config.platform_url, agent.api_trust),
                              ("broker", broker_url, agent.trust)):
        if not url:
            line("WARN", f"{label}: no address yet",
                 "not enrolled, and no GSU_BROKER_URL set")
            continue
        try:
            trust.check(url, label)
        except tls.Refusal as exc:
            line("FAIL", f"{label}: refused", str(exc))
            continue
        if tls.is_tls(url):
            if probe:
                ok, detail = _handshake(url, trust)
                line("PASS" if ok else "FAIL", f"{label}: {url}", detail)
            else:
                line("PASS", f"{label}: {url}", "TLS, not probed (use --probe)")
        else:
            line("WARN", f"{label}: {url}", "plaintext — development only")

    # --- identity ---
    print("\nIdentity")
    enrolment = stored
    if stored_error:
        line("FAIL", "stored credential unreadable", stored_error)
    if enrolment is None:
        line("WARN", "not enrolled", "Enter a code on the setup page.")
    else:
        remaining = enrolment.credential.seconds_remaining() / 3600
        line("PASS" if remaining > 0 else "FAIL",
             f"enrolled as {enrolment.site.name}",
             f"{enrolment.station_id}\ncredential expires in {remaining:.1f} h "
             f"({enrolment.credential.expires_at.isoformat()})")
    for path in (config.credential_path, config.ca_path):
        if not path.exists():
            continue
        mode = path.stat().st_mode & 0o777
        line("PASS" if mode == 0o600 else "FAIL", f"{path.name} permissions",
             f"{oct(mode)}" + ("" if mode == 0o600 else " — should be 0600"))

    # --- the setup page, which is how somebody without a terminal installs ---
    #
    # Here because the answer is only interesting *before* somebody drives out.
    # "The page will be on loopback only" is cheap to learn at a desk and
    # expensive to learn standing at an enclosure with a laptop.
    print("\nSetup page")
    if not config.setup_enabled:
        line("WARN", "disabled", "GSU_SETUP=0. There is no web setup on this box, "
                                 "and deploy/gsu-update.sh has no health endpoint "
                                 "to gate updates on.")
    else:
        from .setup_access import is_loopback_host

        loopback = is_loopback_host(config.setup_host)
        has_password = bool(config.setup_password)
        if loopback:
            line("PASS", f"http://{config.setup_host}:{config.setup_port}",
                 "Loopback only — reach it over an SSH tunnel. Nobody can open "
                 "it from a laptop on site. Set GSU_SETUP_HOST and a password "
                 "if they need to.")
        elif not has_password:
            line("FAIL", f"GSU_SETUP_HOST={config.setup_host} will be ignored",
                 "No GSU_SETUP_PASSWORD_HASH, so the page binds to loopback "
                 "instead of serving an unauthenticated form on a routable "
                 "interface. Run `python -m gsu setup-password`.")
        else:
            line("PASS", f"http://{config.setup_host}:{config.setup_port}",
                 "Password required from the local network.")
        if has_password and not str(config.setup_password).startswith("pbkdf2_"):
            line("WARN", "the setup password is stored in plain text",
                 "Works, but `python -m gsu setup-password` gives a hash that "
                 "survives the environment file being read.")
        if not loopback and has_password and config.setup_window_minutes <= 0:
            line("WARN", "GSU_SETUP_WINDOW_MINUTES=0",
                 "The page will answer on the local network for as long as this "
                 "station runs. That is a permanent unattended listener.")

    # --- what is plugged in ---
    print("\nSerial ports present")
    ports = list_ports()
    if not ports:
        line("WARN", "none", "Neither USB-UART is enumerating. Check leads, then dmesg.")
    for port in ports:
        line("PASS" if port.stable else "WARN", port.path,
             port.target if port.stable else
             "unstable name — use the /dev/serial/by-id/… one instead")

    print("\nDevices")
    for _ in range(3):
        agent.step(1.0)
        time.sleep(0.2)
    for report in agent.inventory.report():
        if not report.configured:
            line("WARN", f"{report.slot}: nothing fitted")
        elif report.status == "present":
            line("PASS", f"{report.slot}: {report.label}", report.detail)
        else:
            line("FAIL", f"{report.slot}: {report.label}", report.detail)
    for conflict in agent.inventory.conflicts():
        line("FAIL", "device conflict", conflict)

    print(f"\n{failures} failed, {warnings} warned.")
    if failures:
        print("A FAIL means this station will not do that thing. Fix, re-run.\n")
    agent.shutdown()
    return 1 if failures else 0


def _setup_password(from_stdin: bool = False) -> int:
    """Turn a typed password into the line that goes in the environment file.

    The setup page will accept a plain `GSU_SETUP_PASSWORD`, and on a 0640
    root:gsu file that is not unreasonable. This exists because the hash is
    strictly better for the same effort: it survives the environment file being
    read — by a backup, by an image someone copies, by anyone who ends up in
    the `gsu` group — and the page behaves identically either way.

    Read with `getpass`, so it is not in a shell history or in `ps`.

    `--stdin` reads it from a pipe instead and prints the bare hash, for
    provisioning: `bootstrap.sh` has to set this without a human at a terminal,
    and a pipe keeps the password out of argv exactly as getpass keeps it out
    of the history. No confirmation prompt on that path — there is nobody to
    mistype it twice — and no banner, so the output is the hash and nothing
    else.
    """
    import getpass

    from .setup_access import ITERATIONS, hash_password

    if from_stdin:
        password = sys.stdin.read().strip()
        if len(password) < 10:
            print("Too short: 10 characters or more.", file=sys.stderr)
            return 1
        print(hash_password(password))
        return 0

    try:
        password = getpass.getpass("Setup password: ")
        again = getpass.getpass("Again: ")
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 1
    if password != again:
        print("They do not match. Nothing written.")
        return 1
    if len(password) < 10:
        # Not a policy, a floor. The page is on a network an installer is
        # standing on; the thing to defend against is a four-digit guess, not a
        # cracking rig.
        print("Use at least 10 characters. Nothing written.")
        return 1
    print(
        "\nPut this in /etc/percepta/gsu.env, then restart the agent:\n\n"
        f"GSU_SETUP_PASSWORD_HASH={hash_password(password)}\n\n"
        f"(pbkdf2-sha256, {ITERATIONS} rounds. Remove any GSU_SETUP_PASSWORD "
        "line: the hash wins, and a stale plain line is a second password "
        "nobody knows is live.)\n"
        "Write the password itself on the box. It is what an installer types "
        "into the setup page from a laptop or a phone, and it is not the "
        "enrolment code."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gsu", description=__doc__)
    parser.add_argument("command", nargs="?", default="run",
                        choices=["run", "preflight", "enrol", "status", "whoami",
                                 "devices", "bench", "camera", "radio", "stream",
                                 "adsb", "setup-password"])
    parser.add_argument("--token", help="enrolment code, as issued (XXXX-XXXX-XXXX)")
    parser.add_argument("--stdin", action="store_true",
                        help="setup-password: read the password from a pipe and "
                             "print the bare hash, for provisioning.")
    parser.add_argument("--probe", action="store_true",
                        help="preflight: open a TLS connection to the platform and "
                             "the broker and verify their certificates. Sends no "
                             "credential and no token.")
    parser.add_argument("--seconds", type=float, default=20.0,
                        help="stream: how long to encode for. adsb: how long to "
                             "listen before dumping the contact table")
    parser.add_argument("--out", help="stream: where to write the fragmented MP4 "
                                      "(default /dev/shm/gsu-stream.mp4). "
                                      "adsb: where to write the contacts as JSON")
    parser.add_argument("--size", help="stream/camera: WxH, e.g. 1920x1080")
    parser.add_argument("--fps", type=float, help="stream: frames per second")
    parser.add_argument("--bitrate", type=int, help="stream: kbit/s target")
    parser.add_argument("--encoder", choices=["auto", "hardware", "software"],
                        help="stream: which H.264 encoder to use. Default auto, "
                             "which probes and prefers a hardware encode block. "
                             "Overrides GSU_ENCODER.")
    parser.add_argument("--frames", type=int, default=5,
                        help="camera: how many snapshots to take")
    parser.add_argument("--freq", type=float,
                        help="radio: frequency in MHz, e.g. 118.7")
    parser.add_argument("--monitor", action="store_true",
                        help="radio: hold the squelch open, so a quiet channel "
                             "still proves the audio path")
    parser.add_argument("--gain", help="radio: tuner gain in dB, or 'auto'. "
                                       "Drop to 1-15 for a close-range key-up "
                                       "test or the front end compresses and a "
                                       "flat envelope demodulates to silence.")
    parser.add_argument("--ppm", type=int, help="radio: crystal correction")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    _logging(args.verbose)

    if args.command == "setup-password":
        # Deliberately before an Agent exists: this command touches no state,
        # needs no station and must work on a laptop while an image is being
        # built, which is where the password is actually chosen.
        return _setup_password(from_stdin=args.stdin)

    config = AgentConfig.from_env()
    agent = Agent(config)

    if args.command == "run":
        return agent.run()

    if args.command == "preflight":
        return _preflight(agent, config, args.probe)

    if args.command == "devices":
        # Give each device a moment to say something before asking whether it
        # is talking: a driver that has been constructed and never read from is
        # indistinguishable from one that is silent, and reporting it as absent
        # would be the exact mistake this command exists to expose.
        for _ in range(3):
            agent.step(1.0)
            time.sleep(0.3)
        # Intent against fact, which is the whole point of the inventory.
        for report in agent.inventory.report():
            print(
                f"{report.slot:8} {report.status:18} {report.label}\n"
                f"         {'found: ' + report.detail if report.detail else ''}"
            )
            if report.absent:
                print(f"         no source for: {', '.join(report.absent)}")
        for conflict in agent.inventory.conflicts():
            print(f"CONFLICT {conflict}")
        unsourced = agent.inventory.unsourced_streams()
        if unsourced:
            print(f"\nNot published at all (no source): {', '.join(sorted(unsourced))}")
        agent.shutdown()
        return 0

    if args.command == "stream":
        if args.encoder:
            import dataclasses

            config = dataclasses.replace(config, encoder=args.encoder)
            agent.config = config
        return _stream(agent, config, args.seconds, args.out, args.size,
                       args.fps, args.bitrate)

    if args.command == "camera":
        return _camera(agent, args.frames, args.size, args.out)

    if args.command == "adsb":
        return _adsb(agent, args.seconds, args.out)

    if args.command == "radio":
        return _radio(agent, args.freq, args.seconds, args.out,
                      args.monitor, args.gain, args.ppm)

    if args.command == "bench":
        # Run this on the target hardware. The station's own cost is only part
        # of the load question, but it is the part this code is responsible for.
        return _bench(agent)

    if args.command == "enrol":
        token = args.token or config.enrol_token
        if not token:
            print("Give a code with --token or GSU_ENROL_TOKEN.", file=sys.stderr)
            return 2
        try:
            enrolment = agent.enrol(token)
        except Exception as exc:  # noqa: BLE001 - this message is the product
            print(f"Enrolment failed: {exc}", file=sys.stderr)
            return 1
        print(
            f"Enrolled as {enrolment.site.name} ({enrolment.station_id}).\n"
            f"  broker      {enrolment.broker.url} as {enrolment.broker.username}\n"
            f"  telemetry   {enrolment.broker.telemetry_topic}\n"
            f"  commands    {enrolment.broker.command_topic}\n"
            f"  expires     {enrolment.credential.expires_at.isoformat()}\n"
            f"  renew after {enrolment.credential.renew_after.isoformat()}"
        )
        return 0

    enrolment = agent.credentials.load()
    if enrolment is None:
        print("This box is not enrolled.", file=sys.stderr)
        return 1

    if args.command == "whoami":
        print(
            f"{enrolment.site.name} ({enrolment.station_id})\n"
            f"  agent       {AGENT_VERSION}\n"
            f"  broker      {config.broker_url or enrolment.broker.url} "
            f"as {enrolment.broker.username}\n"
            f"  credential  expires {enrolment.credential.expires_at.isoformat()}, "
            f"renew after {enrolment.credential.renew_after.isoformat()}"
        )
        return 0

    try:
        standing = agent.client.status(enrolment.credential.secret)
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach the platform: {exc}", file=sys.stderr)
        return 1
    print(
        f"{standing.name} ({standing.station_id})\n"
        f"  config version {standing.config_version}\n"
        f"  credential expires {standing.credential_expires_at}\n"
        f"  renew now: {standing.renew_now}\n"
        f"  platform clock {standing.server_time} (reference only)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
