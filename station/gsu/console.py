"""The local console: one app for setting a box up and seeing what it is doing.

`contract/enrolment.md` §5 wants a page the box serves on its own network where
a technician enters a code and watches for a green light. §7 wants the device
inventory — which sensors are present and how to reach them. Those are the same
job five minutes apart, done by the same person standing in front of the same
box, so they are one app.

Three rules it is built to:

**It works with the link down.** Everything it shows is local state, and the
device selection, the parameters and the events all come off the box's own disk.
The moment someone is most likely to be standing in front of it is the moment
the platform is unreachable.

**Configured and detected are shown separately, always.** "An Airmar 110WX
should be on /dev/ttyUSB0" and "there is one there" are different facts, and the
UI never merges them into a tick. A camera that has failed and a camera that was
never fitted look identical in a database and completely different at the site.

**It says what has no source.** If a device cannot provide a field the console
renders — rainfall on an instrument with no rain gauge — that is listed at
selection time, not discovered later by an operator reading 0.0 mm during a
downpour.

No authentication, deliberately: physical presence on the setup network is the
control, which is only true if this is not on a routable network. Where it
belongs depends on the compute platform — an open decision, see DECISIONS.md.
"""

from __future__ import annotations

import html
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from .devices import registry

log = logging.getLogger("gsu.console")

STYLE = """
 body { font: 15px/1.5 system-ui, sans-serif; margin: 0; background: #10151b; color: #dfe6ee; }
 main { max-width: 54rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
 h1 { font-size: 1.35rem; margin: 0 0 .2rem; }
 h2 { font-size: 1rem; margin: 2rem 0 .6rem; color: #9db0c4; text-transform: uppercase;
      letter-spacing: .08em; }
 .sub { color: #8fa0b3; margin: 0 0 1.2rem; }
 .card { background: #18202a; border: 1px solid #253040; border-radius: 8px;
         padding: 1rem 1.1rem; margin-bottom: .9rem; }
 .row { display: flex; justify-content: space-between; gap: 1rem; padding: .3rem 0;
        border-bottom: 1px solid #1f2833; }
 .row:last-child { border-bottom: 0; }
 .k { color: #8fa0b3; }
 .ok { color: #5fd08a; } .warn { color: #f0c674; } .bad { color: #f08a7a; }
 .muted { color: #7d8ea1; font-size: .88rem; }
 input[type=text], input[type=password], input[type=number], select {
   font: .95rem system-ui, sans-serif; padding: .45rem .55rem; background: #0d1218;
   color: #dfe6ee; border: 1px solid #2c3a4c; border-radius: 5px; min-width: 12rem; }
 input.code { font: 1.2rem ui-monospace, monospace; letter-spacing: .12em; width: 100%;
   box-sizing: border-box; text-transform: uppercase; }
 button { margin-top: .7rem; font-size: .95rem; padding: .5rem 1rem; border-radius: 6px;
   border: 0; background: #2f6feb; color: white; cursor: pointer; }
 .msg { padding: .7rem .9rem; border-radius: 6px; margin-bottom: 1rem; }
 .msg.bad { background: #3a1f1c; color: #f5b3a7; }
 .msg.good { background: #14301f; color: #9fe3b8; }
 .pill { font-size: .78rem; padding: .1rem .5rem; border-radius: 999px; border: 1px solid; }
 .pill.ok { border-color: #2c6b47; background: #13291d; }
 .pill.warn { border-color: #6b5a2c; background: #292213; }
 .pill.bad { border-color: #6b3129; background: #291714; }
 .pill.off { border-color: #33404f; background: #1b232d; color: #8fa0b3; }
 .field { display: flex; flex-wrap: wrap; gap: .5rem 1rem; align-items: center;
          margin: .5rem 0; }
 label { color: #a9b8c8; font-size: .9rem; }
 ul { margin: .4rem 0 0; padding-left: 1.1rem; color: #b9c6d4; }
 li { padding: .1rem 0; }
 code { color: #9fb4cc; }
 .slot-head { display: flex; justify-content: space-between; align-items: baseline;
              gap: 1rem; }
"""

