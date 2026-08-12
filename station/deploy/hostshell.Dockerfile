# The host-shell helper image (the second half of the "platform admin reaches a
# station" feature). Tiny and stable: a Python runtime, `nsenter` from
# util-linux, and two files — the bridge and the agent's own WebSocket client,
# copied verbatim so this stays a handful of files with no pip dependency.
#
# Built locally and trusted, like the updater. It is the most dangerous container
# in the station — CAP_SYS_ADMIN and `pid: host` so it can `nsenter` a shell onto
# the host — so it holds NOTHING else: no docker socket, no state, no network
# service. It
# only dials OUT to the platform, and only while the agent has written it a live
# request (deploy/hostshell/bridge.py). It lives behind the `hostshell` compose
# profile, so a box that has not opted in never builds or runs it at all.
FROM python:3.12-alpine

# nsenter, to enter the host's namespaces from PID 1. Nothing else.
RUN apk add --no-cache util-linux

# The bridge, and the agent's WebSocket client beside it as an importable module.
# `gsu/media/websocket.py` is stdlib-only with no relative imports, so it runs
# standalone here exactly as it does in the agent.
COPY deploy/hostshell/bridge.py /usr/local/bin/hostshell-bridge.py
COPY gsu/media/websocket.py /usr/local/bin/websocket.py
ENV PYTHONPATH=/usr/local/bin
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python3", "/usr/local/bin/hostshell-bridge.py"]
