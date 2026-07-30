/**
 * Which station a socket message is about, and whether it belongs on screen.
 *
 * The socket is scoped server-side by `select_station`, which made this look
 * unnecessary. It is not, because the scoping is not instantaneous: between the
 * client sending `select_station` and the server acting on it, frames for the
 * station being left are already in flight. The console clears every reading on
 * a switch, so those frames land in a freshly emptied panel and populate it.
 *
 * For a stream the new station also publishes, the next frame overwrites the
 * mistake within a second or two and nobody sees it. For a stream the new
 * station does **not** publish, nothing ever overwrites it: the value sits
 * there for the life of the session, under the wrong station's name, looking
 * exactly like a reading.
 *
 * That is how a Pi with no weather head came to be showing wind, temperature
 * and pressure — a single in-flight frame from a demo station, five seconds
 * before the switch, and no second frame to correct it.
 *
 * Extracted from the handler rather than written inline because the handler is
 * a `useCallback` with no dependencies — deliberately stable, so recreating it
 * does not churn the socket — which means it cannot close over the selected
 * station and has to be told. A pure function is also the part worth testing.
 */

/** The subset of a server message this decision needs. */
export type Addressed = { station_id?: string };

/**
 * True if a message should be applied to the console's state.
 *
 * `selected` is null before the first station is chosen; nothing is applied
 * then, because there is no panel yet for it to belong to.
 *
 * A message with no `station_id` is allowed through: `hello`,
 * `station_selected` and `station_revoked` are about the connection rather than
 * about a station's readings, and dropping them would break selection itself.
 * Only a message that names a station is checked against the selection — an
 * unaddressed *reading* does not exist in the protocol, and if one ever
 * appeared it would be a server bug rather than something to silently discard.
 */
export function isForSelectedStation(
  message: Addressed,
  selected: string | null,
): boolean {
  if (message.station_id === undefined) return true;
  return selected !== null && message.station_id === selected;
}
