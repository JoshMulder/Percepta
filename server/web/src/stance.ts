import type { FleetStation } from "./types";

/**
 * STANCE: the wall spends its area.
 *
 * The grid this replaces treats every station identically and sizes itself for a
 * fleet that does not exist — five 11rem columns declared for three stations,
 * most of the wall empty, and a camera still rendered too small to tell you
 * anything. The premise here is that **screen area is a rationed channel exactly
 * like colour**: it should be spent on the stations that have earned it, and a
 * healthy fleet should not be able to claim the same area as a failing one.
 *
 * So this is not a grid with a responsive column count. It is an ALLOCATOR that
 * hands out one of four FORMS, worst-first. The forms are not one tile at four
 * sizes — each is a different content contract, because what is worth saying
 * about a station changes with how much room it has:
 *
 *   STRIP  the whole width. Near-native poster beside a ledger of plain words.
 *   PANEL  poster on top, an opaque ledge of readings beneath.
 *   CARD   poster as a band, name and organisation under it.
 *   CHIP   one line. No picture at all.
 *
 * THE POSTER'S NATIVE SIZE IS A CEILING, NOT A TARGET. The station sends
 * 480x270 (`gsu/poster.POSTER_HEIGHT`). Rendering that at 900px does not show
 * more of the site, it shows the same site blurrier — a lie about how much you
 * can see. STRIP therefore stops at roughly native and spends its remaining
 * width on 34px names and words like "offline for 2h" instead. If the source
 * ever grows, the ceiling moves; the layout does not have to.
 *
 * AND NO TEXT IS EVER LAID OVER A PICTURE. The scrim in the current tile exists
 * to solve text-over-uncontrolled-image, and it has been wrong twice: first
 * painting under the image and dimming nothing, then multiplying with the
 * image's own opacity and leaving 2% contrast. Every form here puts the picture
 * in a bounded region with the words beside or beneath it, which deletes the
 * problem rather than tuning the fix.
 */

/**
 * `hidden` is a station the wall has run out of room for. It is not a form —
 * nothing is drawn — and it exists because the alternative is worse.
 *
 * Beyond a few hundred stations the allocation rule's own invariant ("everyone
 * below me still gets a chip") becomes unsatisfiable, and a greedy walk that
 * simply keeps failing that test degenerates: EVERY station falls to a chip,
 * including the one that is on fire, and the wall shows no picture at exactly
 * the moment it exists to show one. Collapsing the calmest stations into a
 * count is what the old grid already did past sixty; this keeps that behaviour
 * and states the rule that governs it — a case is NEVER hidden, only a nominal
 * station is, and only from the quiet end.
 */
export type Form = "strip" | "panel" | "card" | "chip" | "hidden";

/**
 * What each form costs, in units of one CHIP.
 *
 * Taken from the forms' own pixel areas rather than invented, so the ratios stay
 * true if the sizes are retuned: chip 310x26, card 250x175, panel 630x400,
 * strip 1280x290.
 */
export const COST: Record<Form, number> = {
  strip: 46,
  panel: 31,
  card: 5.4,
  chip: 1,
  // Nothing is drawn, so nothing is spent. Listed rather than left out so the
  // map stays exhaustive over `Form` — a new form added without a cost should
  // be a type error, not a silent zero.
  hidden: 0,
};

/**
 * The wall's area, in the same chip units — a centre column of roughly
 * 1280x900. A parameter rather than a constant because it is the one number a
 * test needs to move, and because a wall panel and a laptop are not the same
 * wall.
 */
export const DEFAULT_BUDGET = 143;

const ORDER: Form[] = ["strip", "panel", "card", "chip"];

/**
 * Whether this station is quiet enough to be counted rather than drawn.
 *
 * Deliberately the SAME predicate the existing wall collapses on, re-exported
 * from here rather than re-derived: two definitions of "nominal" on one screen
 * is two answers to "is that site fine", and the disagreement would show up as
 * a station that is collapsed by one rule and promoted by the other.
 */
export function isNominal(s: FleetStation): boolean {
  if (s.dark || s.status !== "online") return false;
  if (s.health != null && s.health !== "ok") return false;
  if ((s.condition_count ?? 0) > 0) return false;
  if (s.worst_condition) return false;
  if (s.uplink_connected === false) return false;
  if (s.soc_pct != null && s.soc_pct <= 30) return false;
  if (s.slots) {
    for (const state of Object.values(s.slots)) {
      if (state !== "present" && state !== "absent") return false;
    }
  }
  return true;
}

