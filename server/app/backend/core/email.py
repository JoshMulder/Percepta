"""Outbound email (SMTP).

Dormant by default. With no SMTP configured a send raises rather than returning
quietly, because the failure mode that matters here is a password reset that the
console reports as sent and that never arrives - the person waiting for it has no
way to tell the difference between "not sent" and "in your spam folder", and
will wait a long time before asking.

Nothing here initiates mail on its own. Every send is a user-triggered action.

In development, point this at the Mailpit container in docker-compose (SMTP on
1025, a web inbox on 8025). No credentials, no TLS, nothing leaves the host, and
every message is readable in a browser - which also makes it the only honest way
to check what these mails actually look like.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage

from backend.core.config import settings

log = logging.getLogger(__name__)


class EmailNotConfiguredError(RuntimeError):
    """Raised when a send is attempted while SMTP is not configured."""


class EmailService:
    @property
    def enabled(self) -> bool:
        return settings.email_enabled

    def _build(
        self,
        *,
        to: str | list[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = settings.smtp_from
        message["To"] = to if isinstance(to, str) else ", ".join(to)
        message["Subject"] = subject
        message.set_content(body_text)
        if body_html:
            message.add_alternative(body_html, subtype="html")
        return message

    def send(
        self,
        *,
        to: str | list[str],
        subject: str,
        body_text: str,
        body_html: str | None = None,
    ) -> None:
        if not self.enabled:
            raise EmailNotConfiguredError(
                "Email is not configured. Set SMTP_HOST and SMTP_FROM in .env "
                "and restart. In development, docker-compose runs Mailpit: "
                "SMTP_HOST=mailpit, SMTP_PORT=1025, SMTP_FROM=percepta@localhost."
            )

        message = self._build(
            to=to, subject=subject, body_text=body_text, body_html=body_html
        )

        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                context=ssl.create_default_context(),
                timeout=20,
            ) as server:
                self._authenticate(server)
                server.send_message(message)
        else:
            with smtplib.SMTP(
                settings.smtp_host, settings.smtp_port, timeout=20
            ) as server:
                if settings.smtp_use_tls:
                    server.starttls(context=ssl.create_default_context())
                self._authenticate(server)
                server.send_message(message)

        # The address is not logged. A log line naming who was sent a reset is a
        # list of accounts worth attacking, sitting in a file with looser access
        # than the database it came from. The audit trail records it properly.
        log.info("Sent %r via %s.", subject, settings.smtp_host)

    def _authenticate(self, server: smtplib.SMTP) -> None:
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)


email_service = EmailService()
