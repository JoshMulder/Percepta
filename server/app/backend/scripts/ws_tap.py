"""Listen as a console does, on the console's own socket.

**A diagnostic, not a feature.** It is the last cut in a search that has
already eliminated three suspects.

Airband audio that chops in the console has four places it can be lost. The
station's `/audio.wav` takes the demodulator's PCM before any of them and is
clean. `audio_tap` decodes what the station actually published, after the Opus
encoder and after the broker relay, and is clean. That leaves this platform's
fan-out to the console websocket, and the browser's player — and this tool sits
exactly between them:

    clean here   -> the fault is in the browser: the decoder, the worklet, or
                    the scheduling in useAudio.ts
    chopped here -> the fault is the fan-out, and the first thing to look at is
                    `Connection.enqueue`, which drops the OLDEST frame when its
                    queue is full

It connects the way the console connects — log in, select a station, subscribe
to `audio` — and reports what arrives: the interval between frames, the packets
in each, and the ratio of audio delivered to wall clock. That last number is
the one that caught the station generating audio at 0.82 of real time, and it
means the same thing here.

With `--wav` it also decodes to a playable stream, so the last hop before the
browser can be listened to rather than inferred:

    ssh percepta@<platform> 'cd ~/percepta/server && docker compose exec -T \
        app python -m backend.scripts.ws_tap --wav' < percepta.txt | ffplay -

CREDENTIALS ARE READ FROM STDIN by default — email on one line, password on the
next, which is the layout of the developer credentials file. Never put them in
an argument: a command line is visible in `ps`, in shell history, and in the
logs of whatever ran it. `--creds PATH` reads a file instead, for a box where
that is more convenient than a pipe.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import ssl
import sys
import time

import websockets

from backend.scripts.audio_tap import _Decoder, wav_header


def _read_credentials(source: str) -> tuple[str, str]:
    """Email and password, from stdin or a file.

    The first two non-empty lines, so the developer credentials file can be
    piped in whole without being edited down first.
    """
    if source == "-":
        text = sys.stdin.read()
    else:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise SystemExit(
            "expected an email on one line and a password on the next")
    return lines[0], lines[1]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="https://127.0.0.1:8000",
                        help="platform base URL, as this container sees it")
    parser.add_argument("--creds", default="-",
                        help="file with email and password, or - for stdin")
    parser.add_argument("--station", default="",
                        help="station id; defaults to the first visible one")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--wav", action="store_true",
                        help="decode to a WAV stream on stdout as well")
    parser.add_argument("--insecure", action="store_true", default=True,
                        help="accept the platform's own certificate")
    args = parser.parse_args()

    email, password = _read_credentials(args.creds)

    # Log in over HTTP first: the socket takes the same session, either as the
    # cookie a browser would send or as a bearer token, and a bearer is easier
    # to carry here than a cookie jar.
    import urllib.request

    context = ssl.create_default_context()
    if args.insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    body = json.dumps({"email": email, "password": password}).encode()
    request = urllib.request.Request(
        f"{args.base}/api/auth/login", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, context=context, timeout=10) as reply:
        payload = json.loads(reply.read())
        cookies = reply.headers.get_all("Set-Cookie") or []
    if payload.get("type") == "challenge" or "user_id" not in payload:
        raise SystemExit(f"login did not complete: {payload}")

    # The access cookie, taken as sent. Parsed rather than reconstructed so
    # this keeps working if the cookie's name or attributes change.
    cookie = "; ".join(c.split(";", 1)[0] for c in cookies)
    if not cookie:
        raise SystemExit("login returned no cookie; cannot open the socket")

    url = args.base.replace("https://", "wss://").replace("http://", "ws://")
    started = time.monotonic()
    frames = packets = 0
    audio_s = 0.0
    gaps: list[float] = []
    last: float | None = None
    decoder: _Decoder | None = None
    out = sys.stdout.buffer if args.wav else None

    async with websockets.connect(
        f"{url}/ws", additional_headers={"Cookie": cookie},
        ssl=context if url.startswith("wss") else None,
        max_size=None,
    ) as socket:
        hello = json.loads(await socket.recv())
        station = args.station or (hello.get("stations") or [None])[0]
        if not station:
            raise SystemExit("this account can see no stations")
        print(f"connected as {email}; station {station}", file=sys.stderr)

        await socket.send(json.dumps(
            {"type": "select_station", "ground_station_id": station}))
        await socket.send(json.dumps({"type": "subscribe", "stream": "audio"}))

        deadline = started + args.seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(
                    socket.recv(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            message = json.loads(raw)
            if message.get("type") == "error":
                print(f"server said: {message}", file=sys.stderr)
                continue
            if message.get("type") != "event" or message.get("stream") != "audio":
                continue

            now = time.monotonic()
            if last is not None:
                gaps.append(now - last)
            last = now
            frames += 1

            payload = message.get("payload") or {}
            rate = int(payload.get("rate") or 24000)
            channels = int(payload.get("channels") or 1)
            encoded = payload.get("packets") or []
            packets += len(encoded)
            audio_s += len(encoded) * (payload.get("frame_ms", 20) / 1000.0)

            if out is not None:
                if decoder is None:
                    decoder = _Decoder(rate, channels)
                    out.write(wav_header(rate, channels))
                for packet in encoded:
                    out.write(decoder.decode(base64.b64decode(packet)))
                out.flush()

    if decoder is not None:
        decoder.close()

    elapsed = time.monotonic() - started
    print(f"\nframes {frames}, packets {packets}", file=sys.stderr)
    print(f"audio delivered {audio_s:.2f}s in {elapsed:.2f}s of wall clock "
          f"(ratio {audio_s / elapsed if elapsed else 0:.4f})", file=sys.stderr)
    if gaps:
        ordered = sorted(gaps)
        print("inter-frame ms: med %d  p90 %d  max %d" % (
            ordered[len(ordered) // 2] * 1000,
            ordered[int(len(ordered) * 0.9)] * 1000,
            ordered[-1] * 1000), file=sys.stderr)
        # A frame every 125 ms is the station's cadence. Anything past a
        # quarter second is a stall the player has to absorb, and past the
        # console's 300 ms lead it is a gap the operator hears.
        late = [round(g * 1000) for g in gaps if g > 0.25]
        print(f"intervals over 250ms: {late[:25]}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
