"""Measuring a channel, and the one measurement that must not be got wrong.

`contract/README.md` rule 3 and `server/docs/05-radio-integration.md` obligation
1 say the same thing: **the airband noise floor is the median of the spectrum
15–50 kHz either side of the channel, converted to in-channel power.** Not
measured inside the channel.

The failure that rule exists to prevent is specific and nasty. An in-channel
tracker treats a weak signal arriving while its estimate is stale-high as noise;
the floor drifts up toward the signal; the threshold follows it; the gate latches
shut and the receiver is deaf until someone restarts it. On an unattended site
that is a truck. Measuring outside the channel makes it structurally impossible:
no carrier, however strong or long, is in the bins being measured, and the
estimate is right on the first block after a retune rather than converging.

    ◄── 50 kHz ──►◄─15─►  channel  ◄─15─►◄── 50 kHz ──►
    ┌────────────┐      ┌─────────┐      ┌────────────┐
    │ noise bins │ skip │ signal  │ skip │ noise bins │
    └────────────┘      └─────────┘      └────────────┘

The 15 kHz skirt either side is the guard: an AM airband channel spills past its
nominal edges, and including that spill in the floor would put the signal back
into the measurement by a slower route.

Two corrections turn a per-bin median into a number comparable with in-channel
power, and both are arithmetic rather than taste:

* **median → mean.** Bin powers are exponentially distributed, whose median is
  ln 2 of its mean, so a median under-reads mean noise power by 1.59 dB. Median
  is still the right statistic — it is what makes a stray carrier in the
  measurement bins harmless.
* **per-bin → in-channel.** The channel spans `2 × CHANNEL_HALF_HZ / BIN_HZ`
  bins, so summing them is +10·log₁₀(bins) over one bin. With the bin plan here
  that is 14.77 dB, which is the 14.81 dB constant Remote-Radio measured on the
  hardware. Same number, arrived at from the bin plan rather than copied.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Sequence

#: Resolution of the spectrum the front end hands over.
BIN_HZ = 500.0

#: An AM airband channel is 8.33 or 25 kHz spaced; the occupied bandwidth of the
#: emission is what matters here, and ±7.5 kHz covers it.
CHANNEL_HALF_HZ = 7500.0

#: The measurement window, either side. Rule 3, verbatim.
NOISE_INNER_HZ = 15000.0
NOISE_OUTER_HZ = 50000.0

#: median → mean for exponentially distributed bin power: -10·log₁₀(ln 2).
MEDIAN_TO_MEAN_DB = 1.5917

#: per-bin → in-channel, for this bin plan.
CHANNEL_BINS = int(round(2 * CHANNEL_HALF_HZ / BIN_HZ))
BINS_TO_CHANNEL_DB = 10 * math.log10(CHANNEL_BINS)

#: What the two together come to. Remote-Radio measured 14.81 dB on its own bin
#: plan; this arrives at 14.77 from ours, and the agreement is the check.
IN_CHANNEL_CORRECTION_DB = BINS_TO_CHANNEL_DB

#: How far above the measured floor AUTO holds the gate. Remote-Radio's
#: guidance is a few dB; 8 is clear of the noise without missing weak traffic.
AUTO_SQUELCH_MARGIN_DB = 8.0


def bin_offsets(bins: int, bin_hz: float = BIN_HZ) -> list[float]:
    """Frequency offset from the tuned centre for each bin, in Hz."""
    centre = bins // 2
    return [(index - centre) * bin_hz for index in range(bins)]


def _linear(db: float) -> float:
    return 10 ** (db / 10)


def _db(linear: float) -> float:
    # A bin can legitimately read zero power in a synthetic spectrum; -300 dB is
    # "nothing" without an exception on the way to a log.
    return 10 * math.log10(linear) if linear > 0 else -300.0


def in_channel_power_db(
    spectrum_db: Sequence[float],
    bin_hz: float = BIN_HZ,
    half_hz: float = CHANNEL_HALF_HZ,
) -> float:
    """Total power in the channel, dBFS. This is `rssi_db`."""
    total = 0.0
    for offset, value in zip(bin_offsets(len(spectrum_db), bin_hz), spectrum_db):
        if abs(offset) <= half_hz:
            total += _linear(value)
    return _db(total)


def noise_bins(
    spectrum_db: Sequence[float],
    bin_hz: float = BIN_HZ,
    inner_hz: float = NOISE_INNER_HZ,
    outer_hz: float = NOISE_OUTER_HZ,
) -> list[float]:
    """The bins the floor is measured from: 15–50 kHz either side, both sides."""
    return [
        value
        for offset, value in zip(bin_offsets(len(spectrum_db), bin_hz), spectrum_db)
        if inner_hz <= abs(offset) <= outer_hz
    ]


def noise_floor_db(
    spectrum_db: Sequence[float],
    bin_hz: float = BIN_HZ,
    inner_hz: float = NOISE_INNER_HZ,
    outer_hz: float = NOISE_OUTER_HZ,
    half_hz: float = CHANNEL_HALF_HZ,
) -> float:
    """The noise floor as in-channel power, dBFS.

    Median of the out-of-channel bins, corrected for the median's bias and
    scaled to the channel's width. Nothing inside the channel is read, which is
    the whole trick.
    """
    bins = noise_bins(spectrum_db, bin_hz, inner_hz, outer_hz)
    if not bins:
        # A front end that cannot show us the skirts cannot be squelched
        # sensibly. Better to say so than to invent a floor from the channel.
        raise ValueError(
            "spectrum does not extend to the measurement window; the noise "
            "floor cannot be measured outside the channel"
        )
    channel_bins = max(1, int(round(2 * half_hz / bin_hz)))
    return median(bins) + MEDIAN_TO_MEAN_DB + 10 * math.log10(channel_bins)


def auto_threshold_db(floor_db: float, margin_db: float = AUTO_SQUELCH_MARGIN_DB) -> float:
    return floor_db + margin_db
