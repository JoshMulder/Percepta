"""Issuing the code a technician types into a box.

The code used to be offered with two other facts folded into it —
`CODE@host#fingerprint`, one string to carry. It does not fit: a station's
`token` field is capped at 64 characters by the contract, and the combined
string is a hundred and three on any real host. Somebody handed a string pastes
the string, so every paste of the thing the console told people to paste was a
validation failure at the far end.

That cap is the reason this file exists. It lives in the station's code, one
repository boundary away, where nothing on this side would notice it moving.
"""

from __future__ import annotations

import uuid

#: `ClaimRequest.token` in `backend/api/enrolment.py`, which is the contract's
#: limit. Restated rather than imported so that a change to it has to be made
#: deliberately in two places, one of which is a test that says why.
TOKEN_LIMIT = 64


def issue(client, station) -> dict:
    response = client.post(f"/api/stations/{station.id}/enrolment/token")
    assert response.status_code == 201, response.text
    return response.json()


def test_the_code_is_the_whole_of_what_is_offered(client, station):
    issued = issue(client, station)
    assert set(issued) == {"token", "expires_at"}, (
        "an extra field here is something an installer will paste"
    )


def test_the_code_fits_the_field_it_is_typed_into(client, station):
    # The failure this guards is not subtle at the far end and is invisible at
    # this one: the station sends the token, the platform's own claim endpoint
    # refuses it for length, and the technician is told "the box sent something
    # the platform could not read. This is a bug."
    assert len(issue(client, station)["token"]) <= TOKEN_LIMIT


def test_it_carries_no_address_and_no_fingerprint(client, station):
    token = issue(client, station)["token"]
    assert "@" not in token, "an address folded into the code"
    assert "#" not in token, "a CA fingerprint folded into the code"


def test_issuing_again_supersedes_rather_than_accumulating(client, station):
    """Two live codes for one station is a way to enrol the wrong box and not
    find out."""
    first = issue(client, station)["token"]
    second = issue(client, station)["token"]
    assert first != second

    status = client.get(f"/api/stations/{station.id}/enrolment").json()
    assert status["token_outstanding"] is True
    assert status["token_claimed"] is False


def test_a_code_for_a_station_that_is_not_there_is_a_404(client):
    response = client.post(f"/api/stations/{uuid.uuid4()}/enrolment/token")
    assert response.status_code == 404
