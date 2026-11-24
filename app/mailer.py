"""Outgoing mail. Without SMTP settings every letter goes to the log (development)."""
import logging
import os
import smtplib
import ssl
import subprocess
from email.utils import parseaddr
from email.message import EmailMessage

from .config import get_settings

log = logging.getLogger("mail")


def send(to: str, subject: str, text: str) -> None:
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if s.mail_via_sendmail and os.path.exists(s.sendmail_path):
        # shared hosting: the local MTA, no mailbox password needed
        # -f sets the envelope sender: without it exim sends MAIL FROM:<> (a bounce),
        # which fails SPF/DMARC alignment and Yandex drops or spams the letter
        envelope_from = parseaddr(s.smtp_from)[1]
        subprocess.run([s.sendmail_path, "-t", "-i", "-f", envelope_from], input=msg.as_bytes(), check=True, timeout=30)
        return
    if not s.smtp_host:
        log.warning("MAIL (dev, not sent) to=%s subject=%s\n%s", to, subject, text)
        return
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
