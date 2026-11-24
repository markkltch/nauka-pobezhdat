import os
import tempfile

_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ.update(DATABASE_URL=f"sqlite:///{_db.name}", LLM_PROVIDER="fake", FREE_BETA="true",
                  ADMIN_EMAILS="boss@example.ru", MONTHLY_PLAN_LIMIT="2", FREE_PLANS_PER_MONTH="1", BASE_URL="http://testserver")
