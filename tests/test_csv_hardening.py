import pytest
from fastapi import HTTPException

from routers.clients import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    _csv_safe,
    _csv_unsafe,
    _read_csv,
)


def test_csv_safe_escapes_formulas_but_keeps_phones():
    assert _csv_safe("=1+1") == "'=1+1"
    assert _csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert _csv_safe("-cmd|x") == "'-cmd|x"
    assert _csv_safe("+380 50 123-45-67") == "+380 50 123-45-67"
    assert _csv_safe("+380501234567") == "+380501234567"
    assert _csv_safe("Иван") == "Иван"
    assert _csv_safe("") == ""


def test_csv_unsafe_roundtrip():
    for value in ["=1+1", "@SUM(A1)", "-cmd|x", "+380501234567", "Иван"]:
        assert _csv_unsafe(_csv_safe(value)) == value


def test_read_csv_handles_cp1251_and_semicolon_and_messy_header():
    raw = "Full_Name; Phone\nИван;+380501234567\n".encode("cp1251")
    header, rows = _read_csv(raw)
    assert header == ["full_name", "phone"]
    assert rows[0]["full_name"] == "Иван"
    assert rows[0]["phone"] == "+380501234567"


def test_read_csv_handles_utf8_bom():
    raw = "\ufefffull_name,phone\nПетро,+380991112233\n".encode("utf-8")
    header, rows = _read_csv(raw)
    assert header == ["full_name", "phone"]
    assert rows[0]["full_name"] == "Петро"


def test_read_csv_rejects_too_big_file():
    with pytest.raises(HTTPException) as exc:
        _read_csv(b"x" * (MAX_IMPORT_BYTES + 1))
    assert exc.value.status_code == 413


def test_read_csv_rejects_too_many_rows():
    lines = ["full_name,phone"] + [f"A{i},+38050{i:07d}" for i in range(MAX_IMPORT_ROWS + 1)]
    with pytest.raises(HTTPException) as exc:
        _read_csv("\n".join(lines).encode("utf-8"))
    assert exc.value.status_code == 400