"""One real разбор end to end on the configured model, then the test data is removed.
   cd ~/nauka-app && ~/nauka-venv/bin/python -m app.selftest"""
import time

from sqlalchemy import delete, select

from . import mentor
from .db import SessionLocal, init_db
from .models import Situation, Usage, User

EMAIL = "selftest@nauka.invalid"


def main() -> None:
    init_db()
    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == EMAIL)).first() or User(
            email=EMAIL, name="Самотест", role="Начальник смены на складе", level="line")
        db.add(user)
        db.flush()
        sit = Situation(user_id=user.id)
        db.add(sit)
        db.flush()
        t = time.time()
        mentor.handle(db, user, sit, "Двое кладовщиков в моей смене третью неделю опаздывают на 15–20 минут, "
                                     "из-за этого задерживаем отгрузку первой машины. Как навести порядок?")
        q = sit.messages[-1]
        print(f"вопросы за {time.time() - t:.1f} с: {q.data or q.text}"[:400])
        t = time.time()
        mentor.handle(db, user, sit, "Опаздывают одни и те же, раньше такого не было. Разговаривал один раз "
                                     "мимоходом. Штрафов не вводили, начальник склада в курсе.")
        plan = sit.plan
        print(f"план за {time.time() - t:.1f} с: «{plan.title}»" if plan else f"план не получен: {sit.messages[-1].text[:300]}")
        if plan:
            for s in plan.steps:
                print(f"  — [{s.kind}] {s.text} [{s.owner_name}, до {s.due}]")
            print(f"  глазомер: {plan.scene} | корень: {plan.cause}")
            print(f"  быстрота: {plan.first_day}")
            print(f"  победа: {plan.victory} | риски: {plan.risks}")
            print(f"  принцип: {(plan.principle or {}).get('quote', '')[:120]}")
        cost = mentor.spent(db, sit)
        print(f"стоимость разбора: {cost:.2f} ₽ (потолок {mentor.get_settings().budget_per_situation_rub} ₽)")
        for u in db.scalars(select(Usage).where(Usage.situation_id == sit.id)):
            print(f"  {u.purpose}: {u.model} {u.tokens_in}+{u.tokens_out} ток., {u.cost_rub:.3f} ₽")
        # clean up
        db.execute(delete(Usage).where(Usage.situation_id == sit.id))
        db.delete(sit)  # messages, plans and steps go with it (ORM cascade)
        db.delete(user)
        db.commit()


if __name__ == "__main__":
    main()
