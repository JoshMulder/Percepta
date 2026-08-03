"""The power-history endpoint that feeds the battery popout's two charts.

The state of charge was always here; the flows beneath it — load, solar, and
the two inputs where fitted — are what 0013 added. What these pin down is the
distinction the whole feature turns on: a source that is not fitted comes back
null, not 0, so the chart can leave it off rather than draw a dead line.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from backend.database.models.power_sample import PowerSample


def _sample(org, station, minutes_ago, **flows):
    at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(
        second=0, microsecond=0
    )
    return PowerSample(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        organization_id=org.id,
        ground_station_id=station.id,
        at=at,
        soc_pct=flows.pop("soc_pct", 80.0),
        **flows,
    )


def test_the_flows_come_back_alongside_the_state_of_charge(client, station, db, org):
    db.add(_sample(
        org, station, minutes_ago=2,
        soc_pct=74.0, pv_w=210.0, load_w=180.0, mains_w=0.0, generator_w=90.0,
    ))
    db.commit()

    body = client.get(f"/api/stations/{station.id}/power/history?hours=12").json()

    assert len(body) == 1
    point = body[0]
    assert point["soc"] == 74.0
    assert point["pv"] == 210.0
    assert point["load"] == 180.0
    assert point["mains"] == 0.0
    assert point["gen"] == 90.0


def test_an_unfitted_source_is_null_not_zero(client, station, db, org):
    # A site with no grid and no generator records neither. The chart depends on
    # telling that apart from a fitted input sitting at 0 W.
    db.add(_sample(
        org, station, minutes_ago=1,
        soc_pct=90.0, pv_w=500.0, load_w=120.0, mains_w=None, generator_w=None,
    ))
    db.commit()

    point = client.get(
        f"/api/stations/{station.id}/power/history?hours=12"
    ).json()[0]

    assert point["mains"] is None
    assert point["gen"] is None
    # And the fitted ones are still their real values, 0 W included where real.
    assert point["pv"] == 500.0
    assert point["load"] == 120.0
