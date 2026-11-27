"""Outgoing mail. Without SMTP settings every letter goes to the log (development)."""
import logging
import os
import smtplib
import ssl
import subprocess
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

from .config import get_settings

log = logging.getLogger("mail")


def build(to: str, subject: str, text: str, html: str | None = None, unsubscribe: str | None = None) -> EmailMessage:
    """A letter that passes spam filters: text and HTML parts, Date, a Message-ID on the sender's own domain,
    Reply-To a mailbox people read, and a one-click unsubscribe for letters a person can turn off."""
    s = get_settings()
    sender = parseaddr(s.smtp_from)[1]
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1] if "@" in sender else None)
    if s.mail_reply_to:
        msg["Reply-To"] = s.mail_reply_to
    if unsubscribe:
        msg["List-Unsubscribe"] = f"<{unsubscribe}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg


def send_letter(to: str, letter) -> None:
    _deliver(build(to, letter.subject, letter.text, letter.html, letter.unsubscribe))


def send(to: str, subject: str, text: str) -> None:
    _deliver(build(to, subject, text))


def _deliver(msg: EmailMessage) -> None:
    """SMTP of the mailbox in From when it is set (its server signs the letter with DKIM, so it reaches the
    inbox); the hosting's sendmail otherwise; the log in development."""
    s = get_settings()
    if s.smtp_host:
        ctx = ssl.create_default_context()
        if s.smtp_ssl:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ctx, timeout=20) as smtp:
                smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                smtp.starttls(context=ctx)
                smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
        return
    if s.mail_via_sendmail and os.path.exists(s.sendmail_path):
        # -f sets the envelope sender: without it exim sends MAIL FROM:<> (a bounce),
        # which fails SPF/DMARC alignment and Yandex drops or spams the letter
        envelope_from = parseaddr(s.smtp_from)[1]
        subprocess.run([s.sendmail_path, "-t", "-i", "-f", envelope_from], input=msg.as_bytes(), check=True, timeout=30)
        return
    log.warning("MAIL (dev, not sent) to=%s subject=%s\n%s", msg["To"], msg["Subject"], msg.get_body(("plain",)).get_content())
