"""Проверка прода от начала до конца: регистрация → почта → салон → онлайн-запись → письма → перенос → отмена →
колокольчик → аналитика → удаление тестовых данных.

Запуск (из любой папки, нужен только пакет requests — он уже есть в requirements.txt):
    python3 smoke_prod.py https://<адрес бэкенда> you+smoke@gmail.com

Используйте свой настоящий ящик с «+что-угодно» (Gmail доставит на основной адрес): скрипт попросит вставить токены
из писем. На любом вопросе можно просто нажать Enter, тогда шаг с письмом будет пропущен.
В конце скрипт удаляет тестовый салон и тестовый аккаунт.
"""
import secrets
import sys
import time
from datetime import date, datetime, timedelta

import requests

if len(sys.argv) != 3:
    print(__doc__)
    sys.exit(1)

BASE = sys.argv[1].rstrip("/") + "/api/v1"
EMAIL = sys.argv[2]
PASSWORD = "Smoke" + secrets.token_hex(6) + "9!"
SUFFIX = secrets.token_hex(3)
ORG_NAME = f"Smoke test {SUFFIX}"

session = requests.Session()
session.headers["User-Agent"] = "Clientum-smoke-test/1.0"
results: list[tuple[str, bool, str]] = []
state: dict = {}


def step(name):
    """Декоратор шага: печатает результат и время, ошибку не роняет весь прогон (кроме критичных шагов)."""
    def wrap(func):
        def run(critical=False):
            started = time.perf_counter()
            try:
                note = func() or ""
                ok = True
            except Exception as error:  # noqa: BLE001 — показываем любую ошибку как провал шага
                note, ok = str(error)[:300], False
            ms = (time.perf_counter() - started) * 1000
            mark = "OK  " if ok else "FAIL"
            print(f"[{mark}] {name} ({ms:.0f} ms){' — ' + note if note else ''}")
            results.append((name, ok, note))
            if critical and not ok:
                finish()
            return ok
        return run
    return wrap


def call(method, path, *, expect=(200,), auth=True, **kwargs):
    headers = kwargs.pop("headers", {})
    if auth and "token" in state:
        headers["Authorization"] = f"Bearer {state['token']}"
    response = session.request(method, BASE + path, headers=headers, timeout=60, **kwargs)
    if response.status_code not in expect:
        raise RuntimeError(f"{method} {path} → {response.status_code}: {response.text[:300]}")
    return response


def ask(prompt):
    try:
        return input(f"\n  ➜ {prompt}\n    (Enter — пропустить): ").strip()
    except EOFError:
        return ""


def org(path):
    return f"/organizations/{state['org_id']}{path}"


# ------------------------------------------------------------------ шаги
@step("health")
def health():
    r = session.get(BASE.replace("/api/v1", "") + "/health", timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"/health → {r.status_code}")


@step("регистрация")
def register():
    call("POST", "/users", expect=(201,), auth=False, json={
        "username": f"smoke-{SUFFIX}", "email": EMAIL, "password": PASSWORD, "accept_terms": True, "language": "uk"})


@step("вход и обновление токена")
def login():
    r = call("POST", "/users/token", auth=False, data={"username": EMAIL, "password": PASSWORD})
    tokens = r.json()
    r = call("POST", "/users/refresh", auth=False, json={"refresh_token": tokens["refresh_token"]})
    state["token"] = r.json()["access_token"]
    state["refresh"] = r.json()["refresh_token"]
    call("POST", "/users/refresh", expect=(401,), auth=False, json={"refresh_token": tokens["refresh_token"]})
    return "старый refresh-токен отклонён, как и должно быть"


@step("подтверждение почты по письму")
def verify():
    print(f"\n  Письмо «Підтвердіть пошту» отправлено на {EMAIL}.")
    answer = ask("Откройте письмо, нажмите кнопку (и «Підтвердити» на открывшейся странице), потом напишите 'да'.\n"
                 "    Если ссылка ведёт в никуда — вставьте сюда токен из адреса ссылки (всё после token=)")
    if not answer:
        return "пропущено"
    if answer.lower() not in ("да", "yes", "y", "д"):
        call("POST", "/users/verify-email", expect=(204,), auth=False, json={"token": answer})
    if not call("GET", "/users/me").json()["email_verified"]:
        raise RuntimeError("email_verified всё ещё false")
    return "почта подтверждена"


@step("салон, услуга, мастер с графиком")
def setup_shop():
    r = call("POST", "/organizations", expect=(201,), json={"name": ORG_NAME, "timezone": "Europe/Tirane"})
    state["org_id"], state["slug"] = r.json()["id"], r.json()["slug"]
    r = call("POST", org("/services"), expect=(201,), json={"name": "Smoke cut", "price": 1500, "duration_minutes": 30})
    state["service_id"] = r.json()["id"]
    hours = [{"day_of_week": d, "start_time": "08:00", "end_time": "20:00"} for d in range(7)]
    r = call("POST", org("/masters"), expect=(201,), json={
        "full_name": "Smoke master", "working_hours": hours, "service_ids": [state["service_id"]]})
    state["master_id"] = r.json()["id"]
    sub = call("GET", org("/subscription")).json()
    return f"slug={state['slug']}, подписка {sub['status']}"


