"""Справочник поддерживаемых валют. Цены везде хранятся в минимальных единицах (minor_unit = число знаков после запятой)."""

CURRENCIES = {
    "EUR": {"name": "Euro", "symbol": "€", "minor_unit": 2},
    "USD": {"name": "US Dollar", "symbol": "$", "minor_unit": 2},
    "ALL": {"name": "Albanian Lek", "symbol": "L", "minor_unit": 2},
    "UAH": {"name": "Ukrainian Hryvnia", "symbol": "₴", "minor_unit": 2},
}

SUPPORTED_CURRENCIES = tuple(CURRENCIES)

# Максимальная цена услуги: 1 000 000.00 в основной валюте
MAX_PRICE = 100_000_000