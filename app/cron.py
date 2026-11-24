"""One round of background jobs, for a cron entry on shared hosting:
   */10 * * * * cd ~/www/nauka.ogiinst.ru && ~/nauka-venv/bin/python -m app.cron
"""
from .db import SessionLocal, init_db
from .billing import renew_due
from .jobs import send_reminders


def main() -> None:
    init_db()
    with SessionLocal() as db:
        send_reminders(db)
        renew_due(db)


if __name__ == "__main__":
    main()
