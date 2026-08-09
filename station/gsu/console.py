"""The local setup GUI: four small pages for setting a box up and seeing
what it is.

`contract/enrolment.md` §5 wants a page the box serves on its own network where
a technician enters a code and watches for a green light. §7 wants the device
inventory — which sensors are present and how to reach them. Those are the same
job five minutes apart, done by the same person standing in front of the same
box, so they are one app — split across four pages (Summary, Connection,
Devices, Logging) because on a phone in a paddock one long page buries the
answer under everything that is fine. Every page sits behind the same gate:
the guards in `_handle` run before the router looks at the path, so a new page
can never be an unauthenticated one.

The owner's requirement is that this is enough on its own: a station that comes
up unconfigured must be usable by somebody with a laptop or a phone and **no
terminal**. Enter the code, pick what is fitted in each slot, read back what the
box thinks it is. `python -m gsu` still does all of it for anyone who does have
a terminal, and neither path is the special case.

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

**The platform address is not a field here.** There is one platform, its address
is fixed in the environment file, and an installer's job is to confirm the box
is pointed at the right one — not to retype it. It is rendered read-only for
exactly that reason: a typo in a URL somebody can edit at 3pm on a roof is a
station that enrols against nothing and reports no error anybody sees.

Who may reach this page, and for how long, is `setup_access.py` and the
reasoning is all in that module's docstring. What this file owes it:

- every response carries `Cache-Control: no-store` and a CSP that permits no
  frame, no off-box form target and no script beyond the one inline block a
  response may carry under a per-response nonce. Those scripts are progressive
  enhancement — a refreshing datastream field, the camera preview's re-fetch on
  Devices, and Escape-to-close on Connection — with one deliberate exception:
  configuring a device on the Devices tab commits over a fetch and has no Save
  button behind it, so that single control needs the script, and a `<noscript>`
  says so. Everything else keeps working with the scripts blocked or absent —
  the two overlays are the proof: the preview's click-to-expand is a checkbox
  and the location editor is a `:target` dialog, neither of which is script
- every state-changing POST carries a CSRF token bound to the session cookie
- the `Host` header must be an IP literal, `localhost` or a `.local` name, which
  is what stops a public web page rebinding its own name to this station's
  private address and driving this form from a technician's browser
- request bodies are bounded before they are read: this box has 1 GB of RAM and
  `Content-Length` is attacker-controlled
- no secret is ever rendered back into the HTML. A stored camera password is
  shown as the fact that one is stored, never as its value
"""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import queue
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .camera.rtsp import split_credentials
from .config import parse_elevation_m
from .devices import registry
from .radio.audio import AUDIO_RATE
from .radio.receiver import FREQ_MAX_HZ, FREQ_MIN_HZ
from .setup_access import COOKIE_NAME, Gate, is_loopback_host

log = logging.getLogger("gsu.console")


#: How long the setup page waits for a newly-saved device to report itself.
#:
#: A receiver's presence is only knowable after the sensing loop has polled it,
#: and that loop runs at 1 Hz — so a verdict taken at save time is always taken
#: before the answer exists. Two seconds covers a tick with room, and the wait
#: ends early the moment the slot reports present, so a genuinely absent device
#: still says so almost immediately.
DETECT_GRACE_S = 2.0

#: A setup form is a few hundred bytes. This is three orders of magnitude of
#: headroom and still small enough that a hostile `Content-Length` cannot make
#: a 1 GB box swap. Read in bounded chunks rather than trusting the header.
MAX_BODY_BYTES = 64 * 1024

#: How often the window is re-checked when nobody is asking. Short enough that
#: "it closes after thirty minutes" is true to the minute, long enough to be
#: free on a Pi 2B.
WATCH_SECONDS = 5.0

#: No frames, no off-box form target, no external anything. Script is allowed
#: only as the one inline block a page carries, keyed by a nonce generated per
#: response (`_headers`) — a stored-XSS payload cannot know it, and no other
#: script source is ever valid. `connect-src 'self'` is what lets the Devices
#: script poll status.json; it permits nothing off-box.
def _wav_header(rate: int, channels: int = 1, bits: int = 16) -> bytes:
    """A RIFF header for a stream whose length nobody knows yet.

    The two size fields are 0xFFFFFFFF rather than a real count. A live capture
    has no end until the client stops reading, and every player treats an
    over-long size as "keep going" — which is exactly the behaviour wanted and
    is how every other endless-WAV endpoint does it.
    """
    block = channels * bits // 8
    return b"".join((
        b"RIFF", b"\xff\xff\xff\xff", b"WAVE",
        b"fmt ", (16).to_bytes(4, "little"),
        (1).to_bytes(2, "little"),              # PCM, uncompressed
        channels.to_bytes(2, "little"),
        rate.to_bytes(4, "little"),
        (rate * block).to_bytes(4, "little"),   # byte rate
        block.to_bytes(2, "little"),
        bits.to_bytes(2, "little"),
        b"data", b"\xff\xff\xff\xff",
    ))


def _chunk(handler, data: bytes) -> None:
    """One HTTP chunk. `http.server` does not frame these for us."""
    handler.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
    handler.wfile.write(data)
    handler.wfile.write(b"\r\n")
    handler.wfile.flush()


#: How long to wait for the first fragment before giving up on a stream.
#:
#: A cold start has to spawn an encoder, take the sensor and reach a keyframe,
#: and on a Pi 2B that is not instant. Long enough to cover it; short enough
#: that a camera which will never produce anything says so rather than leaving
#: a spinner up for ever.
FIRST_FRAGMENT_WAIT_S = 12.0

CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    # img-src also names the OpenStreetMap tile CDN, the one off-box image
    # source, for the location map's tiles. The operator's browser fetches them
    # straight from OSM; the station never proxies them, which would spend the
    # metered uplink on map tiles.
    "img-src 'self' data: https://*.tile.openstreetmap.org; "
    "media-src 'self' blob:; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; "
    "frame-ancestors 'none'"
)

STYLE = """
 /* The console's palette, transcribed from server/web/src/styles.css rather
    than approximated - an installer moves between this page and the console,
    and two dark themes that almost match read as one of them being wrong.
    Transcribed, not shared: this page is served by a stdlib HTTP server on a
    box in a paddock and must stay self-contained, so the tokens are copied
    and the comment says where from. The console's Inter/JetBrains arrive via
    its bundle; system-ui and ui-monospace are those fonts' own fallbacks. */
 :root { --bg:#070b0f; --panel:#121a23; --panel-2:#0c1219; --line:#22303c;
         --line-soft:#1a2531; --text:#dde6ed; --muted:#7f929f; --dim:#4f626f;
         --brand:#00a0dc; --brand-dim:#0b7ba7; --accent:#35c48a;
         --warn:#e8b04b; --danger:#ff7a45;
         /* The page strip's height, and therefore the offset the slot strip
            pins to. One number in one place because the two must stack and
            never overlap; .pagetabs is given this height rather than being
            allowed to size to its links, so the offset cannot drift when a
            fallback font renders a pixel taller. */
         --nav-h:3.4rem;
         /* The label column every form row shares. */
         --label-w:9.5rem; }
 body { font: 15px/1.5 system-ui, sans-serif; margin: 0; background: var(--bg);
        color: var(--text); }
 main { max-width: 54rem; margin: 0 auto; padding: 2rem 1.25rem 4rem; }
 h1 { font-size: 1.35rem; margin: 0 0 .2rem; }
 h2 { font-size: 1rem; margin: 2rem 0 .6rem; color: var(--muted);
      text-transform: uppercase; letter-spacing: .08em; }
 .sub { color: var(--muted); margin: 0 0 1.2rem; }
 .card { background: var(--panel); border: 1px solid var(--line);
         border-radius: .625rem; padding: 1rem 1.1rem; margin-bottom: .9rem; }
 .row { display: flex; justify-content: space-between; gap: 1rem; padding: .3rem 0;
        border-bottom: 1px solid var(--line-soft); }
 .row:last-child { border-bottom: 0; }
 .k { color: var(--muted); }
 .ok { color: var(--accent); } .warn { color: var(--warn); } .bad { color: var(--danger); }
 /* "The station could not find out", which is neither good news nor a fault.
    It reads as muted body text on purpose: amber would put a station that is
    probably fine on the same footing as one that is known to be broken, and
    somebody reading this page is deciding whether to get in a vehicle. */
 .unknown { color: var(--muted); }
 .muted { color: var(--muted); font-size: .88rem; }
 /* Controls fill their grid column up to a readable cap rather than carrying
    a min-width: inside a fixed column a min-width is what overflows a phone,
    and one shared width is what makes a stack of rows look aligned on the
    right as well as the left. */
 input[type=text], input[type=password], input[type=number], select {
   font: .95rem system-ui, sans-serif; padding: .45rem .55rem; background: var(--panel-2);
   color: var(--text); border: 1px solid var(--line); border-radius: .375rem;
   width: 100%; max-width: 22rem; min-width: 0; box-sizing: border-box; }
 input:focus-visible, select:focus-visible { outline: 2px solid var(--brand);
   outline-offset: 1px; }
 input.code { font: 1.2rem ui-monospace, monospace; letter-spacing: .12em; width: 100%;
   box-sizing: border-box; text-transform: uppercase; }
 button, .btn { margin-top: .7rem; font: inherit; font-weight: 600; padding: .45rem 1rem;
   border-radius: .375rem; border: 1px solid var(--brand); background: var(--brand);
   color: #03202b; cursor: pointer; }
 button:hover, .btn:hover { background: var(--brand-dim); border-color: var(--brand-dim); }
 /* An anchor that opens or closes the editor, shaped like the buttons beside
    it. A link and not a button because with no script the only thing that can
    change :target is a navigation. The .btn colour has to beat the `a` rule
    above it, which it does on specificity. */
 .btn { display: inline-block; text-decoration: none; }
 .btn.quiet { background: transparent; border-color: var(--line);
   color: var(--text); }
 .btn.quiet:hover { background: var(--panel-2); border-color: var(--line); }
 .btn:focus-visible { outline: 2px solid var(--brand); outline-offset: 1px; }
 /* Two controls on one row of the .field grid: the grid would otherwise stack
    them, since each child is placed in column 2 explicitly. */
 .actions { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; }
 .msg { padding: .7rem .9rem; border-radius: .375rem; margin-bottom: 1rem;
        border: 1px solid; }
 .msg.bad { background: rgba(255,122,69,.08); border-color: rgba(255,122,69,.35);
            color: var(--danger); }
 .msg.good { background: rgba(53,196,138,.08); border-color: rgba(53,196,138,.35);
             color: var(--accent); }
 .pill { font-size: .78rem; padding: .1rem .5rem; border-radius: 999px; border: 1px solid; }
 .pill.ok { border-color: var(--accent); background: rgba(53,196,138,.1); color: var(--accent); }
 .pill.warn { border-color: var(--warn); background: rgba(232,176,75,.1); color: var(--warn); }
 .pill.bad { border-color: var(--danger); background: rgba(255,122,69,.1); color: var(--danger); }
 .pill.off { border-color: var(--line); background: var(--panel-2); color: var(--muted); }
 /* One shape for every label/control row on the page. Enrolment, location and
    all six slot forms are rendered by different code paths and each used to
    start its control wherever its label happened to end, so a card of five
    rows read as five unrelated things. A fixed label column is what makes
    them one form to look at.
    Controls are placed in column 2 explicitly rather than by flow: a row that
    carries a hint or a datalist as well as its input would otherwise push the
    extra into the label column. Explicit placement keeps the input beside its
    label and drops anything further underneath it, still in column 2. */
 .field { display: grid; grid-template-columns: var(--label-w) minmax(0, 1fr);
          gap: .35rem 1rem; align-items: center; margin: .55rem 0; }
 .field > label { grid-column: 1; }
 .field > :not(label) { grid-column: 2; }
 /* Both would otherwise be stretched to the column by the grid's default
    justification: a Save button the full width of a card reads as a banner,
    and a checkbox keeps its size but not its position — the 4px the UA
    stylesheet gives it is enough to sit visibly off the line every other
    control starts on, which is the exact complaint this rule answers. */
 .field > button, .field > .btn, .field > .actions { justify-self: start; }
 .field > input[type=checkbox] { justify-self: start; margin: 0; }
 /* Below this the two columns cannot both hold their content — a 9.5rem label
    beside a usable input does not fit a phone held upright — so it becomes one
    column, where every row still starts at the same x. */
 @media (max-width: 34rem) {
   .field { grid-template-columns: minmax(0, 1fr); }
   .field > label, .field > :not(label) { grid-column: 1; }
 }
 label { color: var(--muted); font-size: .9rem; }
 ul { margin: .4rem 0 0; padding-left: 1.1rem; color: var(--text); }
 li { padding: .1rem 0; }
 code { color: var(--muted); font-family: ui-monospace, monospace; }
 a { color: var(--brand); }
 /* The page strip, transcribed from the console's settings tabs (.tabs/.tab
    in server/web/src/styles.css) with its spacing vars resolved to rem. Links
    rather than buttons: these are four GET pages, not panels, and they must
    work with no script. The horizontal padding centres the strip over main's
    54rem column on anything wider than it. */
 .tabs { display: flex; gap: .25rem; overflow-x: auto; scrollbar-width: none;
   padding: .5rem max(.75rem, calc((100vw - 54rem) / 2));
   background: var(--panel-2); border-bottom: 1px solid var(--line); }
 .tabs::-webkit-scrollbar { display: none; }
 .tabs a { flex: none; border: 1px solid transparent; border-radius: .375rem;
   color: var(--muted); font-size: .8rem; padding: .5rem .75rem;
   text-decoration: none; white-space: nowrap; }
 .tabs a:hover { color: var(--text); }
 .tabs a.active { color: var(--brand); border-color: var(--line);
   background: rgba(0,160,220,.14); }
 .tabs a:focus-visible { outline: 2px solid var(--brand); outline-offset: -2px; }
 /* Pinned. The strip is how you leave a page, and on a phone at a site the
    reason to leave is usually something you had to scroll down to find. The
    fixed height is load-bearing rather than cosmetic — the slot strip pins to
    exactly this offset — so the vertical padding is dropped and the links are
    centred in it instead. Full-bleed already (it is outside main and pads
    itself to centre), so its own background covers the whole strip. */
 /* One bar: the mark, what this box is, and where you are in it. The title
    was an <h1> inside main, which scrolled away while the pinned tabs stayed —
    a heading that leaves and a bar that does not read as two separate things.
    The bar is sticky as a whole now, so --nav-h still describes exactly what
    the slot strip pins under. */
 /* Three columns, not a flex row with an absolutely-centred name. The name
    was positioned at 50% of the bar and the tabs were sized by their content,
    so on a station whose name is longer than the space left beside them the
    two overlapped — "Pi 5 Bench" printed through "Summary". A grid reserves
    the middle column, so the name is centred *and* nothing can grow into it;
    the outer columns are 1fr each so the centre stays centred on the bar
    rather than on whatever is left over. */
 .topbar { position: sticky; top: 0; z-index: 6; height: var(--nav-h);
   box-sizing: border-box; display: grid; align-items: center;
   grid-template-columns: 1fr auto 1fr;
   padding: 0 0 0 1.25rem; background: var(--panel-2);
   border-bottom: 1px solid var(--line); }
 .topbar-brand { display: flex; align-items: center; gap: .6rem; min-width: 0; }
 /* Narrow: the two labels that are the same on every station go, and the one
    that identifies this box stays. The mark still says what the product is. */
 @media (max-width: 58rem) {
   .topbar-title { display: none; }
 }
 @media (max-width: 44rem) {
   .topbar-station { display: none; }
 }
 .topbar-mark { display: block; flex: none; }
 .topbar-title { font-size: .95rem; font-weight: 600; letter-spacing: .01em;
   white-space: nowrap; }
 /* Which box this is. Centred in the bar rather than beside the title,
    because on a bench with three stations open in three tabs everything else
    in this strip is identical on every one. `position: absolute` so it centres
    on the *bar* and not on whatever space is left between the title and the
    tabs — those differ in width per page, which would make the name drift as
    you moved between them. */
 /* The middle column. Truncates rather than pushing the tabs about, because
    the bar's height is load-bearing — the slot strip pins to it — and a name
    that wrapped would take the strip with it. */
 .topbar-station { color: var(--text); font-size: 1.05rem; font-weight: 600;
   letter-spacing: .01em; white-space: nowrap; overflow: hidden;
   text-overflow: ellipsis; padding: 0 1rem; }
 /* Last in the strip. A form, because signing out changes state. */
 /* Height comes from the bar, never the other way round: `align-self:
    stretch` with a centred button means the control fills the strip without
    contributing a millimetre to it. */
 /* Identical box to a tab link, so the two sit on one line: same padding,
    same radius, same font size, and the same 1px transparent border — without
    that border the button is 2px shorter than its neighbours and reads as
    misaligned however the flexbox is set. */
 /* `display: contents` so the form contributes no box of its own and the
    button becomes a direct flex item of the strip, centred by the same line
    that centres the tabs. As a flex item the form was 48px tall against the
    nav's 37 and sat its button 6px low — an intermediate box that exists only
    because signing out has to be a POST. The form still submits normally; it
    simply stops taking part in layout. */
 .topbar-out { display: contents; }
 /* `margin-top: 0` is the one that matters. The page's general button rule
    gives every button .7rem of top margin, which is right for a button at the
    end of a form and is exactly what sat this one 11px below the tabs beside
    it. Everything else here matches a tab link so the two are the same box. */
 .topbar-out button { margin: 0; background: none; border: 1px solid transparent;
   border-radius: .375rem; color: var(--muted); font-size: .8rem;
   font-weight: 400; font-family: inherit; line-height: 1.5;
   padding: .5rem .75rem; cursor: pointer; white-space: nowrap; }
 .topbar-out button:hover { color: var(--danger);
   background: rgba(255,122,69,.1); border-color: rgba(255,122,69,.35); }
 .topbar-out button:focus-visible { outline: 2px solid var(--danger);
   outline-offset: -2px; }
 /* Pushed to the right of the title, and allowed to scroll on a narrow phone
    rather than wrapping the bar to two rows — a two-row bar would break the
    single --nav-h the slot strip pins to. */
 /* The right column: tabs then the way out, ending at the same gutter every
    other page element does. */
 /* `min-width: max-content` so this column never shrinks below the tabs and
    the sign-out. With three equal 1fr columns the tabs were squeezed and
    "Logging" was clipped — a nav item you cannot read is worse than a name
    that is a few pixels off centre, so when the bar is tight this column wins
    and the middle one gives way. On any wide screen there is slack and the
    name is exactly centred. */
 /* Hard against the right edge, and the sign-out is laid out by the same
    flex line as the tabs rather than being a block with its own padding —
    which is what left it sitting a couple of pixels low against them. */
 /* Hard against the window's right edge, mirroring the mark on the left.
    It used to end at the 54rem content column's gutter, which on a wide screen
    left the tabs floating in the middle of the bar with a hand's width of
    empty to their right — the bar is full-bleed and its contents should reach
    its ends. */
 .topbar-right { display: flex; align-items: center; align-self: stretch;
   justify-content: flex-end; min-width: max-content; gap: .25rem;
   padding-right: 1.25rem; }
 .topbar .pagetabs { position: static; height: auto; border-bottom: 0;
   background: none; padding: 0; overflow: visible; align-items: center; }
 /* Looks like the muted key it replaces until you point at it — a row of
    underlined blue on the page an installer scans would read as a list of
    warnings. */
 /* The second click, without script: the confirm form does not exist on the
    page until the fragment names it. Same mechanism the location dialog used,
    for the same reason — a destructive control should take two deliberate
    acts, and neither of them should depend on JavaScript being allowed. */
 .confirm { display: none; }
 .confirm:target { display: block; }
 /* Opening the confirm hides the control that opened it, so the two red
    buttons are never on screen together: while closed there is only "Reset
    station", and while open only "Erase everything". The trigger has to follow
    the confirm in the markup for this — `~` reaches forward only. */
 #reset:target ~ #reset-trigger { display: none; }
 .btn.danger, button.danger { border-color: var(--danger); color: var(--danger);
   background: rgba(255,122,69,.08); }
 .btn.danger:hover, button.danger:hover { background: rgba(255,122,69,.18); }
 /* A form that has been submitted and is waiting for the page to come back.
    Saving a device slot tears the driver down, builds a new one and then gives
    it a sensing tick to say whether it is there — seconds on a network camera,
    with nothing on the page changing meanwhile. A button that appears to have
    done nothing gets pressed again, and the second press is a second POST.
    `pointer-events` rather than `disabled`, because the state is set from the
    submit handler and a control disabled there stops being announced halfway
    through the action. */
 form[data-busy] { cursor: progress; }
 form[data-busy] button[type=submit] { pointer-events: none; opacity: .65; }
 /* Name, device, status — three columns, so the pills line up down the card
    instead of following each device name's length. */
 /* The columns are defined once, on the card, and each row borrows them with
    subgrid. A grid per row is what it was, and a grid only aligns its own
    columns — so every row sized column 2 to its own device name and the status
    pills landed wherever each name happened to end. The fallback below is a
    plain three-column grid for anything without subgrid: slightly ragged,
    which is what it was, rather than broken. */
 .slot-grid { display: grid; grid-template-columns: 7rem 1fr auto; }
 .slot-row { display: grid; grid-template-columns: 7rem 1fr auto;
   grid-column: 1 / -1; align-items: center; gap: .75rem; padding: .3rem 0;
   border-bottom: 1px solid var(--line-soft); }
 @supports (grid-template-columns: subgrid) {
   .slot-row { grid-template-columns: subgrid; }
 }
 .slot-row:last-of-type { border-bottom: 0; }
 .slot-device { display: flex; align-items: center; gap: .5rem;
   justify-content: flex-end; min-width: 0; }
 .slot-device > :first-child { overflow: hidden; text-overflow: ellipsis;
   white-space: nowrap; }
 .pill.demo { border-color: var(--warn); background: rgba(232,176,75,.12);
   color: var(--warn); }
 .slot-link { color: var(--muted); text-decoration: none; }
 .slot-link:hover { color: var(--brand); text-decoration: underline; }
 .slot-link:focus-visible { outline: 2px solid var(--brand); outline-offset: 2px;
   border-radius: 3px; }
 /* The picker's select and its no-script button share a row. `.pick-go` is
    hidden by the page's script, so with script the select alone re-renders and
    with none there is a button that does. */
 .pick { margin: 0; }
 .pick .field select { min-width: 0; }
 .pick-go { margin-left: .5rem; }
 .slot-head { display: flex; justify-content: space-between; align-items: baseline;
              gap: 1rem; }
 /* The Devices page's slot strip: same tab language as the page strip, one
    level down, so "which slot am I on" reads the same way as "which page".
    Pinned *under* the page strip — top is that strip's own height, and a lower
    z-index so that if a browser ever disagrees about the arithmetic the page
    strip is the one that stays whole.
    It sits inside main's centred column and is deliberately **not** bled out
    to the viewport edges, which is worth saying because the opposite looks
    like the bug. A sticky strip inside a centred column does need its
    background to cover the full width when that background differs from the
    page's — otherwise content scrolls visibly through the gutters. Here it is
    var(--bg), the page's own colour, and main's gutters never have anything
    painted in them: every child of main is exactly the column's width. So a
    full-bleed adds nothing visible, and the ways of getting one are all worse
    than nothing — negative margins with 100vw hand a phone a horizontal
    scrollbar when the scrollbar is a classic one, and a spread box-shadow
    repaints a viewport-sized layer on every scroll frame of a page whose job
    is to work on a 900 MHz box's cheap phone. The hairline is the separation
    instead, and it lines up with the cards. */
 .subtabs { position: sticky; top: var(--nav-h); z-index: 5;
   background: var(--bg); border-bottom: 1px solid var(--line-soft);
   padding: .45rem 0; margin: 0 0 .6rem; }
 /* The datastream field: the sensor's own last lines, monospace, bounded.
    A fixed min-height so an empty field reads as "no data", not as a
    missing element. */
 /* The event list scrolls inside itself rather than lengthening the page.
    A hundred events is several screens, and scrolling the page took the
    header, the tabs and the storage summary underneath it out of view — so
    the one thing you scroll to read cost you everything around it.

    Height is the viewport minus the header, the heading, the two muted lines
    and the card's own padding, so the box ends near the bottom of the window
    whatever the window is. `min-height` keeps it usable on a short laptop
    where that arithmetic would otherwise leave a sliver. */
 ul.log { max-height: calc(100vh - var(--nav-h) - 17.5rem); min-height: 12rem;
   overflow-y: auto; margin: 0; padding-left: 1.1rem;
   overscroll-behavior: contain; }
 ul.log li { margin: .15rem 0; }

 /* Fixed size, and deliberately not responsive: an earlier version of this
    sized itself to its container and re-measured on every telemetry frame. */
 .spectrum { display: block; max-width: 100%; background: var(--panel-2);
   border: 1px solid var(--line); border-radius: .375rem; margin-bottom: .6rem; }

 /* The signal meter, matching the platform's settings panel so the bar an
    operator learns to read means the same in both places: a readout row, then a
    slim bar whose fill is the in-channel signal, a hairline at the noise floor,
    and the squelch threshold as a thumb riding the same dB scale — set the
    level against the signal it gates, not on a control beside it. The gradient
    is fixed to the scale (a colour is a level, not a fill), and the dB->%
    mapping is the platform's verbatim: -90 floor, -10 saturation. */
 .radio-readout { display: flex; gap: 1rem; align-items: center;
   flex-wrap: wrap; font-size: .82rem; color: var(--muted); margin: 0 0 .1rem; }
 .radio-readout b { color: var(--text); font-weight: 600; }
 .led { width: .8rem; height: .8rem; border-radius: 50%; background: #17222c;
   border: 1px solid var(--line); flex: none; }
 .led.on { background: #3fb950; border-color: #3fb950;
   box-shadow: 0 0 .5rem #3fb950; }
 .meter { position: relative; height: .5rem; margin: .45rem 0;
   background: #060a0e; border: 1px solid var(--line); border-radius: .25rem;
   overflow: visible; }
 .meter-fill { position: absolute; inset: 0 auto 0 0; border-radius: .25rem;
   background: linear-gradient(90deg, #3fb950, #d29922 75%, #f85149);
   transition: width 150ms linear; }
 .meter-floor { position: absolute; top: -.15rem; bottom: -.15rem; width: 1px;
   background: var(--dim); opacity: .7; }
 .squelch-overlay { position: absolute; left: 0; right: 0; top: 50%;
   height: 1.5rem; transform: translateY(-50%); width: 100%; margin: 0;
   appearance: none; -webkit-appearance: none; background: none; padding: 0;
   border: 0; cursor: ew-resize; }
 .squelch-overlay::-webkit-slider-thumb { -webkit-appearance: none;
   appearance: none; width: 1rem; height: 1rem; border-radius: 50%;
   background: var(--brand); border: 2px solid var(--bg);
   box-shadow: 0 0 .4rem rgba(0,160,220,.6); cursor: ew-resize; }
 .squelch-overlay::-moz-range-thumb { width: 1rem; height: 1rem;
   border-radius: 50%; background: var(--brand); border: 2px solid var(--bg);
   box-shadow: 0 0 .4rem rgba(0,160,220,.6); cursor: ew-resize; }
 /* Volume, not a play button — dragging it is the gesture that starts the
    audio, the same as the platform. Fills the control column. */
 .volume-row { display: flex; align-items: center; gap: .6rem; }
 .volume-row input[type=range] { flex: 1; min-width: 6rem; }

 pre.raw { font: .8rem ui-monospace, monospace; color: var(--text);
   background: var(--panel-2); border: 1px solid var(--line-soft);
   border-radius: .375rem; padding: .55rem .7rem; margin: .4rem 0 .6rem;
   min-height: 2.4rem; white-space: pre; overflow-x: auto; }
 button:disabled { opacity: .45; cursor: default; }
 button:disabled:hover { background: var(--brand); border-color: var(--brand); }
 /* The camera preview. Same bounded box as the datastream field; the hidden
    checkbox is the expand state, so the zoom works with scripts blocked —
    :checked pins the label over the whole viewport. */
 .zoom-toggle { display: none; }
 .preview { display: block; margin: .4rem 0 .6rem; }
 /* The live element and the still fallback share this: the <video> arrives
    with a 1920x1080 intrinsic size and, without a cap, renders at it and
    overflows the card. `height: auto` keeps the aspect from the stream rather
    than from anything this page assumes about the camera. */
 .preview img, .preview video { display: block; max-width: 100%; height: auto;
   border: 1px solid var(--line-soft);
   border-radius: .375rem; cursor: zoom-in; background: #000; }
 .preview > span { display: block; padding: .55rem .7rem; min-height: 1.3rem;
   background: var(--panel-2); border: 1px solid var(--line-soft);
   border-radius: .375rem; font-size: .8rem; }
 /* One of three at a time, chosen by a class rather than by rebuilding the
    box. All three elements stay in the page: the <video> is what the live
    stream attaches to, and removing it to show a still stops the stream.
    An empty <video> is also a black rectangle, and a black rectangle where a
    camera should be reads as a camera working in an unlit room — so when
    there is nothing to show, none of them is displayed except the message. */
 #preview-empty { color: var(--muted); }
 .preview > video, .preview > #preview-still, .preview > #preview-empty {
   display: none; }
 .preview.live > video { display: block; }
 .preview.still > #preview-still { display: block; }
 .preview.empty > #preview-empty { display: block; }
 .zoom-toggle:checked ~ .preview { position: fixed; inset: 0; z-index: 10;
   margin: 0; display: grid; place-items: center; padding: 1.5rem;
   background: rgba(7,11,15,.94); cursor: zoom-out; }
 .zoom-toggle:checked ~ .preview img,
 .zoom-toggle:checked ~ .preview video { max-width: 100%; max-height: 100%;
   border: 0; cursor: zoom-out; }
 /* The pop-out editor, on the same no-script principle as the zoom above but
    driven by :target rather than a checkbox, because this one has to be
    openable by something other than a click. A refused save redirects to
    /connection#location, and a fragment in a Location header reopens the
    dialog with the reason inside it; nothing a hidden checkbox can be made to
    do from the server. The URL is also the state, so a reload keeps the editor
    open and Back closes it, which is what a person expects of a dialog.
    Closed is display:none rather than off-screen: the inputs inside must be
    unreachable by Tab and unread by a screen reader while the dialog is shut,
    and they are still submitted normally when it is open. */
 .fixed { color: var(--text); font-family: ui-monospace, monospace; font-size: .9rem;
          word-break: break-all; }
 /* The sign-in, shaped like the console's: a centred card under the brand
    glow, the mark above the wordmark at the console's large brand size. The
    page still names no station: PERCEPTA is what the product is, not which
    box this is. */
 .brand-mark { display: block; width: 3.5rem; height: 3.5rem;
   margin: 0 auto .625rem; }
 .login-wrap { min-height: 100vh; display: grid; place-items: center;
   background: radial-gradient(60% 60% at 50% 38%, rgba(0,160,220,.09) 0%, var(--bg) 70%); }
 .login-card { width: min(22.5rem, calc(100vw - 2rem)); background: var(--panel);
   border: 1px solid var(--line); border-radius: .625rem; padding: 1.75rem;
   display: flex; flex-direction: column; box-sizing: border-box; }
 .brand-word { font-weight: 700; letter-spacing: .18em; font-size: .812rem;
   text-align: center; margin-bottom: 1.375rem; }
 .login-card h1 { font-size: 1rem; font-weight: 600; margin: 0 0 .8rem; text-align: center; }
 .login-card label { font-size: .75rem; margin-bottom: .3rem; }
 .login-card input { width: 100%; box-sizing: border-box; min-width: 0; }
 .login-card button { width: 100%; margin-top: 1.125rem; padding: .55rem 1rem; }
 .login-card .muted { margin-top: .9rem; font-size: .8rem; }
"""