@step("публичная страница: салон, услуги, мастера")
def public_info():
    info = call("GET", f"/public/{state['slug']}", auth=False).json()
    if not info.get("booking_enabled", True):
        raise RuntimeError("booking_enabled=false")
    services = call("GET", f"/public/{state['slug']}/services", auth=False).json()
    masters = call("GET", f"/public/{state['slug']}/masters", auth=False).json()
    if not services or not masters:
        raise RuntimeError("нет услуг или мастеров")


@step("свободные дни и слоты")
def slots():
    start = date.today() + timedelta(days=1)
    dates = call("GET", f"/public/{state['slug']}/available-dates", auth=False, params={
        "service_id": state["service_id"], "from": start.isoformat(), "to": (start + timedelta(days=6)).isoformat()}).json()
    if not dates:
        raise RuntimeError("нет свободных дней")
    state["day"] = dates[0]
    free = call("GET", f"/public/{state['slug']}/available-slots", auth=False, params={
        "service_id": state["service_id"], "date": state["day"]}).json()
    if len(free) < 3:
        raise RuntimeError("слишком мало слотов")
    state["slots"] = [s["start_time"] for s in free]
    return f"{len(dates)} дн., {len(free)} слотов на {state['day']}"


@step("онлайн-запись (любой мастер)")
def book():
    r = call("POST", f"/public/{state['slug']}/book", expect=(201,), auth=False, json={
        "client_full_name": "Smoke client", "client_phone": f"+35569{secrets.randbelow(10**7):07d}",
        "client_email": EMAIL, "service_id": state["service_id"], "start_time": state["slots"][2],
        "language": "uk", "website": ""})
    state["appointment_id"] = r.json()["id"]
    return f"запись #{state['appointment_id']} на {state['slots'][2]}"


@step("колокольчик: уведомление о новой записи")
def bell():
    count = call("GET", org("/notifications/unread-count")).json()["unread"]
    if count < 1:
        raise RuntimeError("уведомления нет")


@step("ссылка из письма-подтверждения: просмотр, перенос, отмена")
def manage():
    token = ask("Откройте письмо «Запис підтверджено». Проверьте, что кнопка ведёт на /booking/<токен>,\n"
                "    и вставьте сюда токен (часть адреса после /booking/)")
    if not token:
        return "пропущено"
    info = call("GET", f"/public/booking/{token}", auth=False).json()
    if not info["can_modify"]:
        raise RuntimeError("can_modify=false")
    call("POST", f"/public/booking/{token}/reschedule", auth=False, json={"start_time": state["slots"][4]})
    after = call("POST", f"/public/booking/{token}/cancel", auth=False).json()
    if after["status"] != "cancelled":
        raise RuntimeError(f"статус {after['status']}")
    return "перенесено и отменено — должны прийти письма «Запис змінено» и «Запис скасовано»"


@step("лист ожидания")
def waitlist():
    call("POST", f"/public/{state['slug']}/waitlist", expect=(201,), auth=False, json={
        "service_id": state["service_id"], "day": state["day"], "client_full_name": "Smoke waiter",
        "client_phone": f"+35568{secrets.randbelow(10**7):07d}", "client_email": EMAIL, "language": "uk"})
    entries = call("GET", org("/waitlist")).json()
    if not entries:
        raise RuntimeError("заявки нет в CRM")


@step("CRM: записи, календарь, клиенты")
def crm():
    day = datetime.fromisoformat(state["day"])
    call("GET", org("/appointments"))
    call("GET", org("/appointments/calendar"), params={
        "date_from": day.isoformat(), "date_to": (day + timedelta(days=1)).isoformat()})
    clients = call("GET", org("/clients")).json()
    call("GET", org("/clients/summary"))
    return f"клиентов: {len(clients)}"


@step("аналитика одним запросом")
def analytics():
    today = date.today()
    started = time.perf_counter()
    call("GET", org("/analytics/dashboard"), params={
        "date_from": f"{today - timedelta(days=30)}T00:00:00", "date_to": f"{today + timedelta(days=7)}T23:59:59"})
    return f"ответ за {(time.perf_counter() - started) * 1000:.0f} ms"


@step("удаление тестового салона")
def delete_org():
    if "org_id" in state:
        call("POST", org("/delete"), expect=(204,), json={"password": PASSWORD, "confirm_name": ORG_NAME})


@step("удаление тестового аккаунта")
def delete_account():
    if "token" in state:
        call("POST", "/users/me/delete", expect=(204,), json={"password": PASSWORD})


def finish():
    delete_org()
    delete_account()
    failed = [name for name, ok, _ in results if not ok]
    print("\n" + ("Всё прошло." if not failed else f"Провалено шагов: {len(failed)} — {', '.join(failed)}"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    print(f"Проверяю {BASE} (ящик {EMAIL})\n")
    health(critical=True)
    register(critical=True)
    login(critical=True)
    verify()
    setup_shop(critical=True)
    public_info()
    slots(critical=True)
    book()
    bell()
    manage()
    waitlist()
    crm()
    analytics()
    finish()