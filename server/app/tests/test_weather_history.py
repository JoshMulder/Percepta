"""The weather-history endpoint that feeds the trend charts.

The twin of `test_power_history`: the same window, the same capability, and the
same distinction it turns on — a sensor a station does not have comes back null,
not 0, so the chart leaves the trace out rather than drawing it flat.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from backend.database.models.weather_sample import WeatherSample


def _sample(org, station, minutes_ago, **fields):
    at = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).replace(
        second=0, microsecond=0
    )
    return WeatherSample(
        id=uuid.uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        organization_id=org.id,
        ground_station_id=station.id,
        at=at,
        **fields,
    )


def test_the_readings_come_back_over_the_window(client, station, db, org):
    db.add(_sample(
        org, station, minutes_ago=2,
        temperature_c=14.2, humidity_pct=71.0, pressure_hpa=1013.0, wind_kt=8.5,
    ))
    db.commit()

    body = client.get(f"/api/stations/{station.id}/weather/history?hours=12").json()

    assert len(body) == 1
    point = body[0]
    assert point["temp"] == 14.2
    assert point["humidity"] == 71.0
    assert point["pressure"] == 1013.0
    assert point["wind"] == 8.5


def test_an_unfitted_sensor_is_null_not_zero(client, station, db, org):
    # A station with no humidity module and no barometer records neither. The
    # chart depends on telling that apart from a real zero.
    db.add(_sample(
        org, station, minutes_ago=1,
        temperature_c=9.0, humidity_pct=None, pressure_hpa=None, wind_kt=0.0,
    ))
    db.commit()

    point = client.get(
        f"/api/stations/{station.id}/weather/history?hours=12"
    ).json()[0]

    assert point["humidity"] is None
    assert point["pressure"] is None
    # A real calm is 0 kt, not missing.
    assert point["wind"] == 0.0
    assert point["temp"] == 9.0
