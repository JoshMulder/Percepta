"""Changing the address you sign in with.

Two proofs are required and the endpoint is only half of one: the request needs
the account's current password (so a borrowed session cannot move the address),
and the change lands only when a link sent to the *new* address is opened (so
nobody can take an address they do not hold). Nothing about the account moves
until then, which is the property most of these assert.

The SMTP client is mocked throughout — these are about the flow, not about
sending mail — but `email_change.send` itself runs, so the link these tests pull
the token out of is the one a real recipient would get.
"""

from __future__ import annotations

import re
import uuid

from unittest import mock

import pytest

from backend.auth.password import hash_password
from backend.core.email import EmailNotConfiguredError
from backend.database.models.user import User

#: The password conftest gives the `admin` fixture.
PASSWORD = "not-used-by-these-tests"
NEW = "moved@example.test"


def token_from(send) -> str:
    """The token out of the email that was actually composed.

    Read from the body rather than the database on purpose: only the hash is
    stored, so the mailbox is the one place the usable value exists — and going
    through the link is what these tests are for.
    """
    body = send.call_args.kwargs["body_text"]
    match = re.search(r"#token=([A-Za-z0-9_\-]+)", body)
    assert match, f"no verification link in the email body: {body!r}"
    return match.group(1)


def request_change(client, *, email: str = NEW, password: str = PASSWORD):
    with mock.patch("backend.services.email_change.email_service") as svc:
        response = client.post(
            "/api/account/email",
            json={"new_email": email, "current_password": password},
        )
    return response, svc.send


def current_email(db, admin) -> str:
    # The endpoint commits on its own session; drop this one's snapshot before
    # looking, or the assertion reads the row as it was before the request.
    db.rollback()
    return db.get(User, admin.id).email


class TestRequesting:
    def test_it_emails_the_new_address_and_changes_nothing_yet(
        self, client, db, admin
    ):
        response, send = request_change(client)
        assert response.status_code == 202, response.text
        assert response.json()["sent_to"] == NEW

        send.assert_called_once()
        assert send.call_args.kwargs["to"] == NEW
        # The whole point: the account still signs in with the old address until
        # somebody proves they hold the new one.
        assert current_email(db, admin) == "admin@example.test"

    def test_a_wrong_current_password_is_refused(self, client, db, admin):
        response, send = request_change(client, password="not the password")
        assert response.status_code == 400, response.text
        send.assert_not_called()
        assert current_email(db, admin) == "admin@example.test"

    def test_the_address_is_normalised(self, client):
        response, send = request_change(client, email="  Moved@Example.TEST ")
        assert response.status_code == 202, response.text
        assert response.json()["sent_to"] == NEW
        assert send.call_args.kwargs["to"] == NEW

    def test_an_address_already_in_use_is_refused(self, client, db, org):
        db.add(User(
            id=uuid.uuid4(),
            email=NEW,
            display_name="Someone Else",
            first_name="Someone",
            last_name="Else",
            # Long enough for the password policy; never used to sign in here.
            password_hash=hash_password("also-not-used-by-these-tests"),
        ))
        db.commit()

        response, send = request_change(client)
        assert response.status_code == 409, response.text
        send.assert_not_called()

    def test_your_own_address_is_refused(self, client):
        response, send = request_change(client, email="admin@example.test")
        assert response.status_code == 400, response.text
        send.assert_not_called()

    def test_something_that_is_not_an_address_is_refused(self, client):
        response, send = request_change(client, email="not-an-address")
        assert response.status_code == 400, response.text
        send.assert_not_called()

    def test_no_smtp_is_reported_rather_than_reported_as_sent(self, client):
        """A console that says "check your email" when no mail server exists
        leaves somebody waiting on a message that was never going to arrive."""
        with mock.patch("backend.services.email_change.email_service") as svc:
            svc.send.side_effect = EmailNotConfiguredError("no smtp here")
            response = client.post(
                "/api/account/email",
                json={"new_email": NEW, "current_password": PASSWORD},
            )
        assert response.status_code == 503, response.text


class TestRedeeming:
    def test_opening_the_link_moves_the_address(self, client, db, admin):
        _, send = request_change(client)
        token = token_from(send)

        response = client.post(
            "/api/auth/email-change/redeem", json={"token": token}
        )
        assert response.status_code == 200, response.text
        assert response.json()["email"] == NEW
        assert current_email(db, admin) == NEW

    def test_the_link_works_once(self, client, db, admin):
        _, send = request_change(client)
        token = token_from(send)

        first = client.post("/api/auth/email-change/redeem", json={"token": token})
        assert first.status_code == 200, first.text
        second = client.post("/api/auth/email-change/redeem", json={"token": token})
        assert second.status_code == 400, second.text

    def test_a_second_request_supersedes_the_first(self, client, db, admin):
        """A typo'd address must not linger as a live second link. Two
        outstanding links is how the wrong one lands and nobody notices."""
        _, first_send = request_change(client, email="typo@example.test")
        stale = token_from(first_send)
        _, second_send = request_change(client, email=NEW)
        fresh = token_from(second_send)

        assert client.post(
            "/api/auth/email-change/redeem", json={"token": stale}
        ).status_code == 400
        assert client.post(
            "/api/auth/email-change/redeem", json={"token": fresh}
        ).status_code == 200
        assert current_email(db, admin) == NEW

    def test_a_token_nobody_issued_is_refused(self, client):
        response = client.post(
            "/api/auth/email-change/redeem", json={"token": "not-a-real-token"}
        )
        assert response.status_code == 400, response.text

    def test_an_address_taken_between_request_and_redemption_is_refused(
        self, client, db, admin
    ):
        """The uniqueness check at request time is a courtesy; this is the one
        that matters, and without it the redemption is a 500 from the unique
        constraint instead of an answer."""
        _, send = request_change(client)
        token = token_from(send)

        db.add(User(
            id=uuid.uuid4(),
            email=NEW,
            display_name="Got There First",
            first_name="Got",
            last_name="First",
            # Long enough for the password policy; never used to sign in here.
            password_hash=hash_password("also-not-used-by-these-tests"),
        ))
        db.commit()

        response = client.post(
            "/api/auth/email-change/redeem", json={"token": token}
        )
        assert response.status_code == 400, response.text
        assert current_email(db, admin) == "admin@example.test"
