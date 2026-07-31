# Third-party assets bundled into the console

Anything under `vendor/` is not ours. This file records what, from where, and
under what terms — because the console is built into a single bundle, and once
that has shipped the provenance is no longer visible in it.

## tar1090 aircraft icons — GPL-2.0

`vendor/tar1090-icons/` holds the ADS-B target silhouettes from
[wiedehopf/tar1090](https://github.com/wiedehopf/tar1090), extracted from its
`html/markers.js` and `html/planeObject.js`, together with its three lookup
tables and per-shape metadata.

**Licence: GPL-2.0.** Fourteen of the 93 shapes are compiled into
`src/adsbIcons.ts` by `scripts/gen-adsb-icons.py` and therefore ship inside the
console bundle. Redistributing that bundle redistributes them, and GPL-2.0
attaches to it.

**Not yet resolved:** the console has no user-facing attribution surface, so
nothing in the running application names tar1090 or offers its licence text.
That needs a home — the map's attribution control is the obvious one, since it
already carries the basemap credits — before the console is distributed outside
this organisation. Raised at the point of adoption rather than found later.

`svg/quadcopter.svg` is **not** from tar1090. It is an original top-down
quadcopter drawn to match the set's style, released CC0, and is what the ADS-B
UAV category is drawn with here — tar1090's own `uav` shape is a fixed-wing
silhouette, which is the wrong picture for a platform that exists to watch
drones.
