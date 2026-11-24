"""Entry point for reg.ru shared hosting (ispmanager + Passenger, WSGI).

Copy this file into the site folder (~/www/nauka.ogiinst.ru). The code, .env and the
database live outside the public folder, in ~/nauka-app; the virtualenv in ~/nauka-venv.
FastAPI speaks ASGI; a2wsgi runs it inside the WSGI server the hosting provides.
"""
import os
import sys

APP_DIR = os.path.expanduser(os.environ.get("NAUKA_APP_DIR", "~/nauka-app"))
VENV = os.path.expanduser(os.environ.get("NAUKA_VENV", "~/nauka-venv"))
PY = f"python{sys.version_info.major}.{sys.version_info.minor}"
sys.path.insert(0, APP_DIR)
sys.path.insert(1, os.path.join(VENV, "lib", PY, "site-packages"))
os.chdir(APP_DIR)  # .env and the sqlite file are read from the app folder

from a2wsgi import ASGIMiddleware  # noqa: E402

from app.main import app as asgi_app  # noqa: E402

application = ASGIMiddleware(asgi_app)