/**
 * Assign a form to every station, worst-first.
 *
 * `ranked` must already be in the wall's own order — this does not sort. It is
 * given the sorted list because the sort is the caller's (it depends on alerts,
 * which this module has no business knowing about), and because a function that
 * both sorts and allocates would hide which of the two put a station where.
 *
 * TWO REGIMES, and the split is what stops a healthy fleet claiming the area a
 * failing one needs:
 *
 * **Nothing is wrong** — every station gets the SAME form, the largest that fits
 * them all. A uniform wall is the correct picture of a uniform fleet, and it is
 * also the only arrangement with no arbitrary winner: with thirty healthy sites,
 * promoting two of them to full-width strips because they happen to sort first
 * would spend the wall's loudest signal on alphabetical order.
 *
 * **Something is wrong** — the cases are handed area worst-first by the rule the
 * concept is built on: each takes the largest form it can have such that every
 * station below it still gets at least a chip. Nominal stations are then capped
 * at CARD however much room is left, because area is a signal and a fine station
 * has not earned it.
 *
 * `promote` is the open drawer's station, which keeps a drawn form whatever its
 * condition — a tile that shrinks to a chip at the moment you click it, because
 * opening it is how you learned it was fine, is a wall arguing with its operator.
 */
export function allocate(
  ranked: FleetStation[],
  { budget = DEFAULT_BUDGET, promote }: { budget?: number; promote?: string | null } = {},
): Form[] {
  const n = ranked.length;
  if (n === 0) return [];

  const cased = ranked.map(
    (s) => !isNominal(s) || (promote != null && s.id === promote),
  );

  if (!cased.some(Boolean)) {
    // Uniform. The largest form that fits the whole fleet at once.
    const form = ORDER.find((f) => COST[f] * n <= budget);
    if (form) return ranked.map(() => form);
    // More stations than the wall can hold even as chips. Draw what fits and
    // count the rest — with nothing wrong anywhere, which of the calm sites is
    // drawn carries no meaning, so rank order is as good an answer as any.
    const room = Math.floor(budget / COST.chip);
    return ranked.map((_, i) => (i < room ? "chip" : "hidden"));
  }

  // **Kept back before anything is handed out.** Without this the worst station
  // competes with the fleet's own length for room, and on a large fleet it
  // loses: the tail of chips consumes the budget and the lead is reduced to a
  // chip alongside them. A wall whose whole premise is spending area on trouble
  // must reserve the trouble's area first.
  const lead = COST.panel;
  // A promoted station is one the operator has open. It is a case by
  // definition here, but it sorts wherever its health puts it — usually last,
  // being fine — so it needs its own reservation or the walk reaches it with
  // nothing left. A card, not a panel: it earns a picture, not the wall.
  const held = promote != null && ranked.some((s) => s.id === promote) ? COST.card : 0;
  const capacity = Math.max(0, Math.floor((budget - lead - held) / COST.chip));

  // Cases are never hidden; nominal stations fill what is left, worst-first.
  const drawn = ranked.map((_, i) => cased[i]);
  let count = drawn.filter(Boolean).length;
  for (let i = 0; i < n && count < capacity; i += 1) {
    if (!drawn[i]) {
      drawn[i] = true;
      count += 1;
    }
  }

  const forms: Form[] = [];
  let spent = 0;
  for (let i = 0; i < n; i += 1) {
    if (!drawn[i]) {
      forms.push("hidden");
      continue;
    }
    const below = drawn.slice(i + 1).filter(Boolean).length;
    const promoted = promote != null && ranked[i].id === promote;
    // A fine station may never take more than a card, however empty the wall.
    const ceiling = cased[i] && !promoted ? 0 : ORDER.indexOf("card");
    const reserved = promoted ? 0 : held;
    const pick =
      ORDER.slice(ceiling).find(
        (f) => spent + COST[f] + reserved + below * COST.chip <= budget,
      ) ?? "chip";
    forms.push(pick);
    spent += COST[pick];
  }
  return forms;
}

/**
 * Which stations are worth asking for a picture.
 *
 * A CHIP has nowhere to put one, so a station on a chip must not be on camera
 * duty — that is a field station opening its lens once a minute, on a board
 * whose supply already cannot hold its own peak, for a row of text. This is the
 * same argument the collapsed-nominal exclusion already makes on the old wall,
 * applied to the form rather than to the collapse.
 */
export function withPosters(ranked: FleetStation[], forms: Form[]): string[] {
  return ranked
    .filter((_, i) => forms[i] !== "chip" && forms[i] !== "hidden")
    .map((s) => s.id);
}
