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
  enhancement only — live save buttons, a refreshing datastream field, the
  camera preview's re-fetch on Devices, and Escape-to-close on Connection —
  and every page keeps working with them blocked or absent. The two overlays
  are the proof: the preview's click-to-expand is a checkbox and the location
  editor is a `:target` dialog, neither of which is script
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
from .radio.receiver import FREQ_MAX_HZ, FREQ_MIN_HZ
from .setup_access import COOKIE_NAME, Gate, is_loopback_host

log = logging.getLogger("gsu.console")

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
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; "
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
 .btn.danger, button.danger { border-color: var(--danger); color: var(--danger);
   background: rgba(255,122,69,.08); }
 .btn.danger:hover, button.danger:hover { background: rgba(255,122,69,.18); }
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
        if self.gate.window_minutes <= 0 and not is_loopback_host(host):
            log.warning(
                "GSU_SETUP_WINDOW_MINUTES=0: the setup page on %s will stay "
                "open for as long as this station runs.", host,
            )
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
            if path.startswith("/frame.jpg"):
                return self._send_frame(handler, cookie)
            if path in ("/index.html", "/login"):
                path = "/"
            if path in PAGES:
                slot = None
                chosen = None
                nonce = None
                if path == "/devices":
                    # The one page that carries an inline script. Minted here
                    # and per response, so the header and the tag can agree
                    # and nothing injected into rendered content can guess it.
                    #
                    # Connection dropped off this list when its location
                    # dialog went inline: no dialog, no Escape handler, no
                    # script, and so no script-src in its policy at all.
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
        if not self.gate.check_csrf(session, (form.get("csrf") or [""])[0]):
            # Almost always a stale tab rather than an attack, and the wording
            # says so — but it is refused either way.
            log.warning("Setup POST from %s had no valid CSRF token.", peer)
            self.message = ("bad", "That page had gone stale. Reload and try again.")
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
            elif path == "/reset":
                self._reset(form)
            else:  # /logout
                self.gate.forget_all()
                self.message = ("good", "Signed out.")
        except Exception as exc:  # noqa: BLE001 - shown to a person
            self.message = ("bad", str(exc))
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
        """The one thing about this box's position that is still set here.

        Coordinates and elevation are settled at commissioning and frozen with
        the enrolment: a station that needs different ones has physically
        moved, and a box that has moved is recommissioned rather than edited.

        What remains is whether to *use* the elevation — the ADS-B barometric
        correction, which is computed on this box from this box's barometer.
        That is a local behaviour switch rather than a fact about the site, and
        it is the one thing here somebody at the mast might reasonably change
        after the fact.
        """
        # An unchecked checkbox sends nothing at all, so its absence from a
        # submission of *this* form is a real "off". That only holds because
        # the input is inside this form and always rendered.
        correction = bool(form.get("adsb_baro_correction"))
        if correction and self.agent.effective_elevation_m() is None:
            # Refused rather than accepted-and-idle. A checkbox that stays
            # ticked while nothing happens is how somebody comes to trust a
            # number that was never computed — and the fix is not here, it is
            # on the platform, so the message has to say so.
            raise ValueError(
                "This station has no elevation, so altitudes cannot be "
                "corrected. Set one on the platform and re-enrol."
            )
        self.agent.site.adsb_baro_correction = correction
        self.agent.site.save(self.agent.config.site_config_path)
        self.message = ("good", "Saved.")

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
        self.agent.inventory.set_device(slot, type_id, params, resource)
        # Rebuild immediately: an installer who changes a port expects to see
        # within seconds whether the box can now talk to the thing.
        self.agent.build_devices()
        if slot == "camera" and getattr(self.agent, "_stream_holds_camera",
                                        lambda: False)():
            # The rebuild deliberately leaves the camera alone while the live
            # stream holds the sensor (agent.build_devices); say so rather
            # than reporting the old driver's state as this save's outcome.
            self.message = ("good", f"{slot}: saved. Applies when the live "
                                    f"stream stops.{note}")
            return slot
        report = {r.slot: r for r in self.agent.inventory.report()}[slot]
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
        out = [
            "<!doctype html><meta charset=utf-8>",
            "<meta name=viewport content='width=device-width,initial-scale=1'>",
            f"<title>Ground station — {PAGES.get(page, 'Summary')}</title>",
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
        elif page == "/devices":
            slot = slot if slot in registry.SLOTS else registry.SLOTS[0]
            out.append(self._section_devices(state, csrf, slot, chosen))
            if slot == "camera":
                out.append(self._section_camera(state))
            if nonce:
                out.append(self._devices_script(nonce))
        elif page == "/logging":
            out.append(self._section_events(state))
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

    def _section_enrol(self, state: dict, csrf: str) -> str:
        if state["enrolled"]:
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
        return (
            "<p class=sub>Not set up yet.</p>"
            "<div class=card><form method=post action='/enrol'>"
            + self._csrf_field(csrf) +
            # In a .field like every other row on the page, rather than the
            # label-<br>-input it was: this is the one form an installer sees
            # first, and it was the one that lined up with nothing.
            "<div class=field>"
            "<label for=token>Enter the code you were given</label>"
            "<input id=token class=code name=token type=text autocomplete=off "
            "placeholder='XXXX-XXXX-XXXX' autofocus>"
            "</div>"
            "<div class=field><button type=submit>Set this station up</button></div>"
            "</form></div>"
        )

    def _section_location(self, state: dict, csrf: str, banner: str = "") -> str:
        """Where this box is, and the one local thing about it worth setting.

        **Position is read-only.** It is settled when the station enrols and
        frozen afterwards: a station that needs a different one has physically
        moved, and a box that has moved is recommissioned rather than edited.
        The rows state what it was issued, with the platform's own words for
        those coordinates beside them so somebody at the site can check the
        position matches the site they are standing at.

        **Elevation is not, and that is not an inconsistency.** It is measured
        at the mast rather than issued, and it exists for the ADS-B barometric
        correction, which is computed on this box from this box's barometer.
        The switch for that correction sits directly under it because they are
        one decision — the correction refuses to run without the elevation.

        **No dialog.** There was one, and it was right while this held three
        coordinates and duplicated the rows above it. With the position frozen
        it holds a number and a checkbox, and putting two controls behind an
        overlay, a fragment target and a focus dance is more machinery than the
        thing it hides. Inline, they are simply the rest of the card.
        """
        position = state.get("position") or {}
        station = position.get("station") or {}
        elevation = position.get("elevation_m")
        rows = [
            ("Position", *self._position_wording(position)),
        ]
        out = ["<h2>Where this box is</h2><div class=card>"]
        for label, value, css in rows:
            out.append(
                f"<div class=row><span class=k>{html.escape(label)}</span>"
                f"<span class='{css}'>{html.escape(str(value))}</span></div>"
            )
        out.append(banner)
        # Elevation is a row, not a field. It is part of the position — the
        # correction below is computed from it, and a correction referenced to
        # the wrong height is out by that height on every aircraft — so it is
        # set at commissioning with the coordinates and frozen with them.
        out.append(
            "<div class=row><span class=k>Elevation</span>"
            + (f"<span class=ok>{html.escape(_degrees(elevation))} m</span>"
               if elevation is not None
               else "<span class=warn>not set</span>")
            + "</div>"
        )
        # No banner here. It existed to put a refusal *inside* the dialog;
        # with the dialog gone the page's own banner is the only one, and
        # emitting it twice is how a single refusal read as two.
        out.append(f"<form method=post action='/location'>{self._csrf_field(csrf)}")
        checked = " checked" if station.get("adsb_baro_correction") else ""
        out.append(
            "<div class=field><label for='adsb_baro_correction'>"
            "Correct altitudes</label>"
            "<input type=checkbox id='adsb_baro_correction' "
            f"name='adsb_baro_correction' value='1'{checked}>"
            "<span class=muted>Uses this station's barometer. "
            "Needs an elevation.</span></div>"
        )
        out.append(
            "<div class=field><button type=submit>Save</button></div></form>"
            "<div class=muted>Position is set when this box is enrolled. "
            "Re-enrol to move it.</div></div>"
        )
        return "".join(out)

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
        out.append(
            "<div class=field><a class='btn danger' href='#reset'>Reset "
            "station</a></div>"
        )
        out.append(
            "<div id=reset class=confirm>"
            f"<form method=post action='/reset'>{self._csrf_field(csrf)}"
            "<input type=hidden name=confirm value='yes'>"
            "<div class=field><button type=submit class=danger>"
            "Erase everything on this box</button>"
            "<a class='btn quiet' href='#'>Cancel</a></div></form></div>"
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
            return ("Broker security", "TLS, system CA bundle (not pinned)", "warn")
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
        out.append(
            "<div class=slot-head><strong>"
            f"{html.escape(SLOT_LABELS.get(slot, slot.title()))}</strong>"
            f"<span class='pill {css}'>{html.escape(wording)}</span></div>"
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
        # split above — so the act of picking leaves nothing for the dirty
        # check to notice: the type lives in a hidden field, which the
        # fingerprint skips, and every visible field already equals its
        # freshly-rendered default. The result was a page offering a device
        # you could select and then not save, with no field you could touch to
        # release the button. Switching back to a demo device was impossible.
        pending = chosen_id != ((entry.type_id or "") if entry else "")
        out.append(
            f"<form method=post action='/device' data-device"
            f"{' data-changed' if pending else ''}>"
            f"<input type=hidden name=slot value='{slot}'>"
            f"<input type=hidden name=type_id value='{html.escape(chosen_id)}'>"
        )
        out.append(self._csrf_field(csrf))

        selected_device = registry.get(chosen_id) if chosen_id else None
        if selected_device is not None:
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
        # Enabled without script (degradation the design accepts); the nonce'd
        # script disables it until a field differs from its loaded value. In a
        # .field with no label so that it sits under the controls rather than
        # under the labels — the only child of a row goes to column 2.
        out.append("<div class=field><button type=submit>Save</button></div></form>")

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
            # The same spectrum the console draws, on the box itself — this is
            # the page somebody has open while pointing an antenna, and a
            # number in a list cannot show them a carrier appearing. Fixed
            # size, refreshed from status.json by the nonce'd script; with no
            # script it is a still of the moment the page was rendered.
            radio = self.agent.radio
            out.append(
                f"<form method=post action='/radio'>{self._csrf_field(csrf)}"
                "<div class=field><label for='freq_mhz'>Frequency</label>"
                "<input type=number step='0.001' id='freq_mhz' name='freq_mhz' "
                f"inputmode=decimal placeholder='MHz' value='"
                f"{radio.freq_hz / 1e6:.3f}" if radio else ""
            )
            out.append(
                "'></div>"
                "<div class=field><label for='monitor'>Hold gate open</label>"
                "<input type=checkbox id='monitor' name='monitor' value='1'"
                + (" checked" if radio and radio.monitor else "")
                + "><span class=muted>Bypasses the squelch, for bringing an "
                "antenna up.</span></div>"
                "<div class=field><button type=submit>Apply</button></div></form>"
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
        out.append(
            "<label for=zoom class=preview id=preview-wrap>"
            "<video id=preview autoplay muted playsinline></video>"
            "<noscript>The live preview needs JavaScript. "
            "<a href='/frame.jpg'>Latest still</a></noscript></label>"
        )
        if video.get("has_frame"):
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

        Three jobs, all progressive enhancement over a page that already works
        without it: the save button goes disabled until a field differs from
        its loaded value, and the datastream field — or, on the camera tab,
        the frame preview and its age — refreshes from status.json, same auth
        gate as every page, every 2.5 seconds, nothing off-box (the CSP's
        connect-src enforces that). Password fields count as changed when
        non-empty: their loaded value is never in the page to compare
        against, by design. The preview image is re-fetched with a timestamp
        query because the response is no-store and the browser still needs
        the src to change before it asks again.
        """
        script = """
"use strict";
(function () {
  function fingerprint(form) {
    var out = [];
    var fields = form.querySelectorAll("input, select");
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      if (f.type === "hidden") continue;
      if (f.type === "checkbox") out.push(f.name + "=" + f.checked);
      else if (f.type === "password") out.push(f.name + "=" + (f.value ? "!" : ""));
      else out.push(f.name + "=" + f.value);
    }
    return out.join("&");
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
  var forms = document.querySelectorAll("form[data-device]");
  for (var i = 0; i < forms.length; i++) {
    (function (form) {
      var button = form.querySelector("button[type=submit]");
      if (!button) return;
      var loaded = fingerprint(form);
      // A pending device change is already a change, whatever the fields say.
      var picked = form.hasAttribute("data-changed");
      button.disabled = !picked;
      var update = function () {
        button.disabled = !picked && fingerprint(form) === loaded;
      };
      form.addEventListener("input", update);
      form.addEventListener("change", update);
    })(forms[i]);
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
          if (raw && s.raw_samples) {
            var lines = s.raw_samples[raw.getAttribute("data-slot")] || [];
            raw.textContent = lines.join("\\n");
          }
          if (wrap && s.video && s.video.has_frame) {
            var shown = document.getElementById("preview");
            // The live element owns this id now, and this poll used to assign
            // /frame.jpg to whatever it found — a JPEG as a video source, over
            // the top of the stream, every 2.5 seconds. The element reported
            // MEDIA_ERR_SRC_NOT_SUPPORTED and nothing ever played.
            var playing = shown && shown.tagName === "VIDEO" && !shown.error;
            if (!playing) {
              // No live picture: fall back to the still, which is what this
              // did before and is better than an empty box.
              if (!shown || shown.tagName === "VIDEO") {
                wrap.textContent = "";
                shown = document.createElement("img");
                shown.id = "preview";
                shown.alt = "latest camera frame";
                wrap.appendChild(shown);
              }
              shown.src = "/frame.jpg?t=" + Date.now();
            }
            var age = document.getElementById("preview-age");
            if (age && typeof s.video.frame_age_s === "number") {
              age.textContent = (playing ? "still " : "frame ")
                + Math.round(s.video.frame_age_s) + " s old";
            }
          }
        })
        .catch(function () {});
    };
    setInterval(poll, 2500);
  }
})();
"""
        return f"<script nonce='{nonce}'>{script}</script>"

    def _section_events(self, state: dict) -> str:
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
        out.append("</div>")
        return "".join(out)

