"""Carrying live video to the platform: fragmented MP4 over a WebSocket.

Two files, both written out rather than pulled in, for the same reason the rest
of this station is: `requirements.txt` is one dependency on purpose, because an
unattended box in the field should boot with what is in its image and never need
to install anything — and because an ARMv7 wheel that has to compile is a
problem discovered on a hillside.

    fmp4.py       H.264 access units → an init segment and one fragment per
                  frame. The station muxes; the platform relays bytes.
    websocket.py  RFC 6455, client side, over the station's pinned TLS.

The muxer is deliberately station-side rather than left to the encoder. It makes
the hardware path, the software path and the synthetic source produce byte-
identical container output from the same code, and it means the Pi is not
relying on whichever muxer flags its build of rpicam-apps happens to support —
which is not something to discover remotely.
"""
