# tar1090 aircraft target icon set

Complete set of ADS-B target silhouettes used by **tar1090** (the popular
dump1090 / readsb web front-end), extracted from `html/markers.js` and
`html/planeObject.js`.

## Contents
- `svg/` — 92 individual shapes, one SVG per shape, named by shape key
  (e.g. `airliner.svg`, `helicopter.svg`, `heavy_2e.svg`, `ground_service.svg`).
- `mapping.json` — the three lookup tables plus per-shape metadata (w, h,
  viewBox, `noRotate`).
- `type-to-icon.csv` — flat ICAO type designator → shape (381 rows, e.g. `A320,a320,1`).
- `category-description-to-icon.csv` — ADS-B emitter category and ICAO
  type-description (WTC) → shape.
- `preview.html` — visual contact sheet of all 92 shapes.

## How a target picks its shape (getBaseMarker)
1. Exact ICAO **type designator** match in `typeDesignatorIcons` (e.g. `B738` → `b738`) — wins if present.
2. Else ICAO **type-description + wake category** in `typeDescriptionIcons`
   (e.g. `L2J-H` = land, 2 jets, heavy → `heavy_2e`).
3. Else ADS-B **emitter category** in `categoryIcons` (e.g. `A5` → `heavy_2e`, `A7` → `helicopter`, `C2` → `ground_service`).
4. Else `unknown`.

Each mapping is `[shapeName, scale]`. Shapes point **north (up)** at heading 0
and are rotated to the aircraft track, except those flagged `noRotate`
(`ground_fixed`, `ground_tower`, station markers) which stay upright.

## Rendering notes
- Use each shape's own `viewBox` (in `mapping.json`); the path coordinate
  space is not the same as the display `w`/`h`.
- These files are filled `#3b82f6` with a thin dark stroke as a neutral
  default — set `fill` per target (tar1090 tints by altitude) and drop the
  stroke if you don't want an outline.

## License / attribution
Source: wiedehopf/tar1090 (https://github.com/wiedehopf/tar1090), **GPL-2.0**.
Keep attribution and comply with GPL-2.0 if you redistribute these shapes.

## Added: quadcopter.svg (custom, not from tar1090)
`svg/quadcopter.svg` is an original top-down quadcopter drawn to match the
set's style — tar1090's only UAV shape (`uav`) is a fixed-wing silhouette.
This one file is NOT tar1090/GPL; it's an original work you may use freely
(public domain / CC0). Front points up (small nose notch); rotate to track,
or leave upright. To use it for drones, point the `B6` ADS-B category and the
`DRON`/`HRON`/`Q1`/`Q4`/`Q9`/`Q25` designators at `quadcopter` instead of `uav`.
