"""Письма владельцу о конце пробного периода и подписки."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import resend
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import models
from core.time_utils import utc_now
from domain.subscription import GRACE_DAYS
from services.billing_notices import SOON_DAYS, STALE_DAYS, notice_stage, send_due_billing_notices
from services.common import to_org_local
from services.i18n import EMAIL_STRINGS, SUPPORTED_LANGUAGES
from services.reminders import reminder_loop
from tests.test_org_data import _add_member

DAY = timedelta(days=1)
KEYS = ["billing_trial_ending", "billing_paid_ending", "billing_grace", "billing_expired"]


@pytest.fixture
def session_factory(db):
    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)


async def _set(db, org, **fields):
    row = await db.get(models.Organization, org.org_id)
    for name, value in fields.items():
        setattr(row, name, value)
    await db.commit()
    return row


async def _trial_ends_in(db, org, delta):
    """Пробный период заканчивается через delta (отрицательный: уже закончился), оплаты нет."""
    return await _set(db, org, trial_ends_at=utc_now() + delta, paid_until=None)


async def _marker(db, org):
    db.expire_all()
    row = await db.get(models.Organization, org.org_id)
    return row.billing_notice_stage, row.billing_notice_for


# ------------------------------------------------------------------ какое письмо актуально

NOW = datetime(2030, 1, 15, 12, 0)


def org_like(**overrides):
    values = dict(plan="basic", trial_ends_at=NOW - 100 * DAY, paid_until=None, is_free=False, is_blocked=False)
    values.update(overrides)
    return SimpleNamespace(**values)


STAGE_CASES = [
    ("trial far", dict(trial_ends_at=NOW + 10 * DAY), None),
    ("trial just outside the window", dict(trial_ends_at=NOW + SOON_DAYS * DAY + timedelta(seconds=1)), None),
    ("trial on the window edge", dict(trial_ends_at=NOW + SOON_DAYS * DAY), "soon"),
    ("trial last hour", dict(trial_ends_at=NOW + timedelta(hours=1)), "soon"),
    ("just ended", dict(trial_ends_at=NOW - timedelta(hours=1)), "grace"),
    ("grace boundary", dict(trial_ends_at=NOW - GRACE_DAYS * DAY), "grace"),
    ("grace over", dict(trial_ends_at=NOW - GRACE_DAYS * DAY - timedelta(seconds=1)), "expired"),
    ("expired, stale edge", dict(trial_ends_at=NOW - (GRACE_DAYS + STALE_DAYS) * DAY), "expired"),
    ("expired, stale", dict(trial_ends_at=NOW - (GRACE_DAYS + STALE_DAYS) * DAY - timedelta(seconds=1)), None),
    ("paid far", dict(paid_until=NOW + 20 * DAY), None),
    ("paid soon", dict(paid_until=NOW + 2 * DAY), "soon"),
    ("paid ended", dict(paid_until=NOW - DAY), "grace"),
    ("free", dict(is_free=True, trial_ends_at=NOW - DAY), None),
    ("blocked", dict(is_blocked=True, trial_ends_at=NOW - DAY), None),
    ("no dates", dict(trial_ends_at=None), None),
]


@pytest.mark.parametrize("name,fields,stage", STAGE_CASES, ids=[c[0] for c in STAGE_CASES])
def test_notice_stage(name, fields, stage):
    assert notice_stage(org_like(**fields), NOW) == stage


# ------------------------------------------------------------------ отправка

async def test_trial_ending_email_goes_to_the_owner_once(db, org_a, sent_emails):
    await _trial_ends_in(db, org_a, timedelta(days=2, hours=3))

    assert await send_due_billing_notices(db) == 1
    assert len(sent_emails) == 1
    mail = sent_emails[0]
    assert mail["to"] == "ownera@example.com"
    assert mail["subject"] == "Your free trial for Shop ownera ends soon"
    assert "Shop ownera" in mail["html"]
    assert "Trial ends" in mail["html"] and "Days left" in mail["html"]
    assert ">3<" in mail["html"]  # 2 дня 3 часа: округляем вверх до 3
    assert (await _marker(db, org_a))[0] == "soon"

    assert await send_due_billing_notices(db) == 0  # повторно не шлём
    assert len(sent_emails) == 1


async def test_nothing_is_sent_while_the_end_is_far(db, org_a, sent_emails):
    await _trial_ends_in(db, org_a, timedelta(days=SOON_DAYS, hours=1))
    assert await send_due_billing_notices(db) == 0
    assert sent_emails == []
    assert (await _marker(db, org_a)) == (None, None)


async def test_paid_subscription_gets_its_own_wording(db, org_a, sent_emails):
    await _set(db, org_a, paid_until=utc_now() + timedelta(days=2), trial_ends_at=utc_now() - 30 * DAY)
    assert await send_due_billing_notices(db) == 1
    assert sent_emails[0]["subject"] == "Your subscription for Shop ownera ends soon"
    assert "Subscription ends" in sent_emails[0]["html"]


async def test_the_three_letters_follow_the_period(db, org_a, sent_emails):
    await _trial_ends_in(db, org_a, timedelta(days=1))
    assert await send_due_billing_notices(db) == 1
    assert [m["subject"] for m in sent_emails] == ["Your free trial for Shop ownera ends soon"]

    await _trial_ends_in(db, org_a, -timedelta(hours=1))  # время идёт: триал закончился
    assert await send_due_billing_notices(db) == 1
    assert sent_emails[-1]["subject"] == "Action needed: access to Shop ownera is about to close"
    assert (await _marker(db, org_a))[0] == "grace"

    await _trial_ends_in(db, org_a, -(GRACE_DAYS * DAY + timedelta(hours=1)))  # льготный период тоже вышел
    assert await send_due_billing_notices(db) == 1
    assert sent_emails[-1]["subject"] == "Access to Shop ownera is closed"
    assert (await _marker(db, org_a))[0] == "expired"

    assert await send_due_billing_notices(db) == 0
    assert len(sent_emails) == 3


async def test_after_a_long_pause_only_the_current_letter_is_sent(db, org_a, sent_emails):
    await _trial_ends_in(db, org_a, -(GRACE_DAYS * DAY + timedelta(hours=1)))
    assert await send_due_billing_notices(db) == 1
    assert [m["subject"] for m in sent_emails] == ["Access to Shop ownera is closed"]


async def test_old_closed_organizations_are_left_alone(db, org_a, sent_emails):
    await _trial_ends_in(db, org_a, -((GRACE_DAYS + STALE_DAYS) * DAY + timedelta(hours=1)))
    assert await send_due_billing_notices(db) == 0
    assert sent_emails == []


async def test_a_payment_starts_the_cycle_again(db, org_a, sent_emails):
    await _trial_ends_in(db, org_a, timedelta(days=1))
    assert await send_due_billing_notices(db) == 1

    await _set(db, org_a, paid_until=utc_now() + 30 * DAY)  # оплатили: конец доступа уехал
    assert await send_due_billing_notices(db) == 0

    await _set(db, org_a, paid_until=utc_now() + timedelta(days=1))  # месяц прошёл: снова «скоро конец»
    assert await send_due_billing_notices(db) == 1
    assert [m["subject"] for m in sent_emails] == [
        "Your free trial for Shop ownera ends soon",
        "Your subscription for Shop ownera ends soon",
    ]


async def test_extension_during_grace_does_not_repeat_old_letters(db, org_a, sent_emails):
    await _trial_ends_in(db, org_a, -timedelta(days=2))
    assert await send_due_billing_notices(db) == 1  # grace
    await _set(db, org_a, paid_until=utc_now() + 30 * DAY)
    assert await send_due_billing_notices(db) == 0
    assert len(sent_emails) == 1


@pytest.mark.parametrize("fields", [dict(is_free=True), dict(is_blocked=True)])
async def test_free_and_blocked_organizations_get_nothing(db, org_a, sent_emails, fields):
    await _trial_ends_in(db, org_a, timedelta(days=1))
    await _set(db, org_a, **fields)
    assert await send_due_billing_notices(db) == 0
    assert sent_emails == []


async def test_only_owners_are_written_to(db, org_a, sent_emails):
    await _add_member(db, org_a, "admin")
    await _add_member(db, org_a, "master")
    await _trial_ends_in(db, org_a, timedelta(days=1))
    assert await send_due_billing_notices(db) == 1
    assert [m["to"] for m in sent_emails] == ["ownera@example.com"]


async def test_every_owner_gets_the_letter_in_their_language(db, org_a, sent_emails):
    owner = (await db.execute(select(models.User).where(models.User.email == "ownera@example.com"))).scalars().one()
    owner.language = "uk"
    second = models.User(username="second", email="second@example.com", password_hash="x", language="xx")
    db.add(second)
    await db.flush()
    db.add(models.Membership(user_id=second.id, organization_id=org_a.org_id, role=models.MembershipRole.owner))
    await db.commit()
    await _trial_ends_in(db, org_a, timedelta(days=1))

    assert await send_due_billing_notices(db) == 1  # считаем организации, а не письма
    by_recipient = {m["to"]: m["subject"] for m in sent_emails}
    assert by_recipient["ownera@example.com"] == "Пробний період для Shop ownera скоро закінчиться"
    assert by_recipient["second@example.com"] == "Your free trial for Shop ownera ends soon"  # неизвестный язык: английский


async def test_date_is_shown_in_the_salon_time_zone(db, org_a, sent_emails):
    end = (utc_now() + timedelta(days=2)).replace(hour=22, minute=30, second=0, microsecond=0)
    await _set(db, org_a, trial_ends_at=end, paid_until=None)  # салон в Киеве: там это уже следующий день
    await send_due_billing_notices(db)
    local = to_org_local(end, "Europe/Kiev").strftime("%d.%m.%Y")
    assert local != end.strftime("%d.%m.%Y")
    assert local in sent_emails[0]["html"]


async def test_grace_letter_shows_when_access_closes(db, org_a, sent_emails):
    end = utc_now() - 2 * DAY
    await _set(db, org_a, trial_ends_at=end, paid_until=None)
    await send_due_billing_notices(db)
    closes = to_org_local(end + GRACE_DAYS * DAY, "Europe/Kiev").strftime("%d.%m.%Y")
    assert closes in sent_emails[0]["html"] and "Access closes" in sent_emails[0]["html"]


async def test_expired_letter_has_no_days_left_row(db, org_a, sent_emails):
    await _trial_ends_in(db, org_a, -(GRACE_DAYS * DAY + timedelta(hours=1)))
    await send_due_billing_notices(db)
    assert "Days left" not in sent_emails[0]["html"]


async def test_organization_name_is_escaped(db, org_a, sent_emails):
    await _set(db, org_a, name="<script>alert(1)</script>", trial_ends_at=utc_now() + DAY, paid_until=None)
    await send_due_billing_notices(db)
    assert "<script>" not in sent_emails[0]["html"]
    assert "&lt;script&gt;" in sent_emails[0]["html"]


async def test_other_organizations_are_not_touched(db, org_a, org_b, sent_emails):
    await _trial_ends_in(db, org_a, timedelta(days=1))
    assert await send_due_billing_notices(db) == 1
    assert [m["to"] for m in sent_emails] == ["ownera@example.com"]
    assert await _marker(db, org_b) == (None, None)


# ------------------------------------------------------------------ сбои и параллельные запуски

async def test_failed_send_is_retried(db, org_a, sent_emails, monkeypatch):
    await _trial_ends_in(db, org_a, timedelta(days=1))
    working = resend.Emails.send

    def broken(*args, **kwargs):
        raise RuntimeError("resend is down")

    monkeypatch.setattr(resend.Emails, "send", staticmethod(broken))
    assert await send_due_billing_notices(db) == 0
    assert await _marker(db, org_a) == (None, None)  # отметка вернулась

    monkeypatch.setattr(resend.Emails, "send", staticmethod(working))
    assert await send_due_billing_notices(db) == 1
    assert len(sent_emails) == 1


async def test_failed_later_letter_keeps_the_earlier_marker(db, org_a, sent_emails, monkeypatch):
    await _trial_ends_in(db, org_a, timedelta(days=1))
    assert await send_due_billing_notices(db) == 1
    marker_before = await _marker(db, org_a)

    await _trial_ends_in(db, org_a, -timedelta(hours=1))
    working = resend.Emails.send
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))))
    assert await send_due_billing_notices(db) == 0
    stage, _ = await _marker(db, org_a)
    assert stage == "soon"  # grace не отправлено, отметка о прошлом письме на месте
    assert marker_before[0] == "soon"

    monkeypatch.setattr(resend.Emails, "send", staticmethod(working))
    assert await send_due_billing_notices(db) == 1
    assert (await _marker(db, org_a))[0] == "grace"


async def test_one_owner_failing_does_not_cancel_the_other(db, org_a, sent_emails, monkeypatch):
    second = models.User(username="second", email="second@example.com", password_hash="x")
    db.add(second)
    await db.flush()
    db.add(models.Membership(user_id=second.id, organization_id=org_a.org_id, role=models.MembershipRole.owner))
    await db.commit()
    await _trial_ends_in(db, org_a, timedelta(days=1))

    def flaky(payload, *args, **kwargs):
        if payload["to"] == "ownera@example.com":
            raise RuntimeError("bounce")
        sent_emails.append(payload)

    monkeypatch.setattr(resend.Emails, "send", staticmethod(flaky))
    assert await send_due_billing_notices(db) == 1
    assert [m["to"] for m in sent_emails] == ["second@example.com"]
    assert (await _marker(db, org_a))[0] == "soon"  # хоть одно письмо ушло: повторно не шлём


async def test_two_parallel_runs_send_one_letter(db, org_a, sent_emails, session_factory):
    await _trial_ends_in(db, org_a, timedelta(days=1))

    async def run():
        async with session_factory() as session:
            return await send_due_billing_notices(session)

    results = await asyncio.gather(run(), run(), run())
    assert sum(results) == 1
    assert len(sent_emails) == 1


async def test_dates_changed_during_the_run_cancel_the_send(db, org_a, sent_emails, monkeypatch, session_factory):
    """Пока шла проверка, организации продлили доступ: письмо «скоро конец» уже неактуально."""
    await _trial_ends_in(db, org_a, timedelta(days=1))
    import services.billing_notices as module
    original = module._find_candidates

    async def find_then_pay(session, now):
        found = await original(session, now)
        async with session_factory() as other:
            row = await other.get(models.Organization, org_a.org_id)
            row.paid_until = utc_now() + 30 * DAY
            await other.commit()
        return found

    monkeypatch.setattr(module, "_find_candidates", find_then_pay)
    assert await send_due_billing_notices(db) == 0
    assert sent_emails == []


# ------------------------------------------------------------------ фоновый цикл и тексты

async def test_background_loop_sends_billing_notices(db, org_a, sent_emails, session_factory):
    await _trial_ends_in(db, org_a, timedelta(days=1))
    task = asyncio.create_task(reminder_loop(session_factory, interval_seconds=3600, initial_delay=0))
    for _ in range(50):
        if sent_emails:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sent_emails) == 1


async def test_loop_keeps_sending_notices_when_reminders_break(db, org_a, sent_emails, session_factory, monkeypatch):
    async def broken(session):
        raise RuntimeError("boom")

    monkeypatch.setattr("services.reminders.send_due_reminders", broken)
    await _trial_ends_in(db, org_a, timedelta(days=1))
    task = asyncio.create_task(reminder_loop(session_factory, interval_seconds=3600, initial_delay=0))
    for _ in range(50):
        if sent_emails:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sent_emails) == 1


def test_all_languages_have_the_same_keys():
    for key in KEYS:
        reference = set(EMAIL_STRINGS[key]["en"])
        assert reference >= {"subject", "heading", "body", "label_date", "label_days", "button", "footer"}
        for lang in SUPPORTED_LANGUAGES:
            assert set(EMAIL_STRINGS[key][lang]) == reference, (key, lang)
            assert all(EMAIL_STRINGS[key][lang][k].strip() for k in reference), (key, lang)
            assert "{organization_name}" in EMAIL_STRINGS[key][lang]["subject"]


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
@pytest.mark.parametrize("key", KEYS)
def test_every_template_renders_in_every_language(key, lang):
    from services.billing_notices import send_billing_notice_email  # noqa: F401
    from services.email_service import render_email

    html = render_email(f"{key}.html", lang, organization_name="Salon X", date_text="01.02.2030", days_left=2,
                        open_url="https://example.com/")
    texts = EMAIL_STRINGS[key][lang]
    assert "Salon X" in html and "01.02.2030" in html
    assert texts["heading"] in html and texts["label_date"] in html and texts["label_days"] in html
    assert 'href="https://example.com/"' in html
    assert "{" not in html and "}" not in html  # не осталось неподставленных значений
