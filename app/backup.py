"""Nightly copy of the SQLite database, consistent even while the site writes to it.
   cron:  17 3 * * * cd ~/nauka-app && ~/nauka-venv/bin/python -m app.backup
Keeps 14 days in ~/nauka-backups."""
import datetime as dt
import sqlite3
from pathlib import Path

from .config import get_settings

KEEP_DAYS = 14


def main() -> None:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise SystemExit("backup.py copies SQLite only; use mysqldump/pg_dump for other databases")
    src = Path(url.removeprefix("sqlite:///"))
    out_dir = Path.home() / "nauka-backups"
    out_dir.mkdir(exist_ok=True)
    target = out_dir / f"nauka-{dt.date.today():%Y-%m-%d}.db"
    with sqlite3.connect(src) as a, sqlite3.connect(target) as b:
        a.backup(b)
    with sqlite3.connect(target) as b:  # prove it opens
        assert b.execute("pragma integrity_check").fetchone()[0] == "ok"
    cutoff = dt.date.today() - dt.timedelta(days=KEEP_DAYS)
    for old in out_dir.glob("nauka-*.db"):
        try:
            if dt.date.fromisoformat(old.stem.removeprefix("nauka-")) < cutoff:
                old.unlink()
        except ValueError:
            pass
    print(target)


if __name__ == "__main__":
    main()
