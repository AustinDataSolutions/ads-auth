"""Sending the package's own emails (password-reset links) through an SMTP relay such as Brevo.

Settings come from the same five variables the ADS apps already use, ideally from one shared Render
environment group: `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`.
A blank `SMTP_HOST` turns email off. Messages are plain text and go out over STARTTLS.

Nothing here logs an address, a subject, or a body. Framework-free: no Streamlit, no I/O at import.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

LOGGER = logging.getLogger(__name__)
SENDER_NAME = "Austin Data Solutions"
TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class MailSettings:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    sender: str

    @property
    def from_header(self) -> str:
        """`EMAIL_FROM` as given when it already carries a name, else "Austin Data Solutions <address>"."""
        name, address = parseaddr(self.sender)
        return self.sender if name else formataddr((SENDER_NAME, address))


def mail_from_environ(environ) -> MailSettings | None:
    """The SMTP settings, or None when `SMTP_HOST` is blank. Raises ValueError when the host is set
    but another value is missing, so a half-configured service fails at startup, not at first send."""
    host = (environ.get("SMTP_HOST", "") or "").strip()
    if not host:
        return None
    values = {name: (environ.get(name, "") or "").strip() for name in ("SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError(f"SMTP_HOST is set but these are missing: {', '.join(missing)}.")
    try:
        port = int((environ.get("SMTP_PORT", "") or "587").strip())
    except ValueError as exc:
        raise ValueError("SMTP_PORT must be a number.") from exc
    return MailSettings(host, port, values["SMTP_USER"], values["SMTP_PASSWORD"], values["EMAIL_FROM"])


def send(mail: MailSettings, to: str, subject: str, body: str) -> None:
    """Send one plain-text message now. smtplib and socket errors propagate."""
    message = EmailMessage()
    message["From"] = mail.from_header
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(mail.host, mail.port, timeout=TIMEOUT_SECONDS) as server:
        server.starttls(context=ssl.create_default_context())
        server.login(mail.user, mail.password)
        server.send_message(message)


def send_later(mail: MailSettings, to: str, subject: str, body: str) -> threading.Thread:
    """Send on a background thread, so the screen answers equally fast whether or not an email goes
    out. A failure is logged as a warning with the error type only."""
    def run():
        try:
            send(mail, to, subject, body)
        except Exception as exc:          # noqa: BLE001 - reported, never raised into a dead thread
            LOGGER.warning("An email could not be sent (%s).", exc.__class__.__name__)
    thread = threading.Thread(target=run, name="ads-auth-mail", daemon=True)
    thread.start()
    return thread