#: The four pages, in the order the strip shows them. The path is the whole
#: identity: the router, the nav and the post-redirects all key on it, so a
#: page cannot be reachable without appearing in the strip or vice versa.
#: Slot ids are wire identifiers — lowercase, no punctuation, and the key in
#: devices.json. What goes on a tab is a name a person reads, so ADS-B keeps
#: the hyphen the standard spells it with rather than inheriting the id's
#: convenience. One map so a new slot cannot appear on screen as a raw key.
SLOT_LABELS = {
    "adsb": "ADS-B",
    "radio": "Radio",
    "weather": "Weather",
    "power": "Power",
    "light": "Light",
    "camera": "Camera",
}

PAGES = {
    "/": "Summary",
    "/connection": "Connection",
    "/devices": "Devices",
    "/logging": "Logging",
}

#: Where each POST goes home to: the page its form lives on, fixed here rather
#: than read from the request, so there is no redirect an attacker can choose.
POST_HOME = {
    "/reset": "/connection",
    "/radio": "/devices?slot=radio",
    "/device": "/devices",
    "/enrol": "/connection",
    "/location": "/connection",
    "/transcripts": "/",
    "/logout": "/",
}

#: Three states, an owner requirement, and they answer three different
#: questions an installer actually has:
#:
#:   Not fitted    nothing is selected for this slot. Not a fault — a station
#:                 without a floodlight is a complete station.
#:   Connected     selected, and talking to us right now.
#:   Disconnected  selected, and not. Something is wrong and it is here.
#:
#: The internal vocabulary keeps four, because `stalled` — a device that was
#: streaming and has gone quiet — is a genuinely different thing from one that
#: never answered, and the drivers are right to distinguish them. It is not a
#: different thing *to an installer standing at the site*: both mean "this is
#: selected and you are not getting data from it", and both send them to the
#: same cable. So the two collapse here, at the point of display, and the
#: `found:` line underneath keeps the distinction for whoever needs it.
#:
#: Disconnected is amber rather than red on purpose. It is overwhelmingly the
#: normal state during commissioning — you select the device before you plug it
#: in — and a red pill on every slot you have not wired yet teaches people that
#: red means nothing.
STATUS_PILL = {
    "present": ("ok", "Connected"),
    "stalled": ("warn", "Disconnected"),
    "configured_absent": ("warn", "Disconnected"),
    "not_fitted": ("off", "Not fitted"),
}


def _degrees(value: float | None) -> str:
    """A coordinate as it was typed, not as a float prints.

    `%g` keeps 173.68 as `173.68` instead of `173.68000000000001`, and drops
    the trailing zeros that make a rounded number look like a measured one.
    """
    return "" if value is None else f"{value:g}"


