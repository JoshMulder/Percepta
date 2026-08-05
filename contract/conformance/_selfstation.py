"""A minimal station, written from the contract, for `selftest.py`.

Not part of the contract and not an example to copy: it exists so the
conformance harness has something to be right and wrong about. It speaks the
2.0 relay format - one WebSocket to /broker, {"stream","payload"}, Opus audio
behind both gates, every command reported back - and `--break X` makes it
violate exactly one rule so the harness can be shown to notice.

Deliberately written from `../transport.md` and `../schemas/` rather than from
the station codebase. A harness validated against the implementation it is
meant to police proves only that the two agree.
"""
import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class Client:
    def __init__(self, host, port, credential="tok_abc123", pong=True):
        self.pong = pong
        self.sock = socket.create_connection((host, port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET /broker HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {credential}\r\n\r\n".encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        assert b"101" in buf.split(b"\r\n")[0], buf[:80]
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def send(self, obj, raw=None, fragment=False, ping_between=False):
        data = raw.encode() if raw else json.dumps(obj).encode()
        if fragment and len(data) > 4:
            half = len(data) // 2
            self._frame(data[:half], opcode=0x1, fin=False)
            if ping_between:
                # RFC 6455 §5.4 permits a control frame between the fragments
                # of a message, and a station with a keepalive sends one
                # eventually by definition. The harness used to discard the
                # fragments it had already accumulated, so the continuation
                # that followed had nothing to continue and the stream
                # desynced for the rest of the run.
                self._frame(b"mid", opcode=0x9)
            self._frame(data[half:], opcode=0x0, fin=True)
            return
        self._frame(data)

    def _frame(self, data, opcode=0x1, fin=True):
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        n = len(data)
        header = bytearray([(0x80 if fin else 0x00) | opcode])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126); header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127); header += struct.pack("!Q", n)
        self.sock.sendall(bytes(header) + mask + masked)

    def _need(self, n, deadline):
        while len(self.buf) < n:
            self.sock.settimeout(max(0.01, min(1.0, deadline - time.time())))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                if time.time() > deadline:
                    raise TimeoutError
                continue
            if not chunk:
                raise ConnectionError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def poll(self, timeout=0.0):
        """A command payload, or None when nothing more is waiting.

        Loops rather than returning on the first non-command frame. The caller
        drains with `while (cmd := poll()) is not None`, so returning None for
        a control frame ended that loop early — and once this station started
        sending pings, the platform's pongs arrived faster than the commands
        did and it stopped acting on commands almost entirely. `_need` serves
        anything already buffered without consulting the deadline, so a
        `continue` here costs nothing when the pong and the command arrived
        together.
        """
        deadline = time.time() + timeout
        while True:
            try:
                first, second = self._need(2, deadline)
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._need(2, deadline))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._need(8, deadline))[0]
                data = self._need(length, deadline) if length else b""
            except (TimeoutError, socket.timeout):
                return None
            opcode = first & 0x0F
            if opcode == 0x8:
                raise ConnectionError("platform closed")
            if opcode == 0x9:                 # ping
                # transport.md requires both ends to answer one: it is the only
                # way either side notices a half-open socket, and a station that
                # stays silent publishes into a dropped NAT mapping for ever.
                if self.pong:
                    self._frame(data, opcode=0xA)
                continue
            if opcode == 0xA:                 # pong
                continue
            try:
                frame = json.loads(data)
            except ValueError:
                continue
            # Match on `type` before `stream`: a refusal carries `stream` too
            # and no `payload`, so dispatching on `stream` alone would crash
            # here on the frame this station's own mistake provoked.
            if frame.get("type"):
                continue
            if frame.get("stream") == "c":
                return frame.get("payload")
            continue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--seconds", type=float, default=90.0)
    ap.add_argument("--break", dest="break_", default="",
                    help="ungated-audio | bad-stream | no-report | extra-key | "
                         "nan | all-unavailable | bad-stream-type | fragmented "
                         "| fragmented-ping | slow-weather | lying-cadence | "
                         "honest-empty | disconnect | transient-unavailable | "
                         "fixed-squelch | oversize | no-pong | quiet-band")
    args = ap.parse_args()

    c = Client(args.host, args.port, pong=(args.break_ != "no-pong"))
    # `fixed-squelch` opens on a hand-set threshold rather than AUTO, which is
    # the state the harness has to notice and put back: the command list ends
    # on `auto_squelch on`, so a site left as found is the whole point.
    fixed = args.break_ == "fixed-squelch"
    st = {"freq_hz": 118_700_000,
          "threshold_db": -70.0 if fixed else -87.0,
          "auto_squelch": not fixed,
          "monitor": False, "light": False, "spectrum_until": 0.0,
          "audio_until": 0.0, "squelch_open": False}

    end = time.time() + args.seconds
    t = 0.0
    while time.time() < end:
        # commands
        while (cmd := c.poll(0.0)) is not None:
            k = cmd.get("kind")
            if args.break_ in ("no-report", "transient-unavailable") \
                    and k == "radio.monitor":
                continue                      # obey nothing, report nothing
            if k == "radio.tune":
                st["freq_hz"] = int(cmd["freq_hz"])
            elif k == "radio.squelch":
                st["threshold_db"] = float(cmd["db"]); st["auto_squelch"] = False
            elif k == "radio.auto_squelch":
                st["auto_squelch"] = bool(cmd["on"])
            elif k == "radio.monitor":
                st["monitor"] = bool(cmd["on"])
            elif k == "light.set":
                st["light"] = bool(cmd["on"])
            elif k == "radio.spectrum":
                st["spectrum_until"] = (time.time() + cmd.get("lease_seconds", 15)
                                        if cmd.get("on", True) else 0.0)
            elif k == "radio.audio":
                st["audio_until"] = time.time() + cmd.get("lease_seconds", 30)
                # Section 6 opens by asking for a 5 s lease, and it is the only
                # thing that does — every other audio request in the run is the
                # 30 s one. Hanging the disconnect off that rather than off a
                # wall-clock guess is what makes this case land: the harness
                # finishes in about thirty seconds when the station sends
                # health at t=0, so a fixed t>=50 closed the socket long after
                # the run had ended and tested nothing.
                if args.break_ == "disconnect" and cmd.get("lease_seconds") == 5:
                    st["quit_at"] = time.time() + 2.0

        # a transmission every 6s, 2s long
        # `quiet-band` never opens the gate. Not a fault — airband is silent
        # most of the time and a station with nothing to send is behaving
        # correctly — which is exactly why the harness must return
        # INCONCLUSIVE rather than certify a run that proved nothing about
        # the two most expensive rules in the contract.
        quiet = args.break_ == "quiet-band"
        st["squelch_open"] = (not quiet) and ((t % 6.0) < 2.0 or st["monitor"])
        leased = time.time() < st["audio_until"]

        radio = {"kind": "radio", "freq_hz": st["freq_hz"], "rssi_db": -60.0,
                 "noise_floor_db": -95.0, "threshold_db": st["threshold_db"],
                 "squelch_open": st["squelch_open"],
                 "auto_squelch": st["auto_squelch"], "monitor": st["monitor"],
                 "tx_capable": False,
                 "audio_lease_remaining_s": round(max(0.0, st["audio_until"] - time.time()), 1)}
        if time.time() < st["spectrum_until"]:
            radio["spectrum"] = [-95] * 128
            radio["span_hz"] = 120_000
        frames = [
            ("t", {"kind": "adsb", "aircraft": [
                {"icao": "C81234", "range_km": 12.5, "bearing_deg": 231.0,
                 "altitude_m": 3000, "speed_kt": 250, "track_deg": 180.0}]}),
            ("t", {"kind": "power", "soc_pct": 88.0, "battery_v": 13.1}),
            ("t", radio),
            ("t", {"kind": "light", "on": st["light"]}),
        ]
        # Only `adsb` goes quiet, and only `adsb` gets the absurd cadence. The
        # earlier version silenced `light` too, so both `light.set` checks
        # failed and the run exited 1 on those — never reaching the verdict
        # branch this case exists to test. It scored green with the whole
        # skipped-streams block deleted, which is to say it tested nothing.
        lying = args.break_ == "lying-cadence"
        if lying:
            frames = [f for f in frames if f[1].get("kind") != "adsb"]

        # Weather only after 35 s, with health declaring that cadence.
        # Believing the station extends the window and finds it; not believing
        # it fails a metered site the contract explicitly protects. Publishing
        # at t=0 — as this once did — meant the declared cadence was never
        # consulted and the case could only ever pass.
        slow = args.break_ == "slow-weather"
        due = (int(t) >= 35 and int(t) % 35 == 0) if slow else int(t) % 5 == 0
        if due:
            frames.append(("t", {"kind": "weather", "wind_kt": 10.0,
                                 "gust_kt": 14.0, "wind_dir_deg": 270.0,
                                 "temperature_c": 11.5}))
        if int(t) % 30 == 0:
            cadence = {"adsb": 86400.0} if lying else {
                "adsb": 1.0, "weather": 35.0 if slow else 5.0}
            frames.append(("t", {"kind": "health", "contract_version": "2.1",
                                 "status": "ok", "uptime_s": round(t, 1),
                                 "cadence": cadence}))
        # audio: both gates, unless told to misbehave
        gate = st["squelch_open"] and (leased or args.break_ == "ungated-audio")
        if gate:
            frames.append(("a", {"kind": "audio", "codec": "opus", "rate": 24000,
                                 "channels": 1, "frame_ms": 20,
                                 "packets": ["AAAA"] * 8}))
        if args.break_ == "bad-stream" and int(t) == 3:
            frames.append(("z", {"kind": "adsb", "aircraft": []}))
        if args.break_ == "oversize" and int(t) == 3:
            # Over the 512 KiB cap, which the platform answers by closing
            # 1009. The harness used to traceback on the next read instead,
            # skipping the block that puts the receiver back.
            frames.append(("t", {"kind": "adsb", "aircraft": [],
                                 "filler": "x" * (600 * 1024)}))
        if args.break_ == "transient-unavailable" and int(t) == 1:
            # One frame saying the tuner is warming up, then a working radio
            # for the rest of the run — the ordinary case, not an attack.
            # Latching on it skipped every radio command, both audio gates
            # included, while the run still exited 0. This station also
            # withholds its `radio.monitor` report, so a harness that tests
            # the radio catches it and one that skips it certifies it.
            frames = [("t", {"kind": "radio", "available": False,
                             "unavailable_reason": "tuner warming up"})]
        if args.break_ in ("all-unavailable", "honest-empty"):
            frames = [("t", {"kind": k, "available": False,
                             "unavailable_reason": " " if args.break_ == "all-unavailable" else "no receiver fitted"})
                      for k in ("adsb", "power", "radio", "light", "weather")]
        if args.break_ == "bad-stream-type":
            frames = [({}, {"kind": "adsb", "aircraft": []})]
        for stream, payload in frames:
            frame = {"stream": stream, "payload": payload}
            if args.break_ == "extra-key":
                frame["topic"] = "gsu/legacy/telemetry"
            if args.break_ == "nan" and payload.get("kind") == "power":
                c.send(None, raw='{"stream":"t","payload":{"kind":"power",'
                                 '"soc_pct":NaN,"battery_v":NaN}}')
            else:
                c.send(frame,
                       fragment=args.break_ in ("fragmented", "fragmented-ping"),
                       ping_between=args.break_ == "fragmented-ping")
        if st.get("quit_at") and time.time() >= st["quit_at"]:
            # Every command answered correctly, then gone, two seconds into the
            # lease check. Section 6 reported "audio stops when the lease
            # lapses — PASS" on the evidence of a closed socket, and the run
            # certified a site in the same breath as noting it was left
            # mistuned.
            c.sock.close()
            return
        time.sleep(1.0)
        t += 1.0


if __name__ == "__main__":
    try:
        main()
    except (ConnectionError, BrokenPipeError):
        print("platform closed the socket", file=sys.stderr)