STATUS_PILL = {
    "present": ("ok", "detected"),
    "stalled": ("warn", "configured, gone quiet"),
    "configured_absent": ("bad", "configured, not detected"),
    "not_fitted": ("off", "not fitted"),
}


class Console:
    def __init__(self, agent, host: str = "127.0.0.1", port: int = 8088) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.message: tuple[str, str] | None = None

    # --- lifecycle ------------------------------------------------------

    def start(self) -> None:
        console = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: A002
                pass

            def do_GET(self):  # noqa: N802
                if self.path.startswith("/status.json"):
                    return console._send_json(self, console.agent.snapshot())
                if self.path.startswith("/registry.json"):
                    return console._send_json(self, console._registry_json())
                return console._send_html(self, console.render())

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                form = parse_qs(self.rfile.read(length).decode())
                try:
                    if self.path.startswith("/device"):
                        console._set_device(form)
                    else:
                        console._enrol(form)
                except Exception as exc:  # noqa: BLE001 - shown to a person
                    console.message = ("bad", str(exc))
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.end_headers()

        try:
            self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        except OSError as exc:
            # A console that cannot bind must not stop the station working.
            log.warning("Console could not start on %s:%s (%s).", self.host, self.port, exc)
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="gsu-console", daemon=True
        )
        self._thread.start()
        log.info("Local console at http://%s:%s", self.host, self.port)

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # --- actions --------------------------------------------------------

    def _enrol(self, form: dict) -> None:
        token = (form.get("token") or [""])[0].strip()
        if not token:
            self.message = ("bad", "Enter the code from the platform.")
            return
        enrolment = self.agent.enrol(token)
        self.message = (
            "good",
            f"Enrolled as {enrolment.site.name}. Telemetry is on its way.",
        )

    def _set_device(self, form: dict) -> None:
        slot = (form.get("slot") or [""])[0]
        type_id = (form.get("type_id") or [""])[0]
        resource = (form.get("resource") or [""])[0] or None
        device = registry.get(type_id) if type_id else None
        params: dict = {}
        if device is not None:
            for parameter in device.parameters:
                raw = (form.get(f"p_{parameter.name}") or [""])[0]
                if parameter.type == "bool":
                    params[parameter.name] = raw == "on"
                elif parameter.type == "number" and raw != "":
                    params[parameter.name] = float(raw) if "." in raw else int(raw)
                elif raw != "":
                    params[parameter.name] = raw
        self.agent.inventory.set_device(slot, type_id, params, resource)
        # Rebuild immediately: an installer who changes a port expects to see
        # within seconds whether the box can now talk to the thing.
        self.agent.build_devices()
        report = {r.slot: r for r in self.agent.inventory.report()}[slot]
        if not type_id:
            self.message = ("good", f"{slot}: nothing fitted.")
        elif report.status == "present":
            self.message = ("good", f"{slot}: {report.label} — detected.")
        else:
            self.message = (
                "bad", f"{slot}: {report.label} saved, but not detected. {report.detail}",
            )

    # --- rendering ------------------------------------------------------

    def _registry_json(self) -> dict:
        return {
            "slots": list(registry.SLOTS),
            "devices": [
                {
                    "id": device.id, "slot": device.slot, "label": device.label,
                    "connection": device.connection, "simulated": device.simulated,
                    "driver": device.driver, "resource": device.resource,
                    "provides": list(device.provides), "absent": list(device.absent),
                    "notes": device.notes,
                    "parameters": [
                        {
                            "name": p.name, "label": p.label, "type": p.type,
                            "default": p.default, "required": p.required,
                            "help": p.help, "choices": list(p.choices),
                        }
                        for p in device.parameters
                    ],
                }
                for device in registry.REGISTRY
            ],
        }

    def _send_json(self, handler, payload: dict) -> None:
        body = json.dumps(payload, indent=2, default=str).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _send_html(self, handler, text: str) -> None:
        body = text.encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def render(self) -> str:
        state = self.agent.snapshot()
        out = [
            "<!doctype html><meta charset=utf-8>",
            "<meta name=viewport content='width=device-width,initial-scale=1'>",
            "<title>Ground station</title>",
            f"<style>{STYLE}</style>",
            "<main>",
            "<h1>Ground station</h1>",
        ]
        if self.message:
            kind, text = self.message
            out.append(f"<div class='msg {kind}'>{html.escape(text)}</div>")
            self.message = None

        out.append(self._section_setup(state))
        out.append(self._section_devices(state))
        out.append(self._section_gaps(state))
        out.append(self._section_events(state))
        out.append("</main>")
        return "".join(out)

    def _section_setup(self, state: dict) -> str:
        out = []
        if state["enrolled"]:
            out.append(f"<p class=sub>{html.escape(state['station'] or '')}</p>")
        else:
            out.append("<p class=sub>Not set up yet.</p>")
            out.append(
                "<div class=card><form method=post action='/enrol'>"
                "<label for=token>Enter the code you were given</label><br>"
                "<input id=token class=code name=token type=text autocomplete=off "
                "placeholder='XXXX-XXXX-XXXX' autofocus>"
                "<button type=submit>Set this station up</button>"
                "</form></div>"
            )
        security = state.get("security") or {}
        trust = security.get("trust") or {}
        clock_state = state.get("clock_source") or {}
        rows = [
            ("Platform", state["platform"], "ok"),
            ("Enrolled", "yes" if state["enrolled"] else "not yet",
             "ok" if state["enrolled"] else "warn"),
            ("Link to the platform", "up" if state["link"] else "down",
             "ok" if state["link"] else "bad"),
            # Whether the uplink is encrypted and verified, in the same list as
            # everything else a technician checks before leaving site. It is
            # not a question that should need a packet capture to answer.
            self._security_row(security, trust),
            ("Telemetry sent", f"{state['published']} frames", "ok"),
            ("Dropped while offline", f"{state['dropped']} frames",
             "ok" if not state["dropped"] else "warn"),
            ("Station clock", state["clock"], "ok"),
            ("Clock kept by", self._clock_wording(clock_state),
             "ok" if clock_state.get("synchronised") else "warn"),
        ]
        out.append("<div class=card>")
        for label, value, css in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class='{css}'>{html.escape(str(value))}</span></div>"
            )
        out.append("</div>")
        if state["health"]:
            out.append("<div class=card><div class=k>Needs attention</div><ul>")
            for condition in state["health"]:
                css = "bad" if condition["severity"] == "critical" else "warn"
                out.append(
                    f"<li class={css}>{html.escape(condition['id'])}: "
                    f"{html.escape(condition['detail'])}</li>"
                )
            out.append("</ul></div>")
        return "".join(out)

    @staticmethod
    def _security_row(security: dict, trust: dict) -> tuple[str, str, str]:
        """One line answering "is this link safe to leave running".

        Deliberately blunt in the failure cases. A station that has stopped
        publishing because it will not accept a certificate looks, from every
        other row on this page, exactly like a station with no signal — and the
        two need completely different people called.
        """
        if security.get("tls_failed"):
            return ("Uplink security",
                    "REFUSED — the broker's certificate did not verify", "bad")
        if not security.get("publishing") and security.get("broker_url"):
            return ("Uplink security", "REFUSED — see the conditions below", "bad")
        if security.get("broker_tls") is None:
            return ("Uplink security", "no broker yet", "warn")
        if not security.get("broker_tls"):
            return ("Uplink security", "PLAINTEXT — development only", "bad")
        if trust.get("mode") == "system":
            return ("Uplink security", "TLS, system CA bundle (not pinned)", "warn")
        fingerprint = (trust.get("fingerprint") or "")[:23]
        return ("Uplink security", f"TLS, CA pinned {fingerprint}…", "ok")

    @staticmethod
    def _clock_wording(state: dict) -> str:
        source = state.get("source", "unknown")
        wording = {
            "gps": "GPS", "ntp": "NTP", "rtc-only": "a hardware RTC, not synced",
            "none": "nothing — the time is a guess",
            "unknown": "cannot tell",
        }.get(source, source)
        return wording if state.get("rtc_present") else f"{wording} (no RTC fitted)"

    def _section_devices(self, state: dict) -> str:
        out = ["<h2>What is fitted</h2>"]
        if state["conflicts"]:
            out.append("<div class='msg bad'><ul>")
            for conflict in state["conflicts"]:
                out.append(f"<li>{html.escape(conflict)}</li>")
            out.append("</ul></div>")

        resources = state["resources"]
        by_slot = {report["slot"]: report for report in state["devices"]}
        fitted = self.agent.inventory.fitted

        for slot in registry.SLOTS:
            report = by_slot[slot]
            entry = fitted.get(slot)
            css, wording = STATUS_PILL.get(report["status"], ("off", report["status"]))
            out.append("<div class=card>")
            out.append(
                f"<div class=slot-head><strong>{html.escape(slot)}</strong>"
                f"<span class='pill {css}'>{html.escape(wording)}</span></div>"
            )
            # Intent and fact, on separate lines, always both.
            out.append(
                f"<div class=muted>selected: {html.escape(report['label'])}</div>"
            )
            if report["detail"]:
                out.append(f"<div class=muted>found: {html.escape(report['detail'])}</div>")
            elif report["configured"]:
                out.append("<div class=muted>found: nothing reported yet</div>")

            out.append(f"<form method=post action='/device'><input type=hidden name=slot value='{slot}'>")
            out.append("<div class=field><label>Device</label><select name=type_id>")
            out.append(
                f"<option value=''{' selected' if not report['configured'] else ''}>"
                "— not fitted —</option>"
            )
            for device in registry.by_slot(slot):
                selected = " selected" if entry and entry.type_id == device.id else ""
                suffix = "" if device.driver else "  (no driver in this build)"
                out.append(
                    f"<option value='{device.id}'{selected}>"
                    f"{html.escape(device.label)}{suffix}</option>"
                )
            out.append("</select></div>")

            selected_device = registry.get(entry.type_id) if entry and entry.type_id else None
            if selected_device is not None:
                for parameter in selected_device.parameters:
                    value = (entry.params or {}).get(parameter.name, parameter.default)
                    out.append("<div class=field>")
                    out.append(
                        f"<label for='p_{parameter.name}'>{html.escape(parameter.label)}</label>"
                    )
                    name = f"p_{parameter.name}"
                    if parameter.type == "bool":
                        checked = " checked" if value else ""
                        out.append(
                            f"<input type=checkbox id='{name}' name='{name}'{checked}>"
                        )
                    elif parameter.type == "select":
                        out.append(f"<select id='{name}' name='{name}'>")
                        for choice in parameter.choices:
                            sel = " selected" if str(value) == str(choice) else ""
                            out.append(f"<option{sel}>{html.escape(str(choice))}</option>")
                        out.append("</select>")
                    elif parameter.name == "port":
                        # The single most likely thing to be got wrong on a
                        # first install, so the ports that exist right now are
                        # offered rather than described. A free-text field is
                        # kept underneath it: the device may not be plugged in
                        # yet, and refusing to save a port that is currently
                        # absent would be worse than saving one that is.
                        out.append(
                            f"<input type=text id='{name}' name='{name}' "
                            f"list='ports-{slot}' value='{html.escape(str(value))}' "
                            "placeholder='/dev/serial/by-id/…'>"
                        )
                        out.append(f"<datalist id='ports-{slot}'>")
                        for port in state.get("serial_ports") or []:
                            out.append(
                                f"<option value='{html.escape(port['id'])}'>"
                                f"{html.escape(port['detail'] or port['model'])}</option>"
                            )
                        out.append("</datalist>")
                    else:
                        field_type = {
                            "password": "password", "number": "number",
                        }.get(parameter.type, "text")
                        out.append(
                            f"<input type={field_type} id='{name}' name='{name}' "
                            f"value='{html.escape(str(value))}'>"
                        )
                    if parameter.help:
                        out.append(f"<span class=muted>{html.escape(parameter.help)}</span>")
                    out.append("</div>")

                if selected_device.resource:
                    out.append("<div class=field><label>Receiver</label><select name=resource>")
                    out.append("<option value=''>— none assigned —</option>")
                    for resource in resources:
                        sel = " selected" if entry and entry.resource == resource["id"] else ""
                        label = f"{resource['model']} serial {resource['serial'] or 'unset'}"
                        out.append(
                            f"<option value='{html.escape(resource['id'])}'{sel}>"
                            f"{html.escape(label)}</option>"
                        )
                    out.append("</select>")
                    out.append(
                        "<span class=muted>One tuner serves one band. Assigned by "
                        "serial number, because USB order changes between boots.</span>"
                    )
                    out.append("</div>")

                if selected_device.absent:
                    out.append(
                        "<div class=muted>No source on this device for: "
                        + html.escape(", ".join(selected_device.absent))
                        + " — those fields are published as absent, never as zero.</div>"
                    )
                if selected_device.notes:
                    out.append(f"<div class=muted>{html.escape(selected_device.notes)}</div>")
            out.append("<button type=submit>Save</button></form>")
            out.append("</div>")

        if not resources:
            out.append(
                "<div class=card><div class=muted>No SDR receivers detected on the "
                "USB bus. A dongle with no serial programmed cannot be told apart "
                "from an identical one — program it with rtl_eeprom before fitting "
                "a second.</div></div>"
            )

        # What is actually plugged in, listed once. On a box with two USB-UARTs
        # this is the page a technician reads to work out which is which.
        ports = state.get("serial_ports") or []
        out.append("<div class=card><div class=k>Serial ports present now</div><ul>")
        if not ports:
            out.append(
                "<li class=warn>None. Neither USB-UART is enumerating — check "
                "the leads, then <code>dmesg | tail</code>.</li>"
            )
        for port in ports:
            out.append(
                f"<li><code>{html.escape(port['id'])}</code>"
                + (f" <span class=muted>→ {html.escape(port['detail'])}</span>"
                   if port["detail"] else "")
                + "</li>"
            )
        out.append(
            "</ul><div class=muted>Use the <code>/dev/serial/by-id/…</code> "
            "names. They come from the adapter's own identity; ttyUSB numbering "
            "changes between boots and two adapters will swap over.</div></div>"
        )
        return "".join(out)

    def _section_gaps(self, state: dict) -> str:
        streams = state["unsourced_streams"]
        fields = state["unsourced_fields"]
        if not streams and not fields:
            return ""
        out = ["<h2>What the console will have no data for</h2><div class=card><ul>"]
        for stream in streams:
            out.append(
                f"<li class=warn><strong>{html.escape(stream)}</strong> — no working "
                "device. Nothing is published for it at all, so the platform can show "
                "'no receiver' rather than an empty panel.</li>"
            )
        for kind, missing in sorted(fields.items()):
            out.append(
                f"<li class=warn><strong>{html.escape(kind)}</strong> — no sensor for "
                f"{html.escape(', '.join(missing))}. Published as absent, not zero.</li>"
            )
        out.append("</ul></div>")
        return "".join(out)

    def _section_events(self, state: dict) -> str:
        out = ["<h2>Recent events (kept on the box)</h2><div class=card><ul>"]
        for event in state["events"] or []:
            out.append(
                f"<li><code>{html.escape(event['at'][11:19])}</code> "
                f"{html.escape(event['kind'])} — {html.escape(event['detail'])}</li>"
            )
        if not state["events"]:
            out.append("<li>nothing yet</li>")
        out.append("</ul>")
        storage = state["storage"]
        out.append(
            f"<div class=muted>{storage['recordings']} audio recording(s), "
            f"{storage['recordings_mb']} MB; {storage['events']} events stored, "
            f"{storage['events_pending']} not yet sent to the platform.</div>"
        )
        out.append("</div>")
        return "".join(out)
