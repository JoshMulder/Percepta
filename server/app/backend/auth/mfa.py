"""TOTP second factor.

Ported from DroneOps, including the reasoning, so the two products behave the
same way for anyone who administers both.

The secret is stored encrypted at rest (`EncryptedString` on the user row).
Encryption is what makes a database backup not a set of second factors - a TOTP
secret is a permanent credential, unlike a code, and a leaked one is silent.

The QR renders to an SVG data URI on the server. That keeps a QR library out of
the console and needs no more than `img-src data:` in the CSP - a client-side
generator would be another dependency handling a secret.
"""

import base64
import io

import pyotp
import segno

TOTP_ISSUER = "Percepta"


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(*, secret: str, account_name: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(
        name=account_name, issuer_name=TOTP_ISSUER
    )


def verify_code(*, secret: str | None, code: str) -> bool:
    if not secret or not code:
        return False
    # valid_window=1 tolerates about 30 seconds of clock skew between the server
    # and the phone. A station site is remote and its people are not; their
    # phones are the thing least likely to be synchronised.
    return pyotp.TOTP(secret).verify(code.strip().replace(" ", ""), valid_window=1)


def qr_svg_data_uri(uri: str) -> str:
    """The provisioning URI as an SVG QR code in a data: URI."""
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="svg", scale=5, border=2)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"