def _host_is_addressable(host_header: str | None) -> bool:
    """Whether the `Host:` we were asked for is one this box can legitimately
    be called by.

    The attack this is for is DNS rebinding: a page on the public internet
    resolves its own name to this station's private address and then drives
    these forms from inside a technician's browser, with the browser's own
    network position. It needs a *name*, because it works by changing what a
    name resolves to. An IP literal cannot be rebound, and neither can an mDNS
    `.local` name — those are answered on the link, not by the public DNS.
    """
    if not host_header:
        return False
    host = host_header.strip()
    if host.startswith("["):                     # [::1]:8088
        host = host.partition("]")[0][1:]
    else:
        host = host.split(":")[0]
    host = host.lower()
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class Console:
    """The setup page, its socket, and the rules about when that socket exists.

    The socket is not a fixed thing. `host` is what the operator asked for;
    `bound_host` is where it actually is, which is loopback whenever the LAN
    listener is not allowed to exist — no password configured, or the window
    closed. Closing means closing: the port stops answering rather than starting
    to answer 403, because a port that answers is a port somebody enumerates.
    """

    def __init__(
        self,
        agent,
        host: str = "127.0.0.1",
        port: int = 8088,
        *,
        password: str | None = None,
        window_minutes: float = 30.0,
        reopen_path: Path | None = None,
    ) -> None:
        self.agent = agent
        self.host = host
        self.port = port
        self.gate = Gate(
            password=password,
            window_minutes=window_minutes,
            reopen_path=reopen_path,
            enrolled=lambda: getattr(agent, "enrolment", None) is not None,
        )
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.bound_host: str | None = None
        #: Why the LAN listener is not up, when it is not. Rendered, so that a
        #: technician who cannot reach the page from a laptop but can over SSH
        #: is told the reason on the page they *can* reach.
        self.demotion_reason: str = ""
        self.message: tuple[str, str] | None = None
        #: Which dialog the pending message belongs inside, if any. A refused
        #: location save reopens its dialog, and the reason has to be in there
        #: with the inputs rather than on the page the overlay is covering.
        #: `None` — the ordinary case — is the banner at the top of the page.
        self.message_at: str | None = None

    @classmethod
    def from_config(cls, agent, config) -> Console:
        return cls(
            agent, config.setup_host, config.setup_port,
            password=config.setup_password,
            window_minutes=config.setup_window_minutes,
            reopen_path=config.setup_reopen_path,
        )

    # --- lifecycle ------------------------------------------------------

    def _target_host(self) -> tuple[str, str]:
        """Where the listener should be right now, and why not where asked.

        This is the safety property the whole design rests on: **there is no
        return value from this function that puts a socket on a routable
        interface without a password.** It is a function rather than a check at
        start-up so that the window closing takes the socket away again.
        """
        if is_loopback_host(self.host):
            return self.host, ""
        if not self.gate.has_password:
            return "127.0.0.1", (
                f"GSU_SETUP_HOST is {self.host} but no GSU_SETUP_PASSWORD_HASH "
                "is set, so the setup page would have been an unauthenticated "
                "form on a routable interface. It is on loopback instead — "
                "reach it over an SSH tunnel, or set a password and restart."
            )
        if not self.gate.window_open():
            return "127.0.0.1", (
                "The setup window has closed. Reboot the station, or touch "
                "the setup-open file in the state directory, to open it again."
            )
        return self.host, ""

    def start(self) -> None:
        host, reason = self._target_host()
        self.demotion_reason = reason
        if reason:
            # Loud, and a health condition: a station whose setup page is not
            # where the installer was told it would be is a site visit unless
            # somebody is told why, and the log on a box nobody can reach is
            # not where they will be told.
            log.error("Setup page demoted to loopback: %s", reason)
            self._raise_condition("setup.demoted", "warning", reason)
        if not self._bind(host):
            return
        # No warning for a pinned-open window: it is the default now, not an
        # unusual override. A deployment that sets a positive GSU_SETUP_WINDOW_
        # MINUTES gets the timed close back; the rest stay reachable, guarded by
        # the password and the local-only source check.
        self._watcher = threading.Thread(
            target=self._watch, name="gsu-console-window", daemon=True
        )
        self._watcher.start()

    def _bind(self, host: str) -> bool:
        if self._stop.is_set():
            # The watcher and `stop()` can race at shutdown, and the loser must
            # not be the one that leaves a listening socket behind.
            return False
        console = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            #: A held-open connection holds a thread. On a box with 64 tasks in
            #: its systemd TasksMax, that is a denial of service one telnet away.
            timeout = 20

            def log_message(self, *args):  # noqa: A002
                pass

            def do_GET(self):  # noqa: N802
                console._handle(self, "GET")

            def do_POST(self):  # noqa: N802
                console._handle(self, "POST")

        try:
            server = ThreadingHTTPServer((host, self.port), Handler)
        except OSError as exc:
            # A console that cannot bind must not stop the station working.
            log.warning("Console could not start on %s:%s (%s).", host, self.port, exc)
            return False
        server.daemon_threads = True
        with self._lock:
            self._server = server
            self.bound_host = host
        self._thread = threading.Thread(
            target=server.serve_forever, name="gsu-console", daemon=True
        )
        self._thread.start()
        log.info(
            "Setup page at http://%s:%s%s", host, self.port,
            "" if is_loopback_host(host) else " (password required from the LAN)",
        )
        return True

    def _watch(self) -> None:
        """Move the socket when the rules change.

        Two transitions, and both matter. Closing takes the LAN listener away
        when the window expires. Opening brings it back when somebody creates
        the reopen marker — without which the marker would only take effect at
        the next restart, and "reboot the station to reach the setup page" is
        exactly the site visit this is all trying to avoid.
        """
        while not self._stop.wait(WATCH_SECONDS):
            try:
                target, reason = self._target_host()
                if target == self.bound_host:
                    self.demotion_reason = reason
                    continue
                log.info(
                    "Setup listener moving from %s to %s. %s",
                    self.bound_host, target, reason or "",
                )
                if target == "127.0.0.1":
                    # Cookies do not outlive the door being shut. A laptop left
                    # on the bench must not walk back in when it reopens.
                    self.gate.forget_all()
                self._shutdown_server()
                self.demotion_reason = reason
                self._bind(target)
            except Exception:  # noqa: BLE001 - a watchdog that dies is silent
                log.exception("Setup window check failed; continuing.")

    def _shutdown_server(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            self.bound_host = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def stop(self) -> None:
        self._stop.set()
        self._shutdown_server()

    def _raise_condition(self, ident: str, severity: str, detail: str) -> None:
        health = getattr(self.agent, "health", None)
        raise_condition = getattr(health, "raise_condition", None)
        if raise_condition:
            try:
                raise_condition(ident, severity, detail)
            except Exception:  # noqa: BLE001
                pass

    # --- request handling -------------------------------------------------

    def _handle(self, handler, method: str) -> None:
        """Everything that is decided before a request is looked at."""
        path = urlsplit(handler.path).path

        if not _host_is_addressable(handler.headers.get("Host")):
            return self._deny(handler, 400, "Bad Host header.")

        peer = handler.client_address[0] if handler.client_address else ""
        decision = self.gate.authorise(peer, handler.headers.get("Cookie"))

        if method == "POST" and path == "/login":
            return self._do_login(handler, peer)

        if not decision.allow:
            if decision.login:
                # A plain first view carries no error: nobody has done
                # anything yet, and "password required" in a red box reads as
                # a fault. Reasons appear only on the responses to an actual
                # attempt — wrong password, lockout — which _do_login renders.
                return self._send_html(
                    handler, self._render_login(""), status=decision.status,
                )
            log.warning(
                "Setup page refused a %s from %s: %s", method, peer, decision.reason
            )
            return self._deny(handler, decision.status, decision.reason)

        session = decision.session
        cookie = session.token if decision.set_cookie and session else None

        if method == "GET":
            if path.startswith("/status.json"):
                return self._send_json(handler, self.agent.snapshot(), cookie)
            if path.startswith("/registry.json"):
                return self._send_json(handler, self._registry_json(), cookie)
            if path.startswith("/stream.mp4"):
                return self._send_stream(handler, cookie)
            if path.startswith("/audio.wav"):
                return self._send_audio(handler, cookie)
            if path.startswith("/frame.jpg"):
                return self._send_frame(handler, cookie)
            if path in ("/index.html", "/login"):
                path = "/"
            if path in PAGES:
                slot = None
                chosen = None
                nonce = None
                if path in ("/devices", "/connection"):
                    # The pages that carry an inline script. Minted here and per
                    # response, so the header and the tag can agree and nothing
                    # injected into rendered content can guess it.
                    #
                    # Connection carries one for the location map — a slippy map
                    # under the coordinate fields that writes back where the pin
                    # sits. Without the script, or without the tiles, the map is
                    # hidden and the fields stand alone exactly as they did.
                    nonce = secrets.token_urlsafe(16)
                if path == "/devices":
                    # One sub-tab per slot; the query names it and anything
                    # unrecognised lands on the first tab rather than erroring.
                    # keep_blank_values, because "— not fitted —" posts
                    # `type=` with nothing after it. Dropped as blank, the
                    # picker fell back to the stored device and un-fitting a
                    # slot was silently impossible — which is the one thing
                    # you reach for when a device has failed.
                    query = parse_qs(urlsplit(handler.path).query,
                                     keep_blank_values=True)
                    slot = (query.get("slot") or [""])[0]
                    if slot not in registry.SLOTS:
                        slot = registry.SLOTS[0]
                    # Present only when the device picker has been used, which
                    # is how "show me this one's settings" is distinguished
                    # from an ordinary visit to the tab.
                    if "type" in query:
                        chosen = (query.get("type") or [""])[0]
                return self._send_html(
                    handler,
                    self.render(session, path, slot=slot, nonce=nonce,
                                chosen=chosen),
                    cookie=cookie, nonce=nonce,
                )
            return self._deny(handler, 404, "No such page.")

        # --- POST, which changes something --------------------------------
        if not self._same_origin(handler):
            return self._deny(handler, 403, "Cross-origin request refused.")
        home = POST_HOME.get(path)
        if home is None:
            return self._deny(handler, 404, "No such action.")
        form = self._read_form(handler)
        if form is None:
            return self._deny(handler, 413, "That request was too large.")
        # The radio tab applies each control the moment it changes, with a fetch
        # that carries this marker. It wants a small answer it can show inline —
        # not the 303 the form fallback needs, which would reload the page and
        # tear down the audio the operator is listening to while they tune.
        is_ajax = bool(form.get("ajax"))
        if not self.gate.check_csrf(session, (form.get("csrf") or [""])[0]):
            # Almost always a stale tab rather than an attack, and the wording
            # says so — but it is refused either way.
            log.warning("Setup POST from %s had no valid CSRF token.", peer)
            self.message = ("bad", "That page had gone stale. Reload and try again.")
            if is_ajax:
                return self._send_json(
                    handler, {"ok": False, "message": self.message[1]}, cookie)
            return self._redirect(handler, cookie, home)
        try:
            if path == "/device":
                saved = self._set_device(form)
                if saved:
                    # Back to the sub-tab the form lives on. `saved` has been
                    # validated against registry.SLOTS — nothing from the
                    # request reaches the Location header unchecked.
                    home = f"/devices?slot={saved}"
            elif path == "/enrol":
                self._enrol(form)
            elif path == "/location":
                self._set_location(form)
            elif path == "/radio":
                self._set_radio(form)
            elif path == "/transcripts":
                self._clear_transcripts()
            elif path == "/reset":
                self._reset(form)
            else:  # /logout
                self.gate.forget_all()
                self.message = ("good", "Signed out.")
        except Exception as exc:  # noqa: BLE001 - shown to a person
            self.message = ("bad", str(exc))
        if is_ajax:
            # No redirect: answer the fetch with what happened and consume the
            # message, so a later full render does not show it a second time.
            ok = not (self.message and self.message[0] == "bad")
            text = self.message[1] if self.message else "Saved."
            self.message = None
            return self._send_json(handler, {"ok": ok, "message": text}, cookie)
        if path == "/location" and self.message and self.message[0] == "bad":
            # A refused coordinate reopens the editor with the reason in it.
            # The fragment is a constant like every other part of this header
            # — nothing from the request reaches it — and it is the whole
            # reason the dialog is driven by :target and not by a checkbox:
            # a redirect can reopen a dialog whose state is its URL, and can
            # do nothing at all to a checkbox.
            self.message_at = "location"
            home = f"{home}#location"
        self._redirect(handler, cookie, home)

    def _same_origin(self, handler) -> bool:
        """Refuse a POST whose `Origin` is not us.

        Browsers send `Origin` on every form POST, so a missing one is a
        non-browser client — curl, or the update gate — and those are judged by
        the peer address and the CSRF token instead. A *present* and mismatched
        one is a cross-site post and there is no benign version of that.
        """
        origin = handler.headers.get("Origin")
        if not origin:
            return True
        host = handler.headers.get("Host") or ""
        return urlsplit(origin).netloc.lower() == host.strip().lower()

    def _read_form(self, handler) -> dict | None:
        """Read a bounded body. `Content-Length` is attacker-controlled and this
        box has 1 GB of RAM, so the header is a claim and not a permission."""
        try:
            length = int(handler.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None
        body = handler.rfile.read(length)
        if len(body) != length:
            return None
        return parse_qs(body.decode("utf-8", "replace"))

    def _do_login(self, handler, peer: str) -> None:
        if not self._same_origin(handler):
            return self._deny(handler, 403, "Cross-origin request refused.")
        form = self._read_form(handler)
        if form is None:
            return self._deny(handler, 413, "That request was too large.")
        decision = self.gate.login(peer, (form.get("password") or [""])[0])
        if not decision.allow:
            # The password is never echoed, never logged and never put in a
            # redirect. The only thing that comes back is whether it worked.
            return self._send_html(
                handler, self._render_login(decision.reason), status=decision.status,
            )
        self._redirect(handler, decision.session.token if decision.session else None)

    # --- actions --------------------------------------------------------

    def _enrol(self, form: dict) -> None:
        token = (form.get("token") or [""])[0].strip()
        if not token:
            self.message = ("bad", "Enter the code from the platform.")
            return
        enrolment = self.agent.enrol(token)
        # The code itself is not repeated back. It is single-use, but it is
        # also a shared secret that would then be sitting in a browser's
        # rendered page and in whatever is behind the technician on the roof.
        self.message = (
            "good",
            f"Enrolled as {enrolment.site.name}. Telemetry is on its way.",
        )

    def _set_location(self, form: dict) -> None:
        """Where this box is, set by the person standing at it.

        **This was read-only, and it should not have been.** The reasoning was
        that a box which has moved is recommissioned rather than edited, so the
        position was settled at enrolment and frozen. But that left an enrolled
        box with no way to be given a position at all except by revoking its
        credential and re-enrolling — and the position the platform then reissues
        is the one it already had, so even that did not work. The person at the
        mast is the one who knows the coordinates; this is where they belong.

        Written to the station's own `SiteConfig`, which `effective_position`
        already prefers over anything the platform issued (agent.py). Nothing
        else changes: the platform still carries its own idea of where the box
        is, and the box still reports its position up.

        Latitude and longitude are both-or-neither: a lone coordinate is a
        half-entered pair, not a position, and storing it would put the box
        somewhere on the equator or the prime meridian.
        """
        from .config import parse_elevation_m, parse_latitude, parse_longitude

        def read(name: str, parser):
            raw = (form.get(name) or [""])[0].strip()
            return parser(raw) if raw else None

        latitude = read("latitude", parse_latitude)
        longitude = read("longitude", parse_longitude)
        elevation = read("elevation_m", parse_elevation_m)

        if (latitude is None) != (longitude is None):
            raise ValueError(
                "Give both a latitude and a longitude, or clear both. One on "
                "its own is not a position."
            )

        # Through the agent's own setter, which parses nothing (already done
        # here) but stores and acts on all three together — see set_location.
        self.agent.set_location(latitude, longitude, elevation)
        self.message = ("good", "Saved.")

    def _set_radio(self, form: dict) -> None:
        """The radio panel is one form with one button; this splits what it
        carries into the two kinds of thing it holds.

        The dashboard drives the receiver through discrete operate commands —
        tune, gain, squelch, monitor — and the setup page now offers the same
        set so a box can be brought up and *heard* before it is enrolled. Those
        act on the live receiver and are saved in its own state file, surviving a
        restart.

        Some controls are device-level, because the front end has to be reopened
        for them: which receiver the tuner is assigned to, the bias tee, and the
        channel and voice filters. Those are stored in the inventory and rebuild
        the receiver — but only when they actually change, so setting a gain or a
        squelch does not tear the tuner down. They arrive as `dev_<param>` fields
        and are typed from the registry, so a new device parameter needs no
        change here.

        Transcription is a persisted site setting the sensing loop re-reads every
        tick: it takes effect at once and survives a restart, but only does
        anything when whisper.cpp and a model are on the box, which the form
        says.

        This replaced the earlier freq+monitor+transcribe handler when the
        panel's two buttons — a device Save and a radio Apply — were merged into
        this single Apply.
        """
        type_id = (form.get("type_id") or [""])[0]
        device = registry.get(type_id) if type_id else None
        if type_id and device is None:
            raise ValueError(f"{type_id!r} is not a device this station supports.")
        resource = (form.get("resource") or [""])[0] or None

        # Rebuild only when a device-level setting changed — a gain or squelch
        # tweak on every Apply must not keep tearing the tuner down and back up.
        previous = self.agent.inventory.fitted.get("radio")
        prev_type = previous.type_id if previous else ""
        prev_params = dict((previous.params or {}) if previous else {})
        prev_resource = previous.resource if previous else None

        # The device-rebuild parameters, read from the `dev_<param>` fields and
        # typed from the registry. gain and ppm are skipped: they are stored
        # device parameters too, but the receiver overrides them from its own
        # live state, so they are operated below rather than rebuilt here — and
        # whatever was stored for them is carried forward untouched.
        new_params = dict(prev_params) if type_id == prev_type else {}
        if device is not None:
            for parameter in device.parameters:
                if parameter.name in ("gain", "ppm"):
                    continue
                field = f"dev_{parameter.name}"
                if parameter.type == "bool":
                    new_params[parameter.name] = bool(form.get(field))
                else:
                    raw = (form.get(field) or [""])[0].strip()
                    if raw != "":
                        new_params[parameter.name] = (
                            (float(raw) if "." in raw else int(raw))
                            if parameter.type == "number" else raw
                        )
        device_changed = (
            type_id != prev_type
            or (bool(type_id) and new_params != prev_params)
            or (bool(type_id) and resource != prev_resource)
        )
        if device_changed:
            self.agent.inventory.set_device("radio", type_id, new_params, resource)
            self.agent.build_devices()

        # Live operate commands, on whatever receiver exists now — a fresh one if
        # the rebuild above ran, which reloaded freq/gain/squelch from its state
        # file and is about to have this Apply's values put on top.
        radio = self.agent.radio
        if radio is not None:
            raw = (form.get("freq_mhz") or [""])[0].strip()
            if raw:
                try:
                    radio.tune(int(round(float(raw) * 1e6)))
                except ValueError:
                    raise ValueError(f"{raw!r} is not a frequency in MHz.")
            gain = (form.get("gain") or [""])[0].strip()
            if gain:
                try:
                    radio.set_gain(
                        gain if gain in ("auto", "managed") else float(gain))
                except ValueError:
                    raise ValueError(f"{gain!r} is not a gain in dB.")
            ppm = (form.get("ppm") or [""])[0].strip()
            if ppm:
                try:
                    radio.set_ppm(int(float(ppm)))
                except ValueError:
                    raise ValueError(f"{ppm!r} is not a crystal correction in ppm.")
            # AUTO wins if ticked; otherwise the slider's level, which set_squelch
            # applies and turns AUTO off in the same move.
            if form.get("auto_squelch"):
                radio.set_auto_squelch(True)
            else:
                level = (form.get("squelch") or [""])[0].strip()
                if level:
                    try:
                        radio.set_squelch(float(level))
                    except ValueError:
                        raise ValueError(f"{level!r} is not a squelch level in dB.")
                else:
                    radio.set_auto_squelch(False)
            radio.set_monitor(bool(form.get("monitor")))
            margin = (form.get("auto_margin") or [""])[0].strip()
            if margin:
                try:
                    radio.set_auto_margin(float(margin))
                except ValueError:
                    raise ValueError(f"{margin!r} is not a squelch margin in dB.")
            hang = (form.get("hang_s") or [""])[0].strip()
            if hang:
                try:
                    radio.set_hang(float(hang))
                except ValueError:
                    raise ValueError(f"{hang!r} is not a hang time in seconds.")

        self.agent.site.radio_transcribe = bool(form.get("radio_transcribe"))
        keep = (form.get("transcript_days") or [""])[0].strip()
        if keep:
            try:
                self.agent.site.transcript_retention_days = max(0.0, float(keep))
            except ValueError:
                raise ValueError(f"{keep!r} is not a number of days.")
        self.agent.site.save(self.agent.config.site_config_path)
        self.message = ("good", "Saved.")

    def _clear_transcripts(self) -> None:
        """Delete the airband transcripts kept on the box, on an explicit click
        from the events section — separate from the radio form so it is never an
        accident of tuning."""
        count = self.agent.store.clear_transcripts()
        self.message = ("good", f"Cleared {count} transcript(s).")

    def _reset(self, form: dict) -> None:
        """Return the box to how it shipped.

        Two clicks rather than a typed confirmation. This page answers only on
        the local network, behind a password, inside a time-boxed window, so
        anybody who can see the button is at the hardware intending to
        reprovision it — and what is destroyed is a box's configuration, not a
        customer's records, which live on the platform.

        The second click is still required, because "everything" includes the
        credential, and a station that has to be re-enrolled is a phone call to
        whoever holds the codes.
        """
        if (form.get("confirm") or [""])[0] != "yes":
            raise ValueError("Reset not confirmed.")
        gone = self.agent.factory_reset()
        self.message = ("good", "Reset. Cleared: " + ", ".join(gone) + ".")

    def _set_device(self, form: dict) -> str:
        slot = (form.get("slot") or [""])[0]
        if slot not in registry.SLOTS:
            raise ValueError(f"{slot!r} is not a slot on this station.")
        type_id = (form.get("type_id") or [""])[0]
        if type_id and registry.get(type_id) is None:
            raise ValueError(f"{type_id!r} is not a device this station supports.")
        resource = (form.get("resource") or [""])[0] or None
        device = registry.get(type_id) if type_id else None
        previous = self.agent.inventory.fitted.get(slot)
        previous_params = dict((previous.params or {}) if previous else {})
        params: dict = {}
        if device is not None:
            for parameter in device.parameters:
                raw = (form.get(f"p_{parameter.name}") or [""])[0]
                if parameter.type == "bool":
                    params[parameter.name] = raw == "on"
                elif parameter.type == "password":
                    # Blank means "leave it as it was", because blank is what
                    # the form always shows: the stored value is never rendered
                    # back, so an empty box cannot be read as "clear it" without
                    # wiping a working camera's password on every other save.
                    if raw:
                        params[parameter.name] = raw
                    elif previous and previous.type_id == type_id:
                        kept = previous_params.get(parameter.name)
                        if kept:
                            params[parameter.name] = kept
                elif parameter.type == "number" and raw != "":
                    params[parameter.name] = float(raw) if "." in raw else int(raw)
                elif raw != "":
                    params[parameter.name] = raw
        note = ""
        if device is not None and device.connection == "network":
            note = self._strip_url_credentials(form, params)
        camera_changed = slot == "camera" and (
            previous is None
            or previous.type_id != type_id
            or previous_params != params
        )
        self.agent.inventory.set_device(slot, type_id, params, resource)
        # A running stream is still showing the OLD camera — its source was
        # built from the driver that is about to be replaced, and `build_devices`
        # refuses to touch the slot while a stream runs, so the save reported
        # success while the demo test card kept going out. Stop it, and stop
        # *any* of it, not only the local preview: a platform viewer watching
        # the demo must not go on receiving the test card after the box has been
        # pointed at a real camera. It re-requests and gets the new source; the
        # few seconds of black is the honest cost of a swap. Only on an actual
        # change, so a no-op save does not interrupt a viewer for nothing —
        # `stop` before the rebuild, or the rebuild is the one that gets
        # deferred.
        stream = getattr(self.agent, "stream", None)
        if camera_changed and stream is not None:
            # Stop the stream still showing the old camera so it restarts on the
            # new one — and stop *any* of it, platform viewer included (see the
            # note where `camera_changed` is set).
            stream.stop("a camera change was saved")
        # Rebuild immediately: an installer who changes a port expects to see
        # within seconds whether the box can now talk to the thing. `force_camera`
        # on an actual change so the new driver is in place at once and cannot be
        # deferred — a platform viewer reconnecting a second later would restart
        # the stream, and a deferred rebuild waits on a stream that never ends,
        # which is how the swap "did not take effect" until a restart.
        self.agent.build_devices(force_camera=camera_changed)
        # **Give the new driver one sensing tick before judging it.**
        #
        # A receiver's presence is not knowable at construction. `describe()`
        # reports `present` only once the driver has actually produced
        # something — for ADS-B that is `status == "streaming"`, which needs a
        # parsed MAVLink frame — and this runs microseconds after
        # `build_devices()`, before the sensing loop has polled anything.
        #
        # So every save of a working receiver reported "saved, but not
        # detected", including the demo one, which cannot be absent because it
        # generates its own frames. The same race is visible at start-up, where
        # `devices.absent` is raised and then cleared about seventy
        # milliseconds later by the first tick.
        #
        # Waiting is the honest fix rather than softening the wording: the
        # question "is it there?" genuinely has no answer yet, and the loop
        # that answers it runs at 1 Hz. Bounded, and it returns the moment the
        # slot reports present, so a device that really is absent still says so
        # promptly.
        deadline = time.monotonic() + DETECT_GRACE_S
        while True:
            report = {r.slot: r for r in self.agent.inventory.report()}[slot]
            if report.status == "present" or not type_id:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.2)

        if not type_id:
            self.message = ("good", f"{slot}: nothing fitted.")
        elif report.status == "present":
            self.message = ("good", f"{slot}: {report.label} — detected.{note}")
        else:
            self.message = (
                "bad",
                f"{slot}: {report.label} saved, but not detected. "
                f"{report.detail}{note}",
            )
        return slot

    @staticmethod
    def _strip_url_credentials(form: dict, params: dict) -> str:
        """A pasted `rtsp://user:pass@…` never survives to the stored address.

        Camera vendors hand installers the whole line, credentials embedded,
        and this form's address box is where it gets pasted. Refusing it makes
        somebody retype a password on a phone on a roof; storing it as typed
        puts a secret in a plain-text field this page renders back on every
        visit — which is exactly the leak the password field was built never
        to have. So the URL is split: the address is stored without its
        userinfo, and the credentials move into the username and password
        parameters, which are stored once and never echoed. Values typed into
        those fields on the same save win over ones embedded in the URL — the
        separate field is the more deliberate act — and a URL-borne password
        replaces a stored one, because a freshly pasted URL means the paste is
        what the installer believes.

        Returns a sentence for the save message when anything moved.
        """
        address = str(params.get("address") or "")
        if not address:
            return ""
        cleaned, username, password = split_credentials(address)
        if cleaned == address:
            return ""
        params["address"] = cleaned
        if username and not (form.get("p_username") or [""])[0]:
            params["username"] = username
        if password and not (form.get("p_password") or [""])[0]:
            params["password"] = password
        if username or password:
            return (" The URL's credentials moved into the username and "
                    "password fields; the URL is stored without them.")
        return ""

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

    def _headers(self, handler, status: int, kind: str, length: int | None,
                 cookie: str | None, nonce: str | None = None,
                 extra: dict | None = None) -> None:
        handler.send_response(status)
        handler.send_header("Content-Type", kind)
        if length is None:
            # A body with no end: the live stream, which stops when the client
            # goes away.
            #
            # Chunked rather than `Connection: close`. Both are legal ways to
            # say "no length", but a media element is not a byte sink — it has
            # to decide it can demux what is arriving, and Chromium refuses a
            # progressive video body whose framing is end-of-connection with
            # MEDIA_ERR_SRC_NOT_SUPPORTED before it parses a single box. With
            # chunked framing each fragment arrives as a delimited unit and it
            # plays.
            handler.send_header("Transfer-Encoding", "chunked")
        else:
            handler.send_header("Content-Length", str(length))
        for name, value in (extra or {}).items():
            handler.send_header(name, value)
        # A setup page is state, and every one of these responses names devices,
        # a site and a station id. None of it belongs in a browser cache on a
        # subcontractor's laptop.
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("X-Frame-Options", "DENY")
        # same-origin, not no-referrer. Since Chrome 85 the Origin header on a
        # POST follows the referrer policy, so no-referrer redacts it to the
        # literal "null" even on a same-origin form - which _same_origin then
        # rightly refuses, and every browser login 403s while curl (no Origin
        # at all) sails through. same-origin sends nothing to any other site,
        # which for a page with no outbound links is the same privacy, and
        # lets the browser vouch for its own posts.
        handler.send_header("Referrer-Policy", "same-origin")
        # The nonce admits exactly one inline script — the one this response
        # itself carries — and is minted per response, so nothing injected
        # into rendered content can ever name it.
        csp = CSP + (f"; script-src 'nonce-{nonce}'" if nonce else "")
        handler.send_header("Content-Security-Policy", csp)
        if cookie:
            # No `Secure`: this is plain HTTP and always will be — see
            # setup_access.py on why a self-signed certificate here is theatre.
            # HttpOnly and SameSite=Strict are the two that do work.
            handler.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Strict",
            )
        handler.end_headers()

    def _send_frame(self, handler, cookie: str | None) -> None:
        """The newest frame the video publisher took, as the JPEG it is.

        A reader, never a trigger: this serves the publisher's cached frame
        and cannot start a capture, so it cannot contend for a sensor the
        live stream holds — while the stream runs, the cached frame simply
        ages, and the age is stated. Behind the same gate as every page, and
        `no-store` like every response (`_headers`), because the newest frame
        is the only one worth anything.
        """
        video = getattr(self.agent, "video", None)
        frame = getattr(video, "last_frame", None)
        if frame is None:
            return self._deny(handler, 404, "No frame yet.")
        age = video.frame_age_s() or 0.0
        self._headers(handler, 200, "image/jpeg", len(frame.jpeg), cookie,
                      extra={"X-Frame-Age": f"{age:.1f}"})
        handler.wfile.write(frame.jpeg)

    def _send_stream(self, handler, cookie: str | None) -> None:
        """The live encoder's fMP4, straight to a <video> on this page.

        The setup page is where somebody aims a camera, and a still refreshed
        every couple of seconds is the wrong instrument for that: the preview
        caps captures at one per two seconds and the page asks for one every
        two and a half, so the picture is three to five seconds behind the
        thing being pointed. You cannot aim with that.

        This is the *same encoder* the platform watches, never a second one —
        see `TeeUplink`. Two readers of one sensor is what wedged this camera
        before, and it is not a bug worth having twice.

        Written as a chunked response rather than through MediaSource: an
        `<video src>` pointed at a never-ending fMP4 body is a few lines of
        HTML against a few hundred of JavaScript, and this page's whole design
        is that it works without script.
        """
        stream = getattr(self.agent, "stream", None)
        if stream is None:
            return self._deny(handler, 503, "No stream on this station.")
        viewer = stream.attach_local()
        if viewer is None:
            return self._deny(
                handler, 503, stream.reason or "The camera will not stream.")

        # Nothing is committed until there is something to send.
        #
        # Sending the headers first and then waiting looked harmless and was
        # not: a cold start spawns an encoder and then waits for its first
        # keyframe, and a <video> given a 200 with no bytes behind it decides
        # the source is unsupported and gives up — `NotSupportedError: The
        # element has no supported sources`, before a single frame arrives.
        # The station meanwhile logged a perfectly healthy stream starting and
        # then stopping again because "the setup page stopped watching".
        first = None
        deadline = time.monotonic() + FIRST_FRAGMENT_WAIT_S
        while first is None and not viewer.closed:
            if time.monotonic() > deadline:
                stream.detach_local(viewer)
                return self._deny(
                    handler, 503,
                    stream.reason
                    or "The camera did not produce a picture in time.",
                )
            first = viewer.read(timeout=0.5)
            stream.renew_local()

        try:
            # No Content-Length: this body ends when the client goes away.
            self._headers(handler, 200, "video/mp4", None, cookie,
                          extra={"Cache-Control": "no-store"})
            _chunk(handler, first)
            while not viewer.closed:
                fragment = viewer.read(timeout=1.0)
                # Renewed on every pass, including the empty ones: the lease is
                # the same fail-closed mechanism the platform uses, and a
                # browser that vanished without closing its socket simply stops
                # renewing. The write below is what discovers that.
                stream.renew_local()
                if fragment is None:
                    continue
                _chunk(handler, fragment)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The tab was closed, which is the ordinary way this ends.
            pass
        finally:
            stream.detach_local(viewer)

    def _send_audio(self, handler, cookie: str | None) -> None:
        """The receiver's own PCM, live, straight off the box.

        **This exists to cut the transport out of an argument.** Airband audio
        that chops in the console has four suspects between the demodulator and
        the speaker — the Opus encoder, the broker relay, the platform's
        fan-out, and the browser's player — and no way to tell which. This is
        the demodulator's output, before any of them: if it chops here the
        fault is on the box, and if it is clean here it is not.

        Uncompressed on purpose. Opus is the first thing downstream and so the
        first thing that has to be ruled out, and PCM16 at 24 kHz is 384 kbit/s
        on a LAN cable to a laptop that is already on this network.

        Play it with anything:

            ffplay -autoexit http://<station>:8088/audio.wav
            vlc http://<station>:8088/audio.wav
            curl -s http://<station>:8088/audio.wav | aplay

        The length in the header is a lie, and deliberately so: this body ends
        when the client goes away, and every player treats an over-long RIFF
        size as "stream until it stops".
        """
        radio = getattr(self.agent, "radio", None)
        if radio is None:
            return self._deny(handler, 503, "No receiver on this station.")

        listener = radio.attach_listener()
        try:
            self._headers(handler, 200, "audio/wav", None, cookie,
                          extra={"Cache-Control": "no-store"})
            _chunk(handler, _wav_header(AUDIO_RATE))
            while True:
                try:
                    pcm = listener.get(timeout=1.0)
                except queue.Empty:
                    # The sensing loop feeds silence while the squelch is shut,
                    # so nothing arriving at all means the loop itself has
                    # stopped — worth discovering by the stream ending.
                    continue
                _chunk(handler, pcm)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The player was closed, which is the ordinary way this ends.
            pass
        finally:
            radio.detach_listener(listener)

    def _send_json(self, handler, payload: dict, cookie: str | None = None) -> None:
        body = json.dumps(payload, indent=2, default=str).encode()
        self._headers(handler, 200, "application/json", len(body), cookie)
        handler.wfile.write(body)

    def _send_html(self, handler, text: str, cookie: str | None = None,
                   status: int = 200, nonce: str | None = None) -> None:
        body = text.encode()
        self._headers(handler, status, "text/html; charset=utf-8", len(body),
                      cookie, nonce)
        handler.wfile.write(body)

    def _deny(self, handler, status: int, reason: str) -> None:
        body = f"{status} {html.escape(reason)}\n".encode()
        self._headers(handler, status, "text/plain; charset=utf-8", len(body), None)
        handler.wfile.write(body)

    def _redirect(self, handler, cookie: str | None = None,
                  location: str = "/") -> None:
        # Only ever one of our own page paths — see POST_HOME. Nothing from
        # the request reaches this header.
        handler.send_response(303)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.send_header("Cache-Control", "no-store")
        if cookie:
            handler.send_header(
                "Set-Cookie",
                f"{COOKIE_NAME}={cookie}; Path=/; HttpOnly; SameSite=Strict",
            )
        handler.end_headers()

    def _render_login(self, reason: str) -> str:
        """Deliberately says nothing about the station.

        Not the site name, not whether it is enrolled, not what is fitted.
        Somebody who has reached this page has reached a private network and
        nothing more, and there is no reason to confirm for them which box they
        have found before they can prove they are meant to be here.

        `reason` is non-empty only on the response to a failed attempt or a
        lockout; a first view shows no error, because there is none. The mark
        is a data: URI (gsu/brand.py) so the page stays self-contained.
        """
        from .brand import LOGO_DATA_URI

        return "".join([
            "<!doctype html><meta charset=utf-8>",
            "<meta name=viewport content='width=device-width,initial-scale=1'>",
            "<title>Ground station setup</title>",
            f"<link rel=icon href='{LOGO_DATA_URI}'>",
            f"<style>{STYLE}</style>",
            "<div class=login-wrap><div class=login-card>",
            f"<img class=brand-mark src='{LOGO_DATA_URI}' alt='' "
            "width=56 height=56>",
            "<div class=brand-word>PERCEPTA</div>",
            "<h1>Ground station setup</h1>",
            f"<div class='msg bad'>{html.escape(reason)}</div>" if reason else "",
            "<form method=post action='/login'>",
            "<label for=password>Setup password</label>",
            "<input id=password name=password type=password autocomplete='off' "
            "autofocus>",
            "<button type=submit>Sign in</button></form>",
            "<div class=muted>The login password can be found on this box's "
            "label, or with whoever provisioned it.</div>",
            "</div></div>",
        ])

    def render(self, session=None, page: str = "/", slot: str | None = None,
               nonce: str | None = None, chosen: str | None = None) -> str:
        from .brand import LOGO_DATA_URI

        state = self.agent.snapshot()
        csrf = self.gate.csrf_token(session)
        # The station's name leads the title so a bench with three of these open
        # in three tabs is told apart at the tab strip, not just inside the page.
        # Absent until enrolled, where there is no name to lead with.
        page_label = PAGES.get(page, "Summary")
        station_name = state.get("station")
        title = (f"{html.escape(station_name)} — {page_label}"
                 if station_name else f"Ground station — {page_label}")
        out = [
            "<!doctype html><meta charset=utf-8>",
            "<meta name=viewport content='width=device-width,initial-scale=1'>",
            f"<title>{title}</title>",
            f"<link rel=icon href='{LOGO_DATA_URI}'>",
            f"<style>{STYLE}</style>",
            self._nav(page, state, csrf, session),
            "<main>",
        ]
        banner = ""
        at, self.message_at = self.message_at, None
        if self.message:
            kind, text = self.message
            banner = f"<div class='msg {kind}'>{html.escape(text)}</div>"
            self.message = None
        # A message addressed to a dialog goes into that dialog, and nowhere
        # else — except on a page that does not render it, where the top of
        # the page is better than swallowing it.
        if banner and (at != "location" or page != "/connection"):
            out.append(banner)
            banner = ""

        if page == "/connection":
            out.append(self._section_enrol(state, csrf))
            out.append(self._section_location(state, csrf, banner))
            out.append(self._section_platform(state))
            out.append(self._section_security(state))
            out.append(self._section_reset(state, csrf))
            if nonce:
                out.append(self._location_script(nonce))
        elif page == "/devices":
            slot = slot if slot in registry.SLOTS else registry.SLOTS[0]
            out.append(self._section_devices(state, csrf, slot, chosen))
            if slot == "camera":
                out.append(self._section_camera(state))
            if nonce:
                out.append(self._devices_script(nonce))
        elif page == "/logging":
            out.append(self._section_events(state, csrf))
        else:
            out.append(self._page_summary(state))
        out.append("</main>")
        return "".join(out)

    def _nav(self, page: str, state: dict, csrf: str,
             session=None) -> str:
        """The one bar at the top: mark, title, which box, tabs, and the way out.

        The title used to be an `<h1>` inside `main`, which meant it scrolled
        away while the tabs — pinned — stayed. A heading that leaves and a bar
        that does not read as two separate things; they are one thing, and the
        bar is where a person looks to know what they are configuring.
        """
        from .brand import LOGO_DATA_URI

        out = [
            "<header class=topbar>",
            "<div class=topbar-brand>",
            f"<img class=topbar-mark src='{LOGO_DATA_URI}' alt='' "
            "width=26 height=26>",
            "<span class=topbar-title>Ground station</span>",
            "</div>",
        ]
        # Which box this is, centred, because on a bench with three of them
        # open in three tabs the tab strip looks identical on every one.
        # Absent until enrolled: there is no name to show, and a placeholder
        # would be a worse answer than the space.
        if state.get("station"):
            out.append(
                f"<span class=topbar-station>{html.escape(state['station'])}</span>"
            )
        else:
            # The middle column still has to exist, or the tabs slide into it
            # and the bar's three parts stop lining up between pages.
            out.append("<span class=topbar-station></span>")
        out.append("<div class=topbar-right><nav class='tabs pagetabs'>")
        for path, label in PAGES.items():
            active = " class=active" if path == page else ""
            out.append(f"<a href='{path}'{active}>{html.escape(label)}</a>")
        out.append("</nav>")
        # A sibling of the tab strip, not a member of it. Inside, it inherited
        # .tabs' vertical padding on top of its own and set the bar's height —
        # which --nav-h has to describe exactly, because the slot strip pins to
        # it. It is also not a page tab: it is a form, because signing out
        # changes state and a GET that ends a session is one a prefetcher can
        # trigger.
        if session is not None and session.scope == "local":
            out.append(
                "<form method=post action='/logout' class=topbar-out>"
                + self._csrf_field(csrf)
                + "<button type=submit>Sign out</button></form>"
            )
        out.append("</div></header>")
        return "".join(out)

    @staticmethod
    def _csrf_field(csrf: str) -> str:
        return f"<input type=hidden name=csrf value='{html.escape(csrf)}'>"

    def _page_summary(self, state: dict) -> str:
        """The landing page: what an installer checks before leaving site,
        worst news first, and nothing they can edit — every fix lives on the
        page whose tab names the thing that is wrong."""
        out = []
        if state["enrolled"]:
            out.append(f"<p class=sub>{html.escape(state['station'] or '')}</p>")
        else:
            out.append(
                "<p class=sub>Not set up yet — "
                "<a href='/connection'>enter the enrolment code</a>.</p>"
            )
        # Two conditions are dropped here and only here, because the Slots
        # table further down this same page already says exactly what they say:
        # `devices.absent` names the selected devices that are not answering,
        # and `telemetry.unsourced` names the streams that have no source —
        # which on this page is the same list, rendered as pills, per slot,
        # next to the device each one is about. Printing them again as a
        # sentence at the top was the same fact twice on one screen.
        #
        # Filtered rather than the box removed: the rest of this list has
        # nowhere else to appear. A credential that is failing to renew is
        # invisible until it expires and then costs a site visit, and a clock
        # that is refusing enrolment, a demoted setup page and a rejected
        # uplink are all things no slot pill will ever show. They still go
        # out in telemetry unchanged — this is display only.
        DUPLICATED_BY_SLOTS = {"devices.absent", "telemetry.unsourced"}
        attention = [
            condition for condition in state["health"]
            if condition["id"] not in DUPLICATED_BY_SLOTS
        ]
        if attention:
            out.append("<div class=card><div class=k>Needs attention</div><ul>")
            for condition in attention:
                css = "bad" if condition["severity"] == "critical" else "warn"
                out.append(
                    f"<li class={css}>{html.escape(condition['id'])}: "
                    f"{html.escape(condition['detail'])}</li>"
                )
            out.append("</ul></div>")
        clock_state = state.get("clock_source") or {}
        rows = [
            ("Enrolled", "yes" if state["enrolled"] else "not yet",
             "ok" if state["enrolled"] else "warn"),
            ("Link to the platform", "up" if state["link"] else "down",
             "ok" if state["link"] else "bad"),
            ("Telemetry sent", f"{state['published']} frames", "ok"),
            ("Dropped while offline", f"{state['dropped']} frames",
             "ok" if not state["dropped"] else "warn"),
            ("Station clock", state["clock"], "ok"),
            ("Clock kept by", self._clock_wording(clock_state),
             self._clock_class(clock_state)),
            # Read-only here like everything else on this page — the form is on
            # Connection. It earns a row because an unset position is a fault
            # an installer can still fix while on site and cannot see from
            # anywhere else: every range and bearing this station reports is
            # computed from it.
            ("Location", *self._position_wording(state.get("position") or {})),
        ]
        out.append("<div class=card>")
        for label, value, css in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class='{css}'>{html.escape(str(value))}</span></div>"
            )
        out.append("</div>")
        # One line per slot: the same pill the Devices page shows, without the
        # forms. Intent (the label) and fact (the pill), still never merged.
        out.append("<h2>Slots</h2><div class='card slot-grid'>")
        by_slot = {report["slot"]: report for report in state["devices"]}
        for slot in registry.SLOTS:
            report = by_slot[slot]
            css, wording = STATUS_PILL.get(report["status"], ("off", report["status"]))
            # The slot name links to its tab. Summary is where somebody
            # notices a slot is wrong and Devices is the only place to fix it,
            # so the thing they are looking at is the way there — rather than
            # reading a row, going to Devices, and finding the slot again.
            # Only the name is a link; the pill beside it is a status, and
            # making a whole row clickable hides where the target is.
            label = SLOT_LABELS.get(slot, slot.title())
            # Three columns rather than a flex row with the device and its pill
            # bundled into one span: bundled, the pills landed wherever each
            # device name happened to end, so a column of statuses an installer
            # scans down was a ragged edge. Grid gives the pill its own column
            # and every one starts at the same x.
            badge = (
                "<span class='pill demo'>DEMO</span>"
                if report.get("simulated") else ""
            )
            out.append(
                "<div class=slot-row>"
                f"<a class=slot-link href='/devices?slot={slot}'>"
                f"{html.escape(label)}</a>"
                f"<span class=slot-device>{html.escape(report['label'])}{badge}</span>"
                f"<span class='pill {css}'>{html.escape(wording)}</span></div>"
            )
        # No trailing "selection and parameters are on the Devices page" line.
        # Devices is a tab at the top of every page, so it was navigation
        # advice for a journey already offered, and prose of exactly the kind
        # this page is supposed to be free of.
        out.append("</div>")
        return "".join(out)

    def _code_form(self, csrf: str, *, autofocus: bool, label: str,
                   button: str) -> str:
        # In a .field like every other row on the page, rather than the
        # label-<br>-input it was: this is the one form an installer sees
        # first, and it was the one that lined up with nothing.
        focus = " autofocus" if autofocus else ""
        return (
            "<div class=card><form method=post action='/enrol'>"
            + self._csrf_field(csrf) +
            "<div class=field>"
            f"<label for=token>{label}</label>"
            "<input id=token class=code name=token type=text autocomplete=off "
            f"placeholder='XXXX-XXXX-XXXX'{focus}>"
            "</div>"
            f"<div class=field><button type=submit>{button}</button></div>"
            "</form></div>"
        )

    def _has_usable_credential(self, state: dict) -> bool:
        """Whether this box holds a credential the platform still honours.

        `state["enrolled"]` only means a credential *file* exists. The platform
        can revoke it, and — this is the honest limit — the box does not learn
        that at the moment it happens. It finds out when a renewal is next
        refused, which raises `credential.revoked`, or when the credential
        expires. Until one of those, a box that has been cut off still believes
        it is enrolled, and no amount of code on this side can know otherwise.

        So "enrolled" for the purpose of the code field means *has a working
        credential*: a revoked or expired one drops the box back to needing a
        code, which is the same state as a box that never had one. There is no
        third "re-enrol" state, because from the box's side there is no third
        thing to be.
        """
        if not state["enrolled"]:
            return False
        credential = getattr(
            getattr(self.agent, "enrolment", None), "credential", None)
        if credential is None or credential.expired():
            return False
        return not any(
            condition.get("id") == "credential.revoked"
            for condition in state.get("health", [])
        )

    def _section_enrol(self, state: dict, csrf: str) -> str:
        """Either a line saying where the box is enrolled, or a field for a code.

        A station is enrolled or it is not, and the field shows only when it is
        not — where "not" includes a credential the box has found the platform
        no longer honours (see `_has_usable_credential`). There is no separate
        re-enrol: a revoked box is simply a box that needs setting up again, and
        the platform tells the two apart, accepting a code issued after
        enrolment and refusing a stale one that predates it.
        """
        if self._has_usable_credential(state):
            # The organisation is echoed back by the platform and shown here
            # because a code carries no visible clue whose it is. The mistake
            # this catches is a contractor commissioning a box into the
            # previous customer's tenant, which otherwise surfaces as data
            # appearing in the wrong console days later.
            org = (state.get("position") or {}).get("organization")
            where = f" · {html.escape(org)}" if org else ""
            return (
                f"<p class=sub>Enrolled as {html.escape(state['station'] or '')}"
                f"{where}.</p>"
            )
        note = (
            "<p class=sub>This station's credential is no longer valid. Enter a "
            "new code to set it up again.</p>"
            if state["enrolled"] else
            "<p class=sub>Not set up yet.</p>"
        )
        return note + self._code_form(
            csrf, autofocus=True,
            label="Enter the code you were given",
            button="Set this station up",
        )

    def _section_location(self, state: dict, csrf: str, banner: str = "") -> str:
        """Where this box is, set by whoever is standing at it.

        **Editable, where it used to be read-only.** The old rule froze the
        position at enrolment on the reasoning that a box which has moved is
        recommissioned — but that left an enrolled box with no way to be given a
        position at all, and the revoke-and-re-enrol workaround reissued the
        same coordinates it already had. The person at the mast knows where the
        box is; this is the field they put it in. It writes to the station's own
        config, which `effective_position` already prefers over anything the
        platform issued.

        Latitude, longitude and elevation are one form. Elevation is recorded
        and reported to the platform but drives nothing on the box — the ADS-B
        altitude correction that once used it has been removed.

        The platform's own words for the coordinates it issued are shown beside
        the fields, unedited, so an installer can check the box against the site
        they are standing at rather than against a bare pair of decimals.
        """
        position = state.get("position") or {}

        def field(name: str, label: str, value, placeholder: str,
                  step: str, lo: str, hi: str) -> str:
            shown = "" if value is None else html.escape(str(value))
            return (
                f"<div class=field><label for={name}>{label}</label>"
                f"<input id={name} name={name} type=number inputmode=decimal "
                f"step={step} min={lo} max={hi} value='{shown}' "
                f"placeholder='{placeholder}'></div>"
            )

        out = ["<h2>Where this box is</h2><div class=card>"]
        out.append(banner)
        out.append(f"<form method=post action='/location'>{self._csrf_field(csrf)}")
        # Prefilled with what the box is actually using, whichever source that
        # came from, so an installer edits from the current position rather than
        # a blank field — and saving what is shown simply confirms it locally.
        out.append(field("latitude", "Latitude", position.get("latitude"),
                         "-43.48972", "any", "-90", "90"))
        out.append(field("longitude", "Longitude", position.get("longitude"),
                         "172.53194", "any", "-180", "180"))
        out.append(field("elevation_m", "Elevation (m)", position.get("elevation_m"),
                         "metres above sea level", "any", "-500", "100000"))
        out.append(self._location_map())
        out.append("<div class=field><button type=submit>Save</button></div></form>")

        # What the platform issued, for comparison only. Named as the platform's
        # so it is never mistaken for what the box is using — a station running
        # a local position and one still on the platform's look identical
        # otherwise, and only the local one was put there by somebody who was
        # at the site.
        where, _ = self._position_wording(position)
        out.append(
            "<div class=muted>Platform's position: "
            f"{html.escape(where)}</div></div>"
        )
        return "".join(out)

    @staticmethod
    def _location_map() -> str:
        """A slippy map under the coordinate fields.

        Progressive enhancement, and hidden until its script runs: a browser
        with no script — or no way to reach the tile server — is left with the
        latitude/longitude fields exactly as they were. Tiles are fetched by the
        operator's browser straight from OpenStreetMap; the station never
        proxies them, which would spend the metered uplink on map tiles.
        """
        return (
            "<style>"
            ".locmap{margin:.2rem 0 .5rem}"
            ".locmap-hint{font-size:.8rem;color:var(--muted);margin:.1rem 0 .35rem}"
            ".locmap-view{position:relative;width:100%;height:230px;overflow:hidden;"
            "border:1px solid var(--line);border-radius:.4rem;background:var(--panel);"
            "touch-action:none;cursor:grab;user-select:none}"
            ".locmap-view.grabbing{cursor:grabbing}"
            ".locmap-tiles{position:absolute;inset:0}"
            ".locmap-tiles img{position:absolute;width:256px;height:256px;pointer-events:none}"
            ".locmap-pin{position:absolute;left:50%;top:50%;width:26px;height:26px;"
            "transform:translate(-50%,-100%);pointer-events:none;z-index:3}"
            ".locmap-pin svg{display:block;filter:drop-shadow(0 1px 2px rgba(0,0,0,.6))}"
            ".locmap-zoom{position:absolute;right:.5rem;top:.5rem;z-index:4;"
            "display:flex;flex-direction:column;gap:.3rem}"
            ".locmap-zoom button{width:2rem;height:2rem;font-size:1.2rem;line-height:1;"
            "background:rgba(7,11,15,.85);color:var(--text);border:1px solid var(--line);"
            "border-radius:.3rem;cursor:pointer}"
            ".locmap-attr{position:absolute;right:0;bottom:0;z-index:4;font-size:10px;"
            "background:rgba(7,11,15,.7);color:var(--muted);padding:1px 4px;"
            "border-top-left-radius:.3rem}"
            ".locmap-attr a{color:var(--muted)}"
            "</style>"
            "<div class=locmap id=locmap hidden>"
            "<div class=locmap-hint>Drag the map so the pin sits on the station, "
            "or tap a spot — the latitude and longitude above follow the pin.</div>"
            "<div class=locmap-view id=locmapview>"
            "<div class=locmap-tiles id=locmaptiles></div>"
            "<div class=locmap-pin>"
            "<svg width=26 height=26 viewBox='0 0 24 24'>"
            "<path fill='#e6484d' stroke='#fff' stroke-width='1.2' "
            "d='M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z'/>"
            "<circle cx=12 cy=9 r=2.6 fill='#fff'/></svg></div>"
            "<div class=locmap-zoom>"
            "<button type=button id=zin aria-label='Zoom in'>+</button>"
            "<button type=button id=zout aria-label='Zoom out'>−</button></div>"
            "<div class=locmap-attr>© <a href='https://www.openstreetmap.org/copyright' "
            "target=_blank rel=noopener>OpenStreetMap</a></div>"
            "</div></div>"
        )

    @staticmethod
    def _location_script(nonce: str) -> str:
        """The slippy map's behaviour, admitted by the page's nonce.

        Standard Web Mercator tile math against OpenStreetMap. The pin is fixed
        at the map's centre — the point being chosen is always the middle — so
        drag the map under it or tap to drop it, and the latitude/longitude
        fields follow. It only writes the fields on a real move, so simply
        opening the page leaves a typed-in coordinate untouched.
        """
        script = """
"use strict";
(function () {
  var box = document.getElementById('locmap');
  var view = document.getElementById('locmapview');
  var layer = document.getElementById('locmaptiles');
  var latEl = document.getElementById('latitude');
  var lonEl = document.getElementById('longitude');
  if (!box || !view || !layer || !latEl || !lonEl) return;
  box.hidden = false;

  var TILE = 256, MINZ = 2, MAXZ = 18;
  function lon2x(lon, z) { return (lon + 180) / 360 * Math.pow(2, z); }
  function lat2y(lat, z) {
    var r = lat * Math.PI / 180;
    return (1 - Math.asinh(Math.tan(r)) / Math.PI) / 2 * Math.pow(2, z);
  }
  function x2lon(x, z) { return x / Math.pow(2, z) * 360 - 180; }
  function y2lat(y, z) {
    var n = Math.PI - 2 * Math.PI * y / Math.pow(2, z);
    return 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
  }
  function clampLat(v) { return Math.max(-85.0511, Math.min(85.0511, v)); }

  var z = 13;
  var cx = lon2x(parseFloat(lonEl.value) || 172.5, z);
  var cy = lat2y(clampLat(parseFloat(latEl.value) || -43.5), z);

  function draw() {
    var w = view.clientWidth, h = view.clientHeight, n = Math.pow(2, z);
    var ox = w / 2 - cx * TILE, oy = h / 2 - cy * TILE;
    var minTX = Math.floor(cx - w / 2 / TILE) - 1, maxTX = Math.floor(cx + w / 2 / TILE) + 1;
    var minTY = Math.max(0, Math.floor(cy - h / 2 / TILE) - 1);
    var maxTY = Math.min(n - 1, Math.floor(cy + h / 2 / TILE) + 1);
    var s = '';
    for (var ty = minTY; ty <= maxTY; ty++) {
      for (var tx = minTX; tx <= maxTX; tx++) {
        var wx = ((tx % n) + n) % n;
        var sub = 'abc'[((wx + ty) % 3 + 3) % 3];
        s += '<img alt="" src="https://' + sub + '.tile.openstreetmap.org/' + z + '/' + wx + '/' + ty + '.png"'
           + ' style="left:' + Math.round(ox + tx * TILE) + 'px;top:' + Math.round(oy + ty * TILE) + 'px">';
      }
    }
    layer.innerHTML = s;
  }
  function commit() {
    latEl.value = clampLat(y2lat(cy, z)).toFixed(5);
    lonEl.value = x2lon(cx, z).toFixed(5);
  }

  var dragging = false, moved = 0, lastX = 0, lastY = 0;
  view.addEventListener('pointerdown', function (e) {
    dragging = true; moved = 0; lastX = e.clientX; lastY = e.clientY;
    view.classList.add('grabbing'); view.setPointerCapture(e.pointerId);
  });
  view.addEventListener('pointermove', function (e) {
    if (!dragging) return;
    var dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY; moved += Math.abs(dx) + Math.abs(dy);
    cx -= dx / TILE; cy -= dy / TILE; draw();
  });
  view.addEventListener('pointerup', function (e) {
    if (!dragging) return;
    dragging = false; view.classList.remove('grabbing');
    if (moved < 6) {
      var r = view.getBoundingClientRect();
      cx += (e.clientX - r.left - r.width / 2) / TILE;
      cy += (e.clientY - r.top - r.height / 2) / TILE;
      draw();
    }
    commit();
  });
  view.addEventListener('pointercancel', function () {
    dragging = false; view.classList.remove('grabbing');
  });

  function zoom(dz) {
    var nz = Math.max(MINZ, Math.min(MAXZ, z + dz));
    if (nz === z) return;
    var lon = x2lon(cx, z), lat = y2lat(cy, z);
    z = nz; cx = lon2x(lon, z); cy = lat2y(lat, z); draw();
  }
  document.getElementById('zin').addEventListener('click', function () { zoom(1); });
  document.getElementById('zout').addEventListener('click', function () { zoom(-1); });

  function fromFields() {
    var lat = parseFloat(latEl.value), lon = parseFloat(lonEl.value);
    if (isFinite(lat) && isFinite(lon)) { cx = lon2x(lon, z); cy = lat2y(clampLat(lat), z); draw(); }
  }
  latEl.addEventListener('change', fromFields);
  lonEl.addEventListener('change', fromFields);

  window.addEventListener('resize', draw);
  draw();
})();
"""
        return f"<script nonce='{nonce}'>{script}</script>"

    @staticmethod
    def _position_wording(position: dict) -> tuple[str, str]:
        """The position and how much to trust it, in one phrase.

        A station using its own position and one still using the platform's
        look identical otherwise, and only the first has been confirmed by
        somebody who was there — so the fallback is marked, not merged.
        """
        where = (f"{_degrees(position.get('latitude'))}, "
                 f"{_degrees(position.get('longitude'))}")
        source = position.get("source") or ""
        locality = position.get("locality")
        if source == "enrolment":
            # The normal case now: settled when the code was redeemed. The
            # locality is what lets somebody standing at the site tell that the
            # coordinates are this site rather than the last one commissioned.
            return (f"{where} — {locality}" if locality else where), "ok"
        if source == "station":
            # A position configured locally, which only boxes enrolled before
            # position moved to enrolment will have. Stated plainly: it is the
            # position this station is using, and which of two mechanisms put
            # it there is not something a person at the site can act on.
            return where, "ok"
        return "not set", "warn"

    def _section_reset(self, state: dict, csrf: str) -> str:
        """The way back to a blank box.

        Last on the page, after everything it clears, because it is the most
        destructive control the station has and nothing below it should be
        reachable by an accidental scroll-and-click.

        Two clicks and no typed confirmation: this page answers only on the
        local network, behind a password, inside a time-boxed window, so
        anybody who can see this is at the hardware intending to reprovision
        it. `:target` gives the second click without script — the button is a
        link to `#reset`, and only then is there a form to submit.
        """
        out = [
            "<h2>Reset</h2><div class=card>",
            "<div class=row><span class=k>Clears</span>"
            "<span>credential, pinned CA, devices, settings, events</span></div>",
        ]
        if state["enrolled"]:
            out.append(
                "<div class=row><span class=k>After this</span>"
                "<span class=warn>this box needs a new enrolment code</span></div>"
            )
        # The confirm block is emitted BEFORE the trigger, and the CSS hides the
        # trigger once the block is open (`#reset:target ~ #reset-trigger`). The
        # two are only siblings the one way round: `~` reaches forward, so the
        # thing to hide has to follow the thing that opens. Without this the
        # first "Reset station" button stayed on screen beside the confirm's own
        # "Erase everything" button — two red controls at once, and no way to
        # tell the arming click from the committing one.
        out.append(
            "<div id=reset class=confirm>"
            f"<form method=post action='/reset'>{self._csrf_field(csrf)}"
            "<input type=hidden name=confirm value='yes'>"
            "<p class=sub>This cannot be undone. The box will need a new "
            "enrolment code before it can publish again.</p>"
            "<div class=field><button type=submit class=danger>"
            "Erase everything on this box</button>"
            "<a class='btn quiet' href='#'>Cancel</a></div></form></div>"
        )
        out.append(
            "<div class=field id=reset-trigger>"
            "<a class='btn danger' href='#reset'>Reset station</a></div>"
        )
        out.append("</div>")
        return "".join(out)

    def _section_security(self, state: dict) -> str:
        """Whether each link is encrypted and verified, and whether the
        credential behind them is still good — in the same list a technician
        checks before leaving site. Separate rows because they have separate
        trust roots, and none of it a question that should need a packet
        capture to answer."""
        security = state.get("security") or {}
        trust = security.get("trust") or {}
        rows = [
            self._security_row(security, trust),
            self._api_security_row(state, security),
            self._credential_row(),
        ]
        out = ["<h2>Security</h2><div class=card>"]
        for label, value, css in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class='{css}'>{html.escape(str(value))}</span></div>"
            )
        out.append("</div>")
        return "".join(out)

    def _credential_row(self) -> tuple[str, str, str]:
        """The station's own credential, which quietly renews itself — and
        which, when renewal is quietly failing, gives weeks of warning that
        only counts if it is written somewhere somebody looks."""
        enrolment = getattr(self.agent, "enrolment", None)
        credential = getattr(enrolment, "credential", None)
        if credential is None:
            return ("Broker credential", "none until this station enrols", "warn")
        when = credential.expires_at.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        if credential.expired():
            return ("Broker credential",
                    f"EXPIRED {when} — this station must re-enrol", "bad")
        if credential.due_for_renewal():
            return ("Broker credential",
                    f"renewal due now; expires {when}", "warn")
        return ("Broker credential", f"expires {when}, renews itself", "ok")

    def _section_platform(self, state: dict) -> str:
        """The addresses, read-only and said to be read-only.

        There is one platform and its address is fixed in the environment file.
        An installer's job here is to confirm the box is pointed at the right
        one before they leave, which is a different job from being able to
        change it — and one that is worth doing, because an address that is
        wrong produces a station that looks like it has no signal.
        """
        security = state.get("security") or {}
        rows = [
            ("Platform API", state.get("platform") or "not set"),
            ("Broker", security.get("broker_url")
             or "not known until this station enrols"),
            ("Contract", state.get("contract_version") or "—"),
            ("Station id", state.get("station_id") or "not enrolled"),
        ]
        out = ["<h2>Where this box talks</h2><div class=card>"]
        for label, value in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class=fixed>{html.escape(str(value))}</span></div>"
            )
        # No paragraph explaining why these are read-only. They are shown
        # without a control beside them, which says it; the reasoning — one
        # platform, and a URL retypable on site is a station that enrols
        # against nothing and reports no error anybody sees — belongs here.
        # Set in the station's environment file: GSU_PLATFORM_URL,
        # GSU_BROKER_URL.
        out.append("</div>")
        return "".join(out)

    def _section_camera(self, state: dict) -> str:
        """Why the camera is doing what it is doing, without an SSH session.

        The question this exists to answer used to be "why is the camera slow"
        and is now "who has the camera". Busy and broken were indistinguishable
        from here for the whole of the wedge hunt: a stream delivering nothing
        and a stream that could not get the sensor looked identical. The lease
        holder is a fact the station knows and nobody could see.
        """
        video = state.get("video") or {}
        camera = video.get("camera") or {}
        stream = video.get("stream") or {}
        sensor = video.get("sensor") or {}
        reason = camera.get("backend_reason") or ""

        holder = sensor.get("holder")
        held_for = sensor.get("held_for_s")
        holds = f"{holder} for {held_for:.0f}s" if holder and held_for else (holder or "nobody")

        rows = [
            # Not a warning either way. A held sensor is what a working live
            # stream looks like — the lease in camera/ownership.py exists so
            # that one reader has it — and amber on ordinary operation is how a
            # page teaches people to stop reading amber. The value names the
            # holder and how long it has held; that is the diagnostic.
            ("Camera held by", holds, "ok"),
            ("Snapshots", "removed", "off"),
            ("Preview", f"{video.get('preview_frames', 0)} frames, "
                        f"{video.get('preview_refused', 0)} refused", "ok"),
            ("Live stream", stream.get("state") or "idle", "ok"),
        ]
        delivered = stream.get("delivered") or {}
        if delivered:
            # What the camera actually sent, beside what the site asked for.
            # A wrong size or rate is invisible until they are side by side,
            # and both were wrong for a whole evening.
            requested = stream.get("requested") or {}
            rows.append((
                "Delivering",
                f"{delivered.get('width')}x{delivered.get('height')} at "
                f"{delivered.get('fps')} fps"
                + (f" (asked for {requested.get('width')}x{requested.get('height')}"
                   f" at {requested.get('fps')})" if requested else ""),
                "ok",
            ))
        if camera.get("backend"):
            # `rpicam` is the CSI camera and `ffmpeg` is a network camera, and
            # both are configurations this station supports on purpose — the
            # RTSP path exists because an owner asked for it. Testing for
            # `rpicam` alone put a permanent amber on every correctly working
            # network camera, which is the same false alarm as the clock: a
            # legitimate configuration rendered as a fault. Only `none` — no
            # capture tool found at all — is one.
            rows.insert(0, ("Capture path", camera["backend"],
                            "warn" if camera["backend"] == "none" else "ok"))
        out = ["<h2>Camera</h2><div class=card>"]
        for label, value, css in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class='{css}'>{html.escape(str(value))}</span></div>"
            )
        if reason:
            # A working camera explaining itself is a note; only one that is
            # not working is a warning.
            css = "warn" if camera.get("backend") in (None, "none") else "muted"
            out.append(f"<div class='{css}'>{html.escape(reason)}</div>")
        if video.get("reason"):
            out.append(
                f"<div class=warn>{html.escape(str(video['reason']))}</div>"
            )
        out.append("</div>")
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
            return ("Broker security",
                    "REFUSED — the broker's certificate did not verify", "bad")
        if not security.get("publishing") and security.get("broker_url"):
            return ("Broker security", "REFUSED — see the conditions below", "bad")
        if security.get("broker_tls") is None:
            return ("Broker security", "no broker yet", "warn")
        if not security.get("broker_tls"):
            return ("Broker security", "PLAINTEXT — development only", "bad")
        if trust.get("mode") == "system":
            # Not a warning: the broker moved onto the platform's own 443 and is
            # now the same TLS endpoint as the API, behind the same public
            # certificate. `mode == "system"` is only ever reached because the
            # platform stated `ca_mode: "system"` at enrolment — `resolve_broker`
            # never falls back to it — so this is the deliberate, correct end
            # state for a proxy-terminated broker, with no private CA to pin and
            # none coming. Green, worded exactly as the API row: trust follows
            # the endpoint, not the role (see tls.resolve_broker).
            return ("Broker security", "TLS, public certificate", "ok")
        fingerprint = (trust.get("fingerprint") or "")[:23]
        return ("Broker security", f"TLS, CA pinned {fingerprint}…", "ok")

    @staticmethod
    def _api_security_row(state: dict, security: dict) -> tuple[str, str, str]:
        """The other half, which has a different trust root and different fixes.

        Shown even though the API is only used at enrolment and renewal: a
        station whose renewal is quietly failing on a certificate has weeks
        before anyone finds out the hard way, and this is where somebody would
        look first.
        """
        api = security.get("api_trust") or {}
        if not security.get("platform_tls"):
            return ("Platform API security", "PLAINTEXT — development only", "bad")
        if api.get("mode") == "system":
            return ("Platform API security", "TLS, public certificate", "ok")
        if not api.get("fingerprint"):
            return ("Platform API security", "pinning asked for, CA unusable", "bad")
        return ("Platform API security",
                f"TLS, CA pinned {(api.get('fingerprint') or '')[:23]}…", "ok")

    @staticmethod
    def _clock_wording(state: dict) -> str:
        """What is keeping this clock, in words that do not outrun the evidence.

        `rtc-only` used to read "a hardware RTC, not synced". It is reached when
        every probe in `clock.py` came back with nothing — which is *don't
        know*, not *not synced*, and the difference decides whether somebody
        drives out to a site. It is also the ordinary state of a container that
        cannot see the host's timesyncd, so the false alarm was permanent rather
        than occasional.
        """
        source = state.get("source", "unknown")
        wording = {
            "gps": "GPS", "ntp": "NTP",
            "rtc-only": "a hardware RTC; cannot tell what is disciplining it",
            "none": "nothing — the time is a guess",
            "unknown": "cannot tell",
        }.get(source, source)
        return wording if state.get("rtc_present") else f"{wording} (no RTC fitted)"

    @staticmethod
    def _clock_class(state: dict) -> str:
        """`synchronised` is three-valued and must be shown that way.

        True is disciplined, False is a clock nobody is keeping, and None is the
        station admitting it could not find out. Collapsing None into the same
        amber as False makes "I don't know" indistinguishable from "I know it is
        wrong", on the one page an installer reads to decide whether the station
        can be trusted.
        """
        synchronised = state.get("synchronised")
        if synchronised is True:
            return "ok"
        if synchronised is False:
            return "warn"
        return "unknown"

    @staticmethod
    def _slot_tabs(active: str) -> str:
        """One sub-tab per slot, same tab language as the page strip. Links,
        not buttons: they must work with no script, and each is a GET."""
        out = ["<nav class='tabs subtabs'>"]
        for slot in registry.SLOTS:
            css = " class=active" if slot == active else ""
            label = SLOT_LABELS.get(slot, slot.title())
            out.append(f"<a href='/devices?slot={slot}'{css}>{html.escape(label)}</a>")
        out.append("</nav>")
        return "".join(out)

    def _section_devices(self, state: dict, csrf: str, slot: str,
                         chosen: str | None = None) -> str:
        # Rendering rule for this page, an owner requirement: labels and short
        # constraints only. Everything that used to be explained on screen —
        # why ports are assigned by-id, why a tuner serves one band, what a
        # device cannot measure and why that matters — lives in the registry
        # and in code comments. The page states facts; the reasoning is here.
        out = ["<h2>What is fitted</h2>", self._slot_tabs(slot)]
        if state["conflicts"]:
            # Not prose: these are faults, in the words an operator acts on.
            out.append("<div class='msg bad'><ul>")
            for conflict in state["conflicts"]:
                out.append(f"<li>{html.escape(conflict)}</li>")
            out.append("</ul></div>")

        resources = state["resources"]
        report = {r["slot"]: r for r in state["devices"]}[slot]
        entry = self.agent.inventory.fitted.get(slot)
        css, wording = STATUS_PILL.get(report["status"], ("off", report["status"]))
        # What the form is showing settings for. The query wins so the page can
        # preview a device before it is saved; anything unrecognised falls back
        # to what is actually stored, so a hand-edited URL cannot render a form
        # for a device this build has never heard of.
        chosen_id = entry.type_id if entry else ""
        if chosen is not None and (chosen == "" or registry.get(chosen) is not None):
            if chosen == "" or registry.get(chosen).slot == slot:
                chosen_id = chosen

        out.append("<div class=card>")
        # The pill carries its slot and an id so the poll can refresh it: an
        # inline apply never reloads, so without this the pill would keep saying
        # "Disconnected" beside a status line that already read "detected".
        out.append(
            "<div class=slot-head><strong>"
            f"{html.escape(SLOT_LABELS.get(slot, slot.title()))}</strong>"
            f"<span id=slot-pill data-pill-slot='{slot}' class='pill {css}'>"
            f"{html.escape(wording)}</span></div>"
        )
        # No "selected:" or "found:" lines. Both were the same fact twice on
        # one card: the dropdown below shows what is selected, and the pill
        # beside the heading says whether it is answering. Intent and detection
        # are still kept apart — that is what the dropdown and the pill *are* —
        # they are simply not also narrated.

        # Choosing a device is a *navigation*, not a write, and it has its own
        # form for that reason.
        #
        # With one form, picking a different device left the previous device's
        # parameter fields on screen — they are rendered from the stored type —
        # so Save posted a serial baud to an ONVIF camera and came back with
        # errors about fields nobody had been given the chance to set. The
        # order was wrong: you cannot fill in a device's settings before the
        # page knows which device you mean.
        #
        # So the select re-renders the page for the type it names, storing
        # nothing. The nonce'd script submits it on change; without script the
        # button beside it does the same thing, which is why it is a real form
        # and not an onchange handler.
        out.append(
            f"<form method=get action='/devices' class=pick data-pick>"
            f"<input type=hidden name=slot value='{slot}'>"
        )
        out.append("<div class=field><label for=type>Device</label>"
                   "<select id=type name=type>")
        out.append(
            f"<option value=''{' selected' if not chosen_id else ''}>"
            "— not fitted —</option>"
        )
        for device in registry.by_slot(slot):
            selected = " selected" if chosen_id == device.id else ""
            suffix = "" if device.driver else "  (no driver in this build)"
            out.append(
                f"<option value='{device.id}'{selected}>"
                f"{html.escape(device.label)}{suffix}</option>"
            )
        out.append(
            "</select><button type=submit class=pick-go>Change</button></div></form>"
        )

        # `data-changed` when the device on screen is not the device stored.
        #
        # Picking a device only re-renders — that is the whole point of the
        # split above — so nothing on the form looks changed: the type is in a
        # hidden field and every visible field equals its freshly-rendered
        # default. This marker is how the script knows a fresh pick is sitting
        # there uncommitted, and with no Save button it is what drives the
        # commit: the apply loop reads `data-changed` and, for a device with
        # nothing to fill in (an un-fit slot, a param-less device), commits it on
        # load — which is what makes un-fitting a slot or switching to a demo
        # device take. A field-bearing device is left for its own field edits to
        # commit, so a bare pick cannot overwrite a working device (see the loop).
        pending = chosen_id != ((entry.type_id or "") if entry else "")
        out.append(
            f"<form method=post action='/device' data-device"
            f"{' data-changed' if pending else ''}>"
            f"<input type=hidden name=slot value='{slot}'>"
            f"<input type=hidden name=type_id value='{html.escape(chosen_id)}'>"
        )
        out.append(self._csrf_field(csrf))

        selected_device = registry.get(chosen_id) if chosen_id else None
        # The radio slot folds its device parameters — the receiver assignment,
        # the bias tee — into the single operate form in its branch below, so it
        # renders neither the generic parameter fields here nor their Save. The
        # owner requirement is one button on that panel, not a Save on this form
        # and an Apply on another; `_set_radio` is what re-joins the two halves.
        if selected_device is not None and slot != "radio":
            stored_params = (
                (entry.params or {}) if entry and entry.type_id == chosen_id else {}
            )
            for parameter in selected_device.parameters:
                value = stored_params.get(parameter.name, parameter.default)
                name = f"p_{parameter.name}"
                out.append("<div class=field>")
                out.append(
                    f"<label for='{name}'>{html.escape(parameter.label)}</label>"
                )
                if parameter.type == "bool":
                    checked = " checked" if value else ""
                    out.append(
                        f"<input type=checkbox id='{name}' name='{name}'{checked}>"
                    )
                elif parameter.type == "password":
                    # The one field whose current value is a secret. Never
                    # rendered — not as a value, not in a placeholder. What is
                    # rendered is whether one is stored, which is the fact an
                    # installer needs; blank means "keep it" (see _set_device).
                    stored = bool(stored_params.get(parameter.name))
                    out.append(
                        f"<input type=password id='{name}' name='{name}' "
                        f"value='' autocomplete='new-password' "
                        f"placeholder='{'unchanged' if stored else 'not set'}'>"
                    )
                    out.append(
                        "<span class=muted>"
                        + ("Stored. Blank keeps it." if stored else "Not set.")
                        + "</span>"
                    )
                elif parameter.type == "select":
                    out.append(f"<select id='{name}' name='{name}'>")
                    for choice in parameter.choices:
                        sel = " selected" if str(value) == str(choice) else ""
                        out.append(f"<option{sel}>{html.escape(str(choice))}</option>")
                    out.append("</select>")
                elif parameter.name == "port":
                    # The ports that exist right now are offered; free text is
                    # kept because the device may not be plugged in yet.
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
                    field_type = "number" if parameter.type == "number" else "text"
                    out.append(
                        f"<input type={field_type} id='{name}' name='{name}' "
                        f"value='{html.escape(str(value))}'>"
                    )
                out.append("</div>")

            if selected_device.resource:
                out.append("<div class=field><label>Receiver</label><select name=resource>")
                out.append("<option value=''>— none assigned —</option>")
                for resource in resources:
                    sel = (
                        " selected"
                        if entry and entry.type_id == chosen_id
                        and entry.resource == resource["id"] else ""
                    )
                    label = f"{resource['model']} serial {resource['serial'] or 'unset'}"
                    out.append(
                        f"<option value='{html.escape(resource['id'])}'{sel}>"
                        f"{html.escape(label)}</option>"
                    )
                out.append("</select><span class=muted>One tuner, one band.</span></div>")

            if selected_device.absent:
                out.append(
                    "<div class=muted>No source for: "
                    + html.escape(", ".join(selected_device.absent)) + "</div>"
                )
        # No Save button. The script applies each field the moment it changes
        # and commits a freshly-picked device on load, so there is nothing to
        # press — its .field holds only the status line the fetch writes (the
        # outcome of each change: detected, or the reason it was not). This is
        # the one control on the page that genuinely needs the script: with it
        # blocked the picker still re-renders, but a device is committed over
        # fetch and there is no button behind it. In a .field with no label so
        # the line sits under the controls, not under the labels.
        if slot != "radio":
            out.append(
                "<div class=field>"
                "<span class='muted device-status' aria-live=polite></span>"
                # Device config is the one control that needs the script — the
                # commit is a fetch with no Save button behind it. Every other
                # control degrades; this one would be a silent dead form, so a
                # no-JS browser is told why rather than left editing fields that
                # never save. Mirrors the camera preview's own <noscript>.
                "<noscript><span class=muted>Configuring a device needs "
                "JavaScript enabled in this browser.</span></noscript>"
                "</div></form>"
            )
        else:
            # Radio commits through the Apply below, not here. Close the (now
            # field-less) /device form so its hidden slot/type_id and csrf are
            # still well-formed markup; nothing submits it, and the nonce'd
            # apply loop skips a form with no status line of its own.
            out.append("</form>")

        # The live tap goes *under* the controls. It is what an installer reads
        # to confirm a change worked, so it belongs after the thing they
        # changed rather than above it — with it on top, saving scrolled the
        # evidence off the screen.
        if slot == "camera":
            # A picture instead of datastream lines: the camera's raw tap is
            # capture statistics, and the question being asked is "is it
            # pointed at the right thing". The image is the cached frame
            # (/frame.jpg — never a fresh capture), and the checkbox is the
            # whole zoom mechanism, so expanding works with scripts blocked.
            out.append(self._preview(state.get("video") or {}))
        elif slot == "radio":
            # The whole radio panel is one form with one button — the owner's
            # requirement, and the reason the generic device form above renders
            # nothing for this slot. The dashboard drives the same receiver
            # through discrete operate commands (tune, gain, squelch, monitor),
            # and this page now offers the same set so a box can be brought up
            # and *heard* before it is ever enrolled. `_set_radio` splits what
            # this posts back into the two things it is: settings that rebuild
            # the receiver (which one, bias tee) and live operate commands.
            radio = self.agent.radio
            rs = state.get("radio") or {}
            stored = (
                (entry.params or {}) if entry and entry.type_id == chosen_id else {}
            )
            out.append(
                "<form method=post action='/radio' data-radio"
                f"{' data-changed' if pending else ''}>{self._csrf_field(csrf)}"
                f"<input type=hidden name=type_id value='{html.escape(chosen_id)}'>"
            )
            # The receiver this tuner is assigned to. The same field the generic
            # device form carries for every other slot, folded in here so the
            # radio keeps its single button. Only shown when the selected device
            # actually consumes a resource (the demo receiver does not).
            if selected_device is not None and selected_device.resource:
                out.append("<div class=field><label for=resource>Receiver</label>"
                           "<select id=resource name=resource>")
                out.append("<option value=''>— none assigned —</option>")
                for resource in resources:
                    sel = (
                        " selected"
                        if entry and entry.type_id == chosen_id
                        and entry.resource == resource["id"] else ""
                    )
                    label = f"{resource['model']} serial {resource['serial'] or 'unset'}"
                    out.append(
                        f"<option value='{html.escape(resource['id'])}'{sel}>"
                        f"{html.escape(label)}</option>"
                    )
                out.append(
                    "</select><span class=muted>One tuner, one band.</span></div>"
                )
            # Frequency — the dashboard's auto-decimal (the point after the third
            # digit) is added by the nonce'd script; without it, type the point.
            freq_value = f"{radio.freq_hz / 1e6:.3f}" if radio else ""
            out.append(
                "<div class=field><label for='freq_mhz'>Frequency</label>"
                "<input type=text id='freq_mhz' name='freq_mhz' spellcheck=false "
                f"inputmode=decimal placeholder='MHz' value='{freq_value}'></div>"
            )
            # Gain, in the tuner's own steps — the same discrete list the platform
            # offers, read from the device so a value the tuner cannot honour
            # cannot be picked. Fixed steps only: "auto" (the tuner's own AGC)
            # floats the level so the noise floor and the squelch both lose their
            # meaning, and the software "managed" mode over-gains into overload on
            # quiet airband — both broke the squelch this receiver is built
            # around, so neither is offered. A legacy box still on managed keeps
            # showing the settled step in the note beside the select.
            gains = rs.get("gains") or []
            current_gain = rs.get("gain")
            managed_db = rs.get("managed_gain_db")
            managed_note = (
                f"· {managed_db:.1f} dB"
                if current_gain == "managed" and isinstance(managed_db, (int, float))
                else ""
            )
            # The recommended step: the nearest one to the registry default. It
            # is marked "(Default)" and, when the stored gain is not one of the
            # tuner's steps — a station carried over from auto/managed, or a value
            # the tuner cannot honour — it is also what the select falls back to,
            # so Apply can never submit the first option (0 dB, deaf).
            default_step = (
                min(gains, key=lambda s: abs(float(s) - registry.AIRBAND_DEFAULT_GAIN_DB))
                if gains else None
            )
            selected_step = next(
                (s for s in gains if isinstance(current_gain, (int, float))
                 and abs(float(current_gain) - float(s)) < 0.05),
                default_step,
            )
            out.append(
                "<div class=field><label for=gain>Tuner gain (dB)</label>"
            )
            if not gains:
                # No tuner is bound yet — the radio is not built until a Receiver
                # is assigned — so there is no step list to read from the device.
                # A disabled field naming the reason, rather than an empty
                # dropdown that reads as broken (the steps appear on Apply).
                out.append(
                    "<select id=gain name=gain disabled>"
                    "<option>— assign a Receiver first —</option></select>"
                    "<span class=muted>Gain steps are read from the tuner: pick a "
                    "Receiver above and Apply to see them.</span></div>"
                )
            else:
                out.append("<select id=gain name=gain>")
                for step in gains:
                    sel = (" selected" if selected_step is not None
                           and abs(float(step) - float(selected_step)) < 0.05 else "")
                    is_default = (default_step is not None
                                  and abs(float(step) - float(default_step)) < 0.05)
                    # A native <option> cannot colour only the suffix, so the
                    # whole default line is greyed (Safari may ignore even that).
                    style = " style='color:#8a8a8a'" if is_default else ""
                    label = f"{float(step):.1f}" + (" (Default)" if is_default else "")
                    out.append(f"<option value='{step}'{sel}{style}>{label}</option>")
                out.append(
                    f"</select><span id=gain-managed class=muted>{managed_note}</span>"
                    "</div>"
                )
            # Squelch and the signal it gates, as one indicator — the platform's
            # settings panel verbatim: a readout, then a meter whose fill is the
            # in-channel signal, a hairline at the noise floor, and the squelch
            # threshold as a thumb on the same dB scale. Setting the level
            # against the signal beats a slider with nothing to aim at. The poll
            # moves the fill, the floor, the readout and the channel LED; the
            # thumb is the operator's, and moving it leaves AUTO (see the script
            # and set_squelch). dB->%: -90 floor, -10 saturation, as the platform.
            rssi, floor = rs.get("rssi_db"), rs.get("floor_db")
            threshold_db = rs.get("threshold_db")
            thr = threshold_db if isinstance(threshold_db, (int, float)) else -70.0
            rssi_txt = f"{rssi:.0f} dB" if isinstance(rssi, (int, float)) else "—"
            floor_txt = f"{floor:.0f} dB" if isinstance(floor, (int, float)) else "—"
            rssi_pct = (
                max(0.0, min(100.0, ((rssi + 90) / 80) * 100))
                if isinstance(rssi, (int, float)) else 0.0
            )
            floor_pct = (
                max(0.0, min(100.0, ((floor + 90) / 80) * 100))
                if isinstance(floor, (int, float)) else 0.0
            )
            led = " on" if rs.get("squelch_open") else ""
            out.append(
                "<div class=field><label>Squelch</label><div>"
                "<div class=radio-readout>"
                f"<span>Signal <b id=sig-rssi>{rssi_txt}</b></span>"
                f"<span>Floor <b id=sig-floor>{floor_txt}</b></span>"
                f"<span>Threshold <b id=sig-thr>{thr:.0f} dB</b></span>"
                f"<span id=sig-led class='led{led}' "
                "title='Channel open'></span></div>"
                "<div class=meter>"
                f"<div id=meter-fill class=meter-fill style='width:{rssi_pct:.1f}%'>"
                "</div>"
                f"<div id=meter-floor class=meter-floor style='left:{floor_pct:.1f}%'>"
                "</div>"
                "<input class=squelch-overlay type=range id=squelch name=squelch "
                f"min=-110 max=-10 step=1 value='{thr:.0f}' "
                "aria-label='Squelch threshold'></div></div></div>"
            )
            out.append(
                "<div class=field><label for=auto_squelch>Auto squelch</label>"
                "<input type=checkbox id=auto_squelch name=auto_squelch value='1'"
                + (" checked" if rs.get("auto") else "")
                + "><span class=muted>Tracks the noise floor. Unticking freezes "
                "the threshold where it is.</span></div>"
            )
            # Auto squelch margin — how far above the floor AUTO opens — live, so
            # a noisy site can widen it without a redeploy.
            margin = rs.get("auto_margin_db")
            margin_val = margin if isinstance(margin, (int, float)) else 8.0
            out.append(
                "<div class=field><label for=auto_margin>Auto squelch margin (dB)"
                "</label><input type=number id=auto_margin name=auto_margin "
                f"min=3 max=25 step=1 value='{margin_val:.0f}'>"
                "<span class=muted>How far above the noise floor AUTO opens the "
                "gate. Higher rejects noise; lower catches weaker signals.</span>"
                "</div>"
            )
            # Squelch hang — how long the gate stays open after a signal drops.
            hang = rs.get("hang_s")
            hang_val = hang if isinstance(hang, (int, float)) else 0.6
            out.append(
                "<div class=field><label for=hang_s>Squelch hang (s)</label>"
                f"<input type=number id=hang_s name=hang_s min=0 max=5 step=0.1 "
                f"value='{hang_val:.1f}'>"
                "<span class=muted>How long the gate stays open after a signal "
                "drops, so a gap mid-over does not clip it into fragments. Lower "
                "closes sooner.</span></div>"
            )
            out.append(
                "<div class=field><label for='monitor'>Hold gate open</label>"
                "<input type=checkbox id='monitor' name='monitor' value='1'"
                + (" checked" if radio and radio.monitor else "")
                + "><span class=muted>Bypasses the squelch, for bringing an "
                "antenna up.</span></div>"
            )
            # Crystal correction — live-settable, a starting guess for a tuner
            # that can come up mis-programmed (see the registry note).
            current_ppm = rs.get("ppm") or 0
            out.append(
                "<div class=field><label for=ppm>Crystal (ppm)</label>"
                "<input type=number id=ppm name=ppm "
                f"value='{int(current_ppm)}'></div>"
            )
            # The device-rebuild parameters — the bias tee, the channel filter,
            # the voice filter — rendered straight from the registry, minus gain
            # and ppm, which have their own live controls above. Each opens the
            # front end differently, so changing one rebuilds the receiver; they
            # post as `dev_<param>` and are persisted with the device. Adding a
            # radio parameter to the registry surfaces here with no change.
            if selected_device is not None:
                for parameter in selected_device.parameters:
                    if parameter.name in ("gain", "ppm"):
                        continue
                    pid = f"dev_{parameter.name}"
                    label = html.escape(parameter.label)
                    tip = (f"<span class=muted>{html.escape(parameter.help)}</span>"
                           if parameter.help else "")
                    value = stored.get(parameter.name, parameter.default)
                    if parameter.type == "bool":
                        on = " checked" if value else ""
                        out.append(
                            f"<div class=field><label for={pid}>{label}</label>"
                            f"<input type=checkbox id={pid} name={pid} value='1'{on}>"
                            f"{tip}</div>"
                        )
                    else:
                        field_type = "number" if parameter.type == "number" else "text"
                        out.append(
                            f"<div class=field><label for={pid}>{label}</label>"
                            f"<input type={field_type} id={pid} name={pid} "
                            f"value='{html.escape(str(value))}'>{tip}</div>"
                        )
            # The site/position dict, under `position` — NOT `state["station"]`,
            # which is the station's *name* (a string) once enrolled and was the
            # crash that took this whole page down: `'str' object has no
            # attribute 'get'` on an enrolled box, while an un-enrolled demo had
            # None here and slipped through.
            station = (state.get("position") or {}).get("station") or {}
            transcribe_on = " checked" if station.get("radio_transcribe") else ""
            keep_days = station.get("transcript_retention_days")
            keep_days = int(keep_days) if isinstance(keep_days, (int, float)) else 30
            note = (
                "Logs what is heard on the airband, on the box, with whisper.cpp."
                if station.get("transcribe_installed")
                else "Needs whisper.cpp on the station: "
                + html.escape(station.get("transcribe_reason") or "not installed")
                + "."
            )
            out.append(
                "<div class=field><label for='radio_transcribe'>Transcribe</label>"
                "<input type=checkbox id='radio_transcribe' "
                f"name='radio_transcribe' value='1'{transcribe_on}>"
                f"<span class=muted>{note}</span></div>"
                "<div class=field><label for='transcript_days'>Keep transcripts "
                "(days)</label><input type=number id='transcript_days' "
                f"name='transcript_days' min=0 step=1 value='{keep_days}'>"
                "<span class=muted>How long transcripts are kept on the box; "
                "0 keeps them until cleared by hand.</span></div>"
                # The button is the no-script fallback: with the nonce'd script
                # running, each control applies the moment it changes and this is
                # hidden, its .field given over to a status line the fetch writes.
                "<div class=field><button type=submit>Apply</button>"
                "<span class=muted id=radio-status aria-live=polite></span>"
                "</div></form>"
            )
            # Listen — a volume control, not a play button, the same as the
            # platform's front panel. Outside the form on purpose: it is local
            # only, so moving it must not post a settings change. The slider is
            # the whole transport: the nonce'd script pulls /audio.wav (the
            # demodulator's own PCM, before Opus and the link) through Web Audio,
            # started by the drag off zero — the gesture a browser requires — and
            # stopped at zero, so a muted or closed page pulls nothing off the
            # box. Web Audio rather than an <audio> element because that buffered
            # without limit and drifted ever further behind; the script schedules
            # each chunk against the clock and drops backlog past a cap. The gate
            # decides what is in it: silence while the squelch is shut, so "Hold
            # gate open" is how you hear a quiet band.
            out.append(
                "<div class=field><label for=volume>Listen</label>"
                "<div class=volume-row>"
                "<input type=range id=volume min=0 max=1 step=0.05 value=0 "
                "aria-label='Listen volume'>"
                "<span class=muted>Drag to listen — bench test before enrolment."
                "</span></div></div>"
            )
            out.append("<div class=field><label>Spectrum</label></div>")
            out.append(
                "<canvas id=spectrum class=spectrum width=512 height=110></canvas>"
            )
            lines = (state.get("raw_samples") or {}).get(slot) or []
            out.append("<div class=field><label>Data</label></div>")
            out.append(
                f"<pre class=raw id=raw data-slot='{slot}'>"
                + html.escape("\n".join(lines)) + "</pre>"
            )
        else:
            # Empty when nothing is connected. The nonce'd script refreshes it
            # from status.json; without script it is the state at render time,
            # which is still the truth.
            lines = (state.get("raw_samples") or {}).get(slot) or []
            out.append("<div class=field><label>Data</label></div>")
            out.append(
                f"<pre class=raw id=raw data-slot='{slot}'>"
                + html.escape("\n".join(lines)) + "</pre>"
            )
        out.append("</div>")

        # No "serial ports present now" list and no "no SDR receivers" note.
        # The first duplicated the port dropdown above it, which offers exactly
        # those ports and is where the choice is actually made; the second was
        # a sentence about absent hardware on a card whose pill already says
        # the slot is not fitted.
        return "".join(out)

    @staticmethod
    def _preview(video: dict) -> str:
        """The camera preview: latest frame, its age, click to expand."""
        out = [
            "<div class=field><label>Preview</label></div>",
            "<input type=checkbox id=zoom class=zoom-toggle>",
        ]
        # Live, not a still. This page is where somebody aims a camera, and a
        # frame up to two seconds old fetched every two and a half is three to
        # five seconds behind the thing being pointed — you cannot aim with
        # that. The <video> is the same encoder the platform watches; see
        # `_send_stream` and `TeeUplink`.
        #
        # `muted` is not a preference: an autoplaying video with sound is
        # blocked outright by every browser, and there is no audio on this
        # stream to lose. `playsinline` stops iOS taking it fullscreen, which
        # matters because the far more likely device in front of a station is
        # a phone.
        # No `src` in the markup. The script attaches the stream, through
        # Media Source Extensions where they exist and progressively where they
        # do not. Leaving a src here meant Chromium began a progressive load it
        # cannot finish — which spawns an encoder on this box and abandons it
        # seconds later — before the script replaced it. One request, always.
        #
        # The empty state is markup, not an absence. A `<video>` with nothing
        # attached renders as a black rectangle, and a black rectangle where a
        # camera should be is the least useful thing this page can show: it is
        # indistinguishable from a camera working in an unlit room, which is
        # exactly the wrong guess to send somebody off with. The span says
        # which, in the station's own words, and the class hides the empty
        # elements rather than leaving them stacked behind it.
        #
        # **The three elements all exist, always, and a class picks one.**
        #
        # The live element is what the stream attaches to, so it must never be
        # torn down to show something else. Rebuilding the box to swap a still
        # in removed it 2.5 seconds after every page load — before it had
        # decoded its first frame, which is the moment it looks most like it is
        # not working — and took the stream that was about to start with it.
        # What was left was a still refreshing every 2.5 s, which reads as a
        # picture that freezes every few seconds, on an idle CPU because the
        # encoder had been stopped.
        has_frame = bool(video.get("has_frame"))
        stream = video.get("stream") or {}
        live = stream.get("state") in ("streaming", "starting")
        mode = "live" if live else ("still" if has_frame else "empty")
        message = str(video.get("reason") or "") or "No picture from this camera yet."
        out.append(
            f"<label for=zoom class=\"preview {mode}\" id=preview-wrap>"
            "<video id=preview autoplay muted playsinline></video>"
            "<img id=preview-still alt='latest camera frame'>"
            f"<span id=preview-empty>{html.escape(message)}</span>"
            "<noscript>The live preview needs JavaScript. "
            "<a href='/frame.jpg'>Latest still</a></noscript></label>"
        )
        # Same rule as the poll: why there is no live picture beats how old the
        # still is. Rendered on the first paint too, so the answer is on screen
        # before the first poll rather than 2.5 seconds into looking at it.
        why = str(stream.get("reason") or "") if not live else ""
        if why:
            out.append(f"<div class=muted id=preview-age>{html.escape(why)}</div>")
        elif has_frame:
            age = video.get("frame_age_s") or 0
            out.append(
                f"<div class=muted id=preview-age>still frame {age:.0f} s old"
                "</div>"
            )
        else:
            out.append("<div class=muted id=preview-age></div>")
        return "".join(out)

    @staticmethod
    def _devices_script(nonce: str) -> str:
        """The one script this app carries, admitted by a per-response nonce.

        Its jobs: apply each radio and device change the moment it lands, over a
        coalescing ajax fetch with no reload (makeInstantApply), so a control
        moved or a device picked takes without a Save button — the one place on
        the page that needs the script, said in a <noscript>; and refresh the
        live readings — the datastream field or, on the camera tab, the frame
        preview and its age, plus the slot pill and the radio meters — from
        status.json, same auth gate as every page, every 2.5 s, nothing off-box
        (the CSP's connect-src enforces that). The preview image is re-fetched
        with a timestamp query because the response is no-store and the browser
        still needs the src to change before it asks again.
        """
        script = """
"use strict";
(function () {
  // Until when the poll must leave the radio controls alone: a control just
  // changed here is mid-apply, and the poll reading back the not-yet-applied
  // value would flick it to the old setting and back. The same idea as the
  // platform's settle timers. Shared between the instant-apply and the poll.
  var radioSettleUntil = 0;
  // How long an apply may wait before it is abandoned. Longer than the slowest
  // honest save — a network camera's rebuild plus its detection grace — and far
  // shorter than a browser's own dead-socket timeout, which can be minutes: a
  // request that never answers must not strand the form on "Saving…" with every
  // later edit swallowed into `again`.
  var APPLY_TIMEOUT_MS = 20000;
  // One instant-apply engine for both the radio panel and the device forms.
  // Coalesce in-flight posts, POST the whole form as ajax (so the answer is a
  // line to show, not a reload), and write the outcome to a status line. Was
  // copied out twice; the coalescing and the failure wording are subtle enough
  // that one copy is worth having. `before` runs on every call, coalesced ones
  // included — the radio uses it to hold the poll off its own controls.
  function makeInstantApply(form, url, statusEl, before) {
    var busy = false, again = false;
    var say = function (text, bad) {
      if (!statusEl) return;
      statusEl.textContent = text;
      statusEl.style.color = bad ? "var(--danger)" : "";
    };
    var apply = function () {
      if (before) before();
      if (busy) { again = true; return; }
      busy = true;
      var data = new URLSearchParams(new FormData(form));
      data.set("ajax", "1");
      say("Saving\\u2026", false);
      var done = function (text, bad) {
        busy = false;
        say(text, bad);
        if (again) { again = false; apply(); }
      };
      // Abandon a request that never answers, so a dead link does not freeze the
      // form on "Saving…" until the browser's own timeout finally gives up.
      var ctrl = ("AbortController" in window) ? new AbortController() : null;
      var timer = ctrl
        ? window.setTimeout(function () { ctrl.abort(); }, APPLY_TIMEOUT_MS)
        : 0;
      fetch(url, {
        method: "POST",
        body: data,
        credentials: "same-origin",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        signal: ctrl ? ctrl.signal : undefined
      })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (timer) window.clearTimeout(timer);
          if (!j) done("Not saved — reload and try again.", true);
          else done(j.message || "Saved.", !j.ok);
        })
        .catch(function () {
          if (timer) window.clearTimeout(timer);
          done("Not saved — no answer from the station.", true);
        });
    };
    return apply;
  }
  // Picking a device re-renders the page for it. The Change button beside the
  // select does this without script; with script the select alone is enough,
  // so the button hides and choosing costs one interaction instead of two.
  var pickers = document.querySelectorAll("form[data-pick]");
  for (var p = 0; p < pickers.length; p++) {
    (function (form) {
      var go = form.querySelector(".pick-go");
      var select = form.querySelector("select");
      if (!go || !select) return;
      go.hidden = true;
      select.addEventListener("change", function () { form.submit(); });
    })(pickers[p]);
  }
  // Airband is always xxx.xxx, so the point goes in after the third digit as the
  // operator types — the same behaviour the dashboard has. "128950" shows as
  // "128.950".
  var freqInput = document.getElementById("freq_mhz");
  if (freqInput) {
    freqInput.addEventListener("input", function () {
      var digits = freqInput.value.replace(/\\D/g, "").slice(0, 6);
      freqInput.value = digits.length > 3
        ? digits.slice(0, 3) + "." + digits.slice(3)
        : digits;
    });
  }
  // The squelch thumb writes its dB into the readout as it moves, and moving it
  // unticks AUTO — the same "a manual level means manual" the station enforces
  // in set_squelch, said in the UI so the two controls do not disagree on
  // screen.
  var squelch = document.getElementById("squelch");
  var autoSquelch = document.getElementById("auto_squelch");
  var sigThr = document.getElementById("sig-thr");
  if (squelch) {
    squelch.addEventListener("input", function () {
      if (sigThr) sigThr.textContent = squelch.value + " dB";
      if (autoSquelch) autoSquelch.checked = false;
    });
  }
  // Listen, through Web Audio rather than an <audio> element.
  //
  // /audio.wav is raw PCM16 mono — the demodulator's own output. An <audio>
  // element played it but buffered without limit, so the box drifted ever
  // further behind the platform. This schedules each chunk against the context
  // clock with a fixed lead and DROPS backlog past a cap, so latency stays put:
  // the platform's own scheduled engine, ported (its AudioWorklet is
  // secure-context-only and a LAN-served page is not one, so the worklet would
  // not run here anyway). No manual resampling — the browser resamples the
  // 24 kHz buffers on playback.
  //
  // The volume slider is the whole transport. Dragging it off zero is the user
  // gesture a browser needs to start audio and the request to hear it; zero
  // aborts the fetch, so a muted or closed page pulls nothing off the box.
  var volume = document.getElementById("volume");
  if (volume) {
    var audioCtx = null, audioGain = null, audioAbort = null, nextTime = 0;
    var streamRate = 24000;
    var schedule = function (samples) {
      if (!audioCtx || audioCtx.state === "closed" || !samples.length) return;
      var buf = audioCtx.createBuffer(1, samples.length, streamRate);
      buf.copyToChannel(samples, 0);
      var src = audioCtx.createBufferSource();
      src.buffer = buf;
      src.connect(audioGain);
      var now = audioCtx.currentTime, LEAD = 0.2, CAP = 0.6;
      // First chunk, an underrun, or a backlog past the cap all reset the cursor
      // to one lead ahead. The cap is what stops a jitter burst adding latency
      // for the rest of the session — the whole point over the <audio> element.
      if (nextTime < now + 0.02 || nextTime > now + CAP) nextTime = now + LEAD;
      src.start(nextTime);
      nextTime += buf.duration;
    };
    var leftover = null;
    var feed = function (bytes) {
      // PCM16 is two bytes a sample, and a TCP read can split one; carry the odd
      // byte to the next.
      if (leftover) {
        var m = new Uint8Array(leftover.length + bytes.length);
        m.set(leftover, 0); m.set(bytes, leftover.length);
        bytes = m; leftover = null;
      }
      var n = bytes.length >> 1;
      if (bytes.length & 1) leftover = new Uint8Array([bytes[bytes.length - 1]]);
      if (!n) return;
      var dv = new DataView(bytes.buffer, bytes.byteOffset, n * 2);
      var samples = new Float32Array(n);
      for (var i = 0; i < n; i++) samples[i] = dv.getInt16(i * 2, true) / 32768;
      schedule(samples);
    };
    var stream = function (signal) {
      fetch("/audio.wav", { credentials: "same-origin", signal: signal })
        .then(function (res) {
          if (!res.ok || !res.body) return;
          var reader = res.body.getReader();
          var head = [], headLen = 0, haveHead = false;
          var pump = function (result) {
            if (result.done) return;
            var bytes = result.value;
            if (!haveHead) {
              head.push(bytes); headLen += bytes.length;
              if (headLen < 44) return reader.read().then(pump);
              var all = new Uint8Array(headLen), at = 0;
              head.forEach(function (b) { all.set(b, at); at += b.length; });
              // The sample rate is bytes 24-27 of the WAV header, little-endian,
              // so the player follows the station rather than assuming 24 kHz.
              var rate = new DataView(all.buffer).getUint32(24, true);
              if (rate > 0) streamRate = rate;
              haveHead = true; head = null;
              bytes = all.subarray(44);
            }
            feed(bytes);
            return reader.read().then(pump);
          };
          return reader.read().then(pump);
        })
        .catch(function () {});
    };
    var start = function () {
      if (!audioCtx) {
        var AC = window.AudioContext || window.webkitAudioContext;
        if (!AC) return;
        try { audioCtx = new AC(); } catch (e) { return; }
        audioGain = audioCtx.createGain();
        audioGain.connect(audioCtx.destination);
      }
      audioCtx.resume().catch(function () {});
      if (audioAbort) return;              // already streaming
      audioAbort = new AbortController();
      nextTime = 0; leftover = null;
      stream(audioAbort.signal);
    };
    var stop = function () {
      if (audioAbort) { audioAbort.abort(); audioAbort = null; }
      nextTime = 0;
    };
    volume.addEventListener("input", function () {
      var v = Number(volume.value);
      if (v > 0) { start(); if (audioGain) audioGain.gain.value = v; }
      else { if (audioGain) audioGain.gain.value = 0; stop(); }
    });
  }
  // The radio settings apply the moment a control changes — no Apply button to
  // find, and no page reload to tear down the audio being listened to. The
  // button is the no-script fallback and is hidden here; a fetch posts the whole
  // form (the station re-applies what it is already on, so this is idempotent)
  // and writes the outcome to the status line. In-flight requests coalesce so a
  // quick series of changes cannot pile up rebuilds.
  var radioForm = document.querySelector("form[data-radio]");
  if (radioForm) {
    var applyBtn = radioForm.querySelector("button[type=submit]");
    if (applyBtn) applyBtn.hidden = true;
    var applyRadio = makeInstantApply(
      radioForm, "/radio", document.getElementById("radio-status"),
      // Hold the poll off these controls while the change is applied and echoed
      // back, so it cannot briefly revert them to the pre-change reading.
      function () { radioSettleUntil = Date.now() + 2000; });
    // Enter in a field would otherwise submit the form for real (a reload);
    // take it over too, so every route to a change goes through the fetch.
    radioForm.addEventListener("submit", function (e) { e.preventDefault(); applyRadio(); });
    radioForm.addEventListener("change", applyRadio);
    // A receiver just picked from the dropdown arrives with its type differing
    // from what is stored (`data-changed`); commit it without waiting for a
    // control to move, so choosing a receiver takes here as it does on every
    // other slot.
    if (radioForm.hasAttribute("data-changed")) applyRadio();
  }
  // The Devices tab applies each change the moment it lands. Picking a device
  // re-renders the page for the chosen type (the pick form above, a
  // navigation); from there every field commits on change with no Save button
  // to find, over a fetch that carries the `ajax` marker so the answer is a
  // line to show and not a reload that would tear down the camera preview. A
  // device just picked arrives with `data-changed` and commits itself, so
  // choosing one — including un-fitting a slot or switching to a demo device,
  // neither of which has a field to touch — is all it takes. In-flight posts
  // coalesce, so a quick series of edits cannot pile up driver rebuilds.
  var deviceForms = document.querySelectorAll("form[data-device]");
  for (var d = 0; d < deviceForms.length; d++) {
    (function (form) {
      var status = form.querySelector(".device-status");
      // No status line is the radio slot's placeholder form — its fields live in
      // the radio panel, which commits them. Nothing here to apply.
      if (!status) return;
      var applyDevice = makeInstantApply(form, "/device", status, null);
      // Enter in a field would submit for real (a reload); route it through the
      // fetch like every other change.
      form.addEventListener("submit", function (e) { e.preventDefault(); applyDevice(); });
      form.addEventListener("change", applyDevice);
      // Auto-commit a freshly-picked device when nothing it needs is left blank:
      // an un-fit slot, a param-less device, or one whose defaults are already
      // complete — the demo sources, a GPIO relay, anything ready to run as
      // rendered. Only a device left with an EMPTY field — a network camera with
      // no address, a serial device with no port — waits for that field, and the
      // edit that fills it is what commits (the `change` listener above). This is
      // safe on a bare pick: a re-pick of the same device is not `data-changed`,
      // and a fresh pick has no stored params to overwrite, so it can neither
      // wipe a working config nor post a half-filled one. Checkboxes, radios and
      // selects always carry a value, so only a text/number/textarea can be
      // blank — which is exactly "the operator still has to type something".
      var blank = false;
      var fields = form.querySelectorAll(
        "input:not([type=hidden]):not([type=checkbox]):not([type=radio]), textarea");
      for (var fi = 0; fi < fields.length; fi++) {
        if (!fields[fi].value.trim()) { blank = true; break; }
      }
      if (form.hasAttribute("data-changed") && !blank) applyDevice();
    })(deviceForms[d]);
  }
  // The live camera, through Media Source Extensions.
  //
  // Not `<video src="/stream.mp4">`, which was tried first and is what the
  // markup still looks like as a fallback. Chromium parses the stream that way
  // — correct dimensions, readyState 3, no error — and then will not play it:
  // its progressive demuxer wants range requests on a resource that has no
  // length and cannot serve them, so the element sits at currentTime 0 and the
  // buffer empties. MSE is the path browsers actually support for live fMP4.
  //
  // The codec comes out of the init segment rather than from the server. It is
  // three bytes in the avcC box and the station would otherwise have to tell
  // us twice, in two places that could disagree.
  var live = document.getElementById("preview");
  if (live && live.tagName === "VIDEO") {
    // Progressive where MSE is missing: Safari and Firefox handle a live fMP4
    // body that way, and it is Chromium specifically that will not.
    if (window.MediaSource) startLive(live);
    else live.src = "/stream.mp4";
  }

  function codecOf(bytes) {
    // An avcC box is: 4-byte size, 4-byte type, then a payload beginning with
    // configurationVersion. The three bytes that name the codec — profile,
    // profile-compatibility, level — come *after* that version byte, so from
    // the type at `i` they are i+5, i+6, i+7.
    //
    // Reading them one byte early produced a plausible-looking string that no
    // decoder accepts, addSourceBuffer threw, and the picture never appeared.
    for (var i = 0; i + 12 < bytes.length; i++) {
      var tag = String.fromCharCode(bytes[i], bytes[i + 1], bytes[i + 2], bytes[i + 3]);
      if (tag !== "avcC" && tag !== "hvcC") continue;
      var hex = "";
      for (var j = 1; j <= 3; j++) {
        hex += ("0" + bytes[i + 4 + j].toString(16)).slice(-2);
      }
      return (tag === "avcC" ? "avc1." : "hvc1.") + hex;
    }
    return null;
  }

  function startLive(v) {
    fetch("/stream.mp4").then(function (res) {
      if (!res.ok || !res.body) {
        return res.text().then(function (why) {
          console.error("live preview: " + (why || res.status));
          var age = document.getElementById("preview-age");
          if (age) age.textContent = why || ("stream refused: " + res.status);
        });
      }
      var reader = res.body.getReader();
      var head = [], headLen = 0, codec = null, ms = null, sb = null;
      var queue = [], ended = false;

      function flush() {
        if (!sb || sb.updating || !queue.length) return;
        try { sb.appendBuffer(queue.shift()); }
        catch (e) { ended = true; fail("appending video failed: " + e.message); }
      }

      // Said out loud. Every failure on this path was swallowed, so an
      // off-by-one in the codec string looked exactly like a camera with
      // nothing to send.
      function fail(why) {
        console.error("live preview: " + why);
        var age = document.getElementById("preview-age");
        if (age) age.textContent = why;
      }

      function attach() {
        ms = new MediaSource();
        v.src = URL.createObjectURL(ms);
        ms.addEventListener("sourceopen", function () {
          try { sb = ms.addSourceBuffer('video/mp4; codecs="' + codec + '"'); }
          catch (e) {
            ended = true;
            fail("this browser will not decode " + codec);
            return;
          }
          sb.mode = "segments";
          sb.addEventListener("updateend", function () {
            // Keep the picture at the live edge and the buffer bounded. A
            // preview left running all afternoon is otherwise an afternoon of
            // video in memory, and a decoder minutes behind the camera.
            if (sb.buffered.length) {
              var end = sb.buffered.end(sb.buffered.length - 1);
              if (v.currentTime < end - 1.5) v.currentTime = end - 0.3;
              var start = sb.buffered.start(0);
              if (end - start > 12 && !sb.updating) {
                try { sb.remove(start, end - 6); } catch (e) {}
              }
            }
            if (v.paused) v.play().catch(function () {});
            flush();
          });
          queue = head.concat(queue);
          head = [];
          flush();
        });
      }

      function pump(r) {
        if (r.done || ended) return;
        var bytes = r.value;
        if (!codec) {
          head.push(bytes);
          headLen += bytes.length;
          var joined = new Uint8Array(headLen), at = 0;
          head.forEach(function (b) { joined.set(b, at); at += b.length; });
          codec = codecOf(joined);
          if (codec) { head = [joined]; attach(); }
        } else {
          queue.push(bytes);
          flush();
        }
        return reader.read().then(pump);
      }
      reader.read().then(pump);
    }).catch(function (e) { console.error("live preview: " + e.message); });
  }

  // The spectrum. Same scale and furniture as the console's canvas: a dashed
  // squelch line, a centre marker on the tuned channel, a filled trace and the
  // window's edge frequencies. Fixed size, so nothing it draws can resize
  // anything around it.
  var spec = document.getElementById("spectrum");
  var drawSpectrum = function (radio) {
    if (!spec || !radio || !radio.spectrum || !radio.spectrum.length) return;
    var ctx = spec.getContext("2d");
    var W = spec.width, H = spec.height;
    var MIN = -110, MAX = -10;
    var y = function (db) {
      return H - ((Math.max(MIN, Math.min(MAX, db)) - MIN) / (MAX - MIN)) * H;
    };
    ctx.clearRect(0, 0, W, H);
    if (typeof radio.threshold_db === "number") {
      ctx.strokeStyle = "rgba(0,160,220,.5)";
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, y(radio.threshold_db));
      ctx.lineTo(W, y(radio.threshold_db));
      ctx.stroke();
      ctx.setLineDash([]);
    }
    ctx.strokeStyle = "rgba(221,230,237,.35)";
    ctx.beginPath(); ctx.moveTo(W / 2, 0); ctx.lineTo(W / 2, H); ctx.stroke();
    var s = radio.spectrum, n = s.length;
    ctx.beginPath(); ctx.moveTo(0, H);
    for (var i = 0; i < n; i++) ctx.lineTo((i / (n - 1)) * W, y(s[i]));
    ctx.lineTo(W, H); ctx.closePath();
    ctx.fillStyle = "rgba(53,196,138,.18)"; ctx.fill();
    ctx.beginPath();
    for (var j = 0; j < n; j++) {
      var px = (j / (n - 1)) * W;
      if (j === 0) ctx.moveTo(px, y(s[j])); else ctx.lineTo(px, y(s[j]));
    }
    ctx.strokeStyle = "#35c48a"; ctx.lineWidth = 1.5; ctx.stroke();
    var half = (radio.span_hz || 240000) / 2;
    var mhz = (radio.freq_mhz || 0) * 1e6;
    ctx.fillStyle = "rgba(127,146,159,.9)";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "bottom";
    ctx.textAlign = "left";
    ctx.fillText(((mhz - half) / 1e6).toFixed(3), 4, H - 3);
    ctx.textAlign = "center";
    ctx.fillText((mhz / 1e6).toFixed(3), W / 2, H - 3);
    ctx.textAlign = "right";
    ctx.fillText(((mhz + half) / 1e6).toFixed(3), W - 4, H - 3);
  };

  var raw = document.getElementById("raw");
  var wrap = document.getElementById("preview-wrap");
  if (raw || wrap || spec) {
    var poll = function () {
      fetch("/status.json", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (s) {
          if (!s) return;
          if (spec && s.radio) drawSpectrum(s.radio);
          // The signal meter under the squelch, moved live so the threshold can
          // be set against what is being heard: fill to the signal, a hairline
          // at the noise floor, the readout and the channel LED. The dB->% scale
          // is the platform's (-90 floor, -10 saturation), and the keys are the
          // status.json ones — rssi_db / floor_db, not the telemetry schema's.
          if (s.radio) {
            var r = s.radio;
            var db = function (v) {
              return (typeof v === "number") ? v.toFixed(0) + " dB" : "\\u2014";
            };
            var pct = function (v) {
              return (typeof v === "number")
                ? Math.max(0, Math.min(100, ((v + 90) / 80) * 100)) : 0;
            };
            var setText = function (id, text) {
              var el = document.getElementById(id);
              if (el) el.textContent = text;
            };
            setText("sig-rssi", db(r.rssi_db));
            setText("sig-floor", db(r.floor_db));
            var fill = document.getElementById("meter-fill");
            if (fill) fill.style.width = pct(r.rssi_db) + "%";
            var mfloor = document.getElementById("meter-floor");
            if (mfloor) mfloor.style.left = pct(r.floor_db) + "%";
            var led = document.getElementById("sig-led");
            if (led) led.className = "led" + (r.squelch_open ? " on" : "");

            // Mirror the receiver's live settings into the controls, so a change
            // made from the platform shows here too — the box and the platform
            // drive the same receiver and must not disagree on screen. Two
            // guards: never touch a control the operator has focused (that is
            // the one being changed here), and stay off all of them for a beat
            // after a local change, so the poll cannot read back a value that is
            // still being applied and flick the control to the old setting. The
            // meter above is exempt: it is a reading, not a control.
            var settling = Date.now() < radioSettleUntil;
            var idle = function (el) {
              return el && !settling && document.activeElement !== el;
            };
            var thr = document.getElementById("squelch");
            var auto = document.getElementById("auto_squelch");
            var dragging = thr && document.activeElement === thr;
            var freqEl = document.getElementById("freq_mhz");
            if (idle(freqEl) && typeof r.freq_mhz === "number") {
              var f = r.freq_mhz.toFixed(3);
              if (freqEl.value !== f) freqEl.value = f;
            }
            var ppmEl = document.getElementById("ppm");
            if (idle(ppmEl) && r.ppm != null && ppmEl.value !== String(r.ppm)) {
              ppmEl.value = String(r.ppm);
            }
            var monEl = document.getElementById("monitor");
            if (idle(monEl)) monEl.checked = !!r.monitor;
            // AUTO is coupled to the slider (dragging it leaves AUTO), so treat a
            // drag as editing AUTO too and leave it alone until the drag ends.
            if (idle(auto) && !dragging) auto.checked = !!r.auto;
            // The gain select: "auto" and "managed" match their own option,
            // and a numeric gain matches by number because the step options are
            // Python floats ("0.0", "37.2") while r.gain is a JSON number.
            var gainEl = document.getElementById("gain");
            if (idle(gainEl)) {
              var word = (r.gain === "auto" || r.gain === "managed");
              for (var gi = 0; gi < gainEl.options.length; gi++) {
                var ov = gainEl.options[gi].value;
                var hit = word
                  ? (ov === r.gain)
                  : (ov !== "auto" && ov !== "managed"
                     && Math.abs(Number(ov) - Number(r.gain)) < 0.05);
                if (hit) {
                  if (gainEl.selectedIndex !== gi) gainEl.selectedIndex = gi;
                  break;
                }
              }
            }
            // The step managed gain settled on, beside the select. Cleared when
            // the mode is off. Updated even while the select is focused — it is
            // a readout, not the control.
            var gainManaged = document.getElementById("gain-managed");
            if (gainManaged) {
              gainManaged.textContent =
                (r.gain === "managed" && typeof r.managed_gain_db === "number")
                  ? "\\u00b7 " + r.managed_gain_db.toFixed(1) + " dB" : "";
            }
            // Squelch: follow the receiver's threshold whenever the slider is not
            // being dragged here — AUTO riding the floor, or a manual level set
            // from the platform, both then show up.
            if (idle(thr) && typeof r.threshold_db === "number") {
              thr.value = r.threshold_db;
              setText("sig-thr", r.threshold_db.toFixed(0) + " dB");
            }
          }
          if (raw && s.raw_samples) {
            var lines = s.raw_samples[raw.getAttribute("data-slot")] || [];
            raw.textContent = lines.join("\\n");
          }
          // The slot-head pill, refreshed from the same per-slot report. An
          // inline apply never reloads, so this is what stops the pill reading
          // "Disconnected" beside a status line that already says the device
          // answered. The status->pill map mirrors STATUS_PILL in console.py;
          // the four states are stable.
          var pill = document.getElementById("slot-pill");
          if (pill && s.devices) {
            var wantSlot = pill.getAttribute("data-pill-slot");
            for (var pi = 0; pi < s.devices.length; pi++) {
              if (s.devices[pi].slot !== wantSlot) continue;
              var PILL = {
                present: ["ok", "Connected"],
                stalled: ["warn", "Disconnected"],
                configured_absent: ["warn", "Disconnected"],
                not_fitted: ["off", "Not fitted"]
              };
              var pv = PILL[s.devices[pi].status] || ["off", s.devices[pi].status];
              pill.className = "pill " + pv[0];
              pill.textContent = pv[1];
              break;
            }
          }
          // Three states, one at a time, and **the station decides which**.
          //
          // This used to work it out from the element — a <video> with no
          // error counted as playing, then a <video> with no decoded frame
          // counted as not playing. Both were guesses about a thing the
          // station already knows and reports, and the second one tore the
          // element down 2.5 s after every page load, before the stream it
          // was waiting for had produced a frame, which stopped the stream.
          //
          // Nothing is created or removed here now. All three elements are in
          // the page and a class picks one, so a stream that is still starting
          // is left alone to finish starting.
          if (wrap && s.video) {
            var stream = s.video.stream || {};
            var live = stream.state === "streaming" || stream.state === "starting";
            var still = document.getElementById("preview-still");
            var empty = document.getElementById("preview-empty");
            var age = document.getElementById("preview-age");

            var mode = live ? "live" : (s.video.has_frame ? "still" : "empty");
            wrap.classList.toggle("live", mode === "live");
            wrap.classList.toggle("still", mode === "still");
            wrap.classList.toggle("empty", mode === "empty");

            if (mode === "still" && still) {
              still.src = "/frame.jpg?t=" + Date.now();
            } else if (mode === "empty" && empty) {
              empty.textContent = s.video.reason
                || "No picture from this camera yet.";
            }
            // **Why there is no live picture beats how old the still is.**
            //
            // `startLive` writes the stream's refusal here when /stream.mp4
            // will not open, and this line then overwrote it 2.5 seconds
            // later with "frame 3 s old" — so the one explanation available
            // survived exactly one poll and nobody ever saw it. The station
            // reports the same thing in `video.stream.reason`, which does not
            // evaporate, so it is read from there and it wins whenever there
            // is no live picture to describe.
            if (age) {
              var why = (mode !== "live" && stream.reason) ? stream.reason : "";
              if (why) age.textContent = why;
              else if (mode === "empty") age.textContent = "";
              else if (typeof s.video.frame_age_s === "number") {
                age.textContent = (mode === "live" ? "still " : "frame ")
                  + Math.round(s.video.frame_age_s) + " s old";
              }
            }
          }
        })
        .catch(function () {});
    };
    // The spectrum wants to move, not step: on the radio tab poll fast enough
    // that a carrier appearing is visible, which on a loopback link costs
    // nothing. Everything else stays lazy.
    setInterval(poll, spec ? 300 : 2500);
  }

  // Say that a save is happening.
  //
  // Saving a slot tears the old driver down, builds a new one, and then gives
  // it a sensing tick to report whether it is actually there. On a network
  // camera that is seconds, and until the page came back nothing on screen
  // acknowledged the click at all — so the button read as broken, and the
  // reasonable response to a button that did nothing is to press it again.
  //
  // Deliberately not `disabled`: these buttons carry no name or value, so the
  // label is free to change, but a control disabled from inside its own submit
  // handler stops being announced halfway through the action it is describing.
  // The CSS takes the clicks instead.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.nodeName !== "FORM" || form.getAttribute("data-busy")) return;
    // The radio and device forms never navigate — their own handlers apply each
    // change over fetch and preventDefault the submit — so this "Working…"
    // treatment, meant for a form about to reload the page, does not apply.
    if (form.hasAttribute("data-radio") || form.hasAttribute("data-device")) return;
    var button = (event.submitter && event.submitter.nodeName === "BUTTON")
      ? event.submitter
      : form.querySelector("button[type=submit]");
    form.setAttribute("data-busy", "1");
    if (button) {
      button.setAttribute("aria-busy", "true");
      button.textContent = "Working\\u2026";
    }
  }, true);
})();
"""
        return f"<script nonce='{nonce}'>{script}</script>"

    def _section_events(self, state: dict, csrf: str) -> str:
        """Read straight off the store rather than the snapshot: the snapshot
        carries fifteen events because it also goes over the wire in
        status.json, and this page is the one place the longer history is
        worth its bytes. The store is built to be read from this thread —
        see the check_same_thread note in store.py."""
        events = self.agent.store.recent_events(100)
        zone = datetime.now().astimezone().tzname() or "local time"
        out = [
            "<h2>Recent events (kept on the box)</h2><div class=card>",
            f"<div class=muted>Newest first, at most 100. Times are the "
            f"station's own, {html.escape(zone)}.</div><ul class=log>",
        ]
        for event in events:
            css = {"critical": "bad", "error": "bad", "warning": "warn"}.get(
                event.severity, ""
            )
            when = event.at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            out.append(
                f"<li class='{css}'><code>{when}</code> "
                f"{html.escape(event.kind)} — {html.escape(event.detail)}</li>"
            )
        if not events:
            out.append("<li>nothing yet</li>")
        out.append("</ul>")
        storage = state["storage"]
        out.append(
            f"<div class=muted>{storage['recordings']} audio recording(s), "
            f"{storage['recordings_mb']} MB; {storage['events']} events stored, "
            f"{storage['events_pending']} not yet sent to the platform.</div>"
        )
        out.append(
            "<form method=post action='/transcripts' class=field>"
            + self._csrf_field(csrf) +
            "<button type=submit>Clear transcripts</button>"
            "<span class=muted>Deletes the airband transcripts kept on the box. "
            "Other events are untouched.</span></form>"
        )
        out.append("</div>")
        return "".join(out)

