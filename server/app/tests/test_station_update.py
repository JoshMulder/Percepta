"""The station.update command endpoint (DECISIONS item 48).

The station runs only signature-verified images, so the endpoint's job is to
dispatch the target to the box and audit who did it — behind its own capability,
which an org admin holds. publish_sync is mocked: these exercise the endpoint and
its validation, not the Redis relay (which the other command endpoints share and
which has no fixture here).
"""

from unittest import mock

_DIGEST = "sha256:" + "a" * 64
_IMAGE = "ghcr.io/joshmulder/percepta-gsu"


def test_it_dispatches_the_command(client, station):
    with mock.patch("backend.api.commands.publish_sync", return_value=True) as pub:
        response = client.post(
            f"/api/stations/{station.id}/update",
            json={"image": _IMAGE, "digest": _DIGEST, "tag": "v0.2.0"},
        )
    assert response.status_code == 202, response.text
    channel, command = pub.call_args.args
    assert command == {
        "kind": "system.update", "image": _IMAGE,
        "digest": _DIGEST, "tag": "v0.2.0",
    }


def test_force_is_passed_through_when_set(client, station):
    with mock.patch("backend.api.commands.publish_sync", return_value=True) as pub:
        response = client.post(
            f"/api/stations/{station.id}/update",
            json={"image": _IMAGE, "digest": _DIGEST, "force": True},
        )
    assert response.status_code == 202, response.text
    _, command = pub.call_args.args
    assert command["force"] is True
    assert "tag" not in command  # omitted, not sent empty


def test_a_malformed_digest_is_rejected(client, station):
    response = client.post(
        f"/api/stations/{station.id}/update",
        json={"image": _IMAGE, "digest": "not-a-digest"},
    )
    assert response.status_code == 422, response.text


def test_an_unreachable_station_is_a_503(client, station):
    with mock.patch("backend.api.commands.publish_sync", return_value=False):
        response = client.post(
            f"/api/stations/{station.id}/update",
            json={"image": _IMAGE, "digest": _DIGEST},
        )
    assert response.status_code == 503, response.text
