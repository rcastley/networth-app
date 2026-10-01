"""Currency validation, formatting, conversion, and Frankfurter rate retrieval."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


SUPPORTED_CURRENCIES: tuple[str, ...] = (
    "AUD", "BGN", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP",
    "HKD", "HUF", "IDR", "ILS", "INR", "ISK", "JPY", "KRW", "MXN", "MYR",
    "NOK", "NZD", "PHP", "PLN", "RON", "SEK", "SGD", "THB", "TRY", "USD",
    "ZAR",
)

CURRENCY_NAMES = {
    "AUD": "Australian dollar",
    "BGN": "Bulgarian lev",
    "BRL": "Brazilian real",
    "CAD": "Canadian dollar",
    "CHF": "Swiss franc",
    "CNY": "Chinese yuan",
    "CZK": "Czech koruna",
    "DKK": "Danish krone",
    "EUR": "Euro",
    "GBP": "British pound",
    "HKD": "Hong Kong dollar",
    "HUF": "Hungarian forint",
    "IDR": "Indonesian rupiah",
    "ILS": "Israeli new shekel",
    "INR": "Indian rupee",
    "ISK": "Icelandic króna",
    "JPY": "Japanese yen",
    "KRW": "South Korean won",
    "MXN": "Mexican peso",
    "MYR": "Malaysian ringgit",
    "NOK": "Norwegian krone",
    "NZD": "New Zealand dollar",
    "PHP": "Philippine peso",
    "PLN": "Polish złoty",
    "RON": "Romanian leu",
    "SEK": "Swedish krona",
    "SGD": "Singapore dollar",
    "THB": "Thai baht",
    "TRY": "Turkish lira",
    "USD": "US dollar",
    "ZAR": "South African rand",
}

_SYMBOLS = {
    "EUR": "€",
    "GBP": "£",
    "JPY": "¥",
    "USD": "$",
}
_API_BASE = "https://api.frankfurter.dev/v1"
_MAX_RESPONSE_BYTES = 128 * 1024
_RATE_QUANTUM = Decimal("0.0000000001")
_MONEY_QUANTUM = Decimal("0.01")
_MIN_RATE = _RATE_QUANTUM
_MAX_RATE = Decimal("9999999999.9999999999")


class FxError(RuntimeError):
    """A safe, user-displayable exchange-rate failure."""


@dataclass(frozen=True)
class RateSet:
    effective_date: date
    rates_per_eur: dict[str, Decimal]
    source: str = "ecb"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise FxError("The exchange-rate service returned an unexpected redirect.")


def normalize_currency(value: object) -> str:
    code = str(value or "").strip().upper()
    if code not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported currency code: {code or '(empty)'}")
    return code


def currency_options() -> list[tuple[str, str]]:
    return [(code, CURRENCY_NAMES[code]) for code in SUPPORTED_CURRENCIES]


def normalize_rate(value: object) -> Decimal:
    try:
        rate = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FxError("Exchange rates must be valid decimal numbers.") from exc
    if not rate.is_finite() or rate < _MIN_RATE or rate > _MAX_RATE:
        raise FxError(
            "Exchange rates must be positive and within the supported range."
        )
    quantized = rate.quantize(_RATE_QUANTUM, rounding=ROUND_HALF_UP)
    if quantized < _MIN_RATE or quantized > _MAX_RATE:
        raise FxError(
            "Exchange rates must be positive and within the supported range."
        )
    return quantized


def format_currency(value: object, currency_code: str = "GBP") -> str:
    if value is None:
        return "–"
    code = normalize_currency(currency_code)
    try:
        amount = Decimal(value).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return "–"
    absolute = abs(amount)
    prefix = _SYMBOLS.get(code, f"{code} ")
    rendered = f"{prefix}{absolute:,.2f}"
    return f"({rendered})" if amount < 0 else rendered


def convert(
    amount: object,
    source_currency: str,
    target_currency: str,
    rates_per_eur: dict[str, Decimal],
) -> Decimal:
    source = normalize_currency(source_currency)
    target = normalize_currency(target_currency)
    value = Decimal(amount)
    if not value.is_finite():
        raise FxError("Monetary values must be finite numbers.")
    if source == target:
        return value
    try:
        source_rate = Decimal(rates_per_eur[source])
        target_rate = Decimal(rates_per_eur[target])
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise FxError(f"No saved {source}/{target} exchange rate is available.") from exc
    if (
        not source_rate.is_finite()
        or not target_rate.is_finite()
        or source_rate <= 0
        or target_rate <= 0
    ):
        raise FxError("Saved exchange rates must be greater than zero.")
    return (value / source_rate * target_rate).quantize(
        _MONEY_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def direct_rate(
    source_currency: str,
    target_currency: str,
    rates_per_eur: dict[str, Decimal],
) -> Decimal:
    source = normalize_currency(source_currency)
    target = normalize_currency(target_currency)
    if source == target:
        return Decimal("1")
    return (Decimal(rates_per_eur[target]) / Decimal(rates_per_eur[source])).quantize(
        _RATE_QUANTUM,
        rounding=ROUND_HALF_UP,
    )


def manual_rate_set(
    reporting_currency: str,
    direct_rates: dict[str, Decimal],
    effective_date: date | None = None,
) -> RateSet:
    """Build a synthetic cross-rate set from native-to-reporting manual rates."""
    reporting = normalize_currency(reporting_currency)
    rates = {reporting: Decimal("1")}
    for raw_code, raw_rate in direct_rates.items():
        code = normalize_currency(raw_code)
        rate = normalize_rate(raw_rate)
        try:
            rates[code] = normalize_rate(Decimal("1") / rate)
        except FxError as exc:
            raise FxError(
                f"The manual {code}/{reporting} rate is outside the supported range."
            ) from exc
    return RateSet(effective_date or date.today(), rates, "manual")


def rate_set_supports(rate_set: RateSet, currencies: set[str]) -> bool:
    return all(
        code in rate_set.rates_per_eur
        and rate_set.rates_per_eur[code].is_finite()
        and rate_set.rates_per_eur[code] > 0
        for code in currencies
    )


def fetch_rate_set(on_date: date | None = None) -> RateSet:
    """Fetch a bounded, non-redirecting EUR-based daily rate set."""
    endpoint = "latest" if on_date is None else on_date.isoformat()
    url = f"{_API_BASE}/{endpoint}"
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "networth-app/0.3"},
        method="GET",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=6) as response:
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise FxError("The exchange-rate service returned an invalid response.")
            payload_bytes = response.read(_MAX_RESPONSE_BYTES + 1)
    except FxError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise FxError("Current exchange rates are unavailable. Enter manual rates to continue.") from exc

    if len(payload_bytes) > _MAX_RESPONSE_BYTES:
        raise FxError("The exchange-rate service response was unexpectedly large.")
    try:
        payload = json.loads(payload_bytes)
        effective = datetime.strptime(payload["date"], "%Y-%m-%d").date()
        raw_rates = payload["rates"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise FxError("The exchange-rate service returned malformed data.") from exc
    if not isinstance(raw_rates, dict):
        raise FxError("The exchange-rate service returned malformed rate data.")

    rates: dict[str, Decimal] = {"EUR": Decimal("1")}
    for raw_code, raw_rate in raw_rates.items():
        code = str(raw_code).upper()
        if code not in SUPPORTED_CURRENCIES:
            continue
        try:
            value = Decimal(str(raw_rate))
        except InvalidOperation as exc:
            raise FxError(f"The exchange-rate service returned an invalid {code} rate.") from exc
        if not value.is_finite() or value <= 0:
            raise FxError(f"The exchange-rate service returned an invalid {code} rate.")
        rates[code] = normalize_rate(value)

    if "GBP" not in rates or "USD" not in rates:
        raise FxError("The exchange-rate service response omitted required currencies.")
    return RateSet(effective, rates)
