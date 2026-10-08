from datetime import UTC, datetime


def utc_now() -> datetime:
    """Текущее время UTC без указания пояса, в том же виде, в каком время хранится в базе.

    Для служебных меток (сроки токенов и приглашений, created_at, deleted_at).
    Время записей клиентов считается иначе, по часовому поясу организации (get_org_now).
    """
    return datetime.now(UTC).replace(tzinfo=None)