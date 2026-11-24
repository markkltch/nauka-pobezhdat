"""An existing database from the first release gets the new columns on start."""
import sqlite3
import subprocess
import sys


def test_old_database_is_upgraded(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""
        create table users (id integer primary key, email varchar(254), name varchar(120), role varchar(160),
                            level varchar(8), consent_at datetime, created_at datetime, extra_plans integer default 0);
        insert into users (email, name, level) values ('old@example.ru', 'Старый', 'mid');
        create table situations (id integer primary key, user_id integer, title varchar(160), stage varchar(16),
                                 created_at datetime, updated_at datetime);
    """)
    c.commit(); c.close()
    code = "from app.db import init_db; init_db()"
    r = subprocess.run([sys.executable, "-c", code], env={"DATABASE_URL": f"sqlite:///{db}", "PATH": ""},
                       capture_output=True, text=True, cwd=".")
    assert r.returncode == 0, r.stderr
    c = sqlite3.connect(db)
    row = c.execute("select password_hash, remind_steps, morning_report, team_note from users").fetchone()
    assert row == ("", 1, 1, "")
    assert c.execute("select review from situations").fetchall() == []
