"""Input validation / normalisation shared by the API, CLI and importers.

Every write goes through a Schema so that a CSV import, a REST call and a CLI
command all reject the same bad data with the same messages.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE = re.compile(r"^https?://\S+$", re.I)
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str] | str):
        self.errors = errors if isinstance(errors, dict) else {"_": errors}
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


class NotFound(LookupError):
    pass


# ---------------------------------------------------------------- coercers
# Each takes a raw value (str from CSV/form, or JSON-typed) and returns the
# DB value or raises ValueError with a human message.

MAX_CENTS = 10 ** 15  # $10 trillion; keeps values far inside SQLite's 64-bit INTEGER


def _scalar(v):
    if isinstance(v, (dict, list, tuple, set)):
        raise ValueError("must be a single value, not a list or object")
    return v


def text(max_len: int = 2000) -> Callable[[Any], str]:
    def f(v):
        s = "" if v is None else str(_scalar(v)).strip()
        if len(s) > max_len:
            raise ValueError(f"must be at most {max_len} characters")
        return s
    return f


def email(v) -> str:
    s = text(320)(v)
    if s and not EMAIL_RE.match(s):
        raise ValueError("is not a valid email address")
    return s.lower()


def url(v) -> str:
    s = text(2000)(v)
    if s and not re.match(r"^[a-z]+://", s, re.I):
        s = "https://" + s
    if s and not URL_RE.match(s):
        raise ValueError("is not a valid URL")
    return s


def phone(v) -> str:
    s = text(40)(v)
    if s and not re.fullmatch(r"[0-9+()\-.\s xX#*]+", s):
        raise ValueError("contains invalid characters")
    return s


# US-style m/d/Y is tried before d.m.Y; ISO always wins.
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d.%m.%Y", "%Y/%m/%d", "%Y%m%d", "%b %d %Y", "%B %d %Y", "%d %b %Y")
MIN_YEAR, MAX_YEAR = 1000, 9999


def iso_date(v) -> str | None:
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        d = v.date()
    elif isinstance(v, date):
        d = v
    else:
        d = _parse_date(str(_scalar(v)).strip().replace(",", ""))
    if not MIN_YEAR <= d.year <= MAX_YEAR:
        raise ValueError(f"year must be between {MIN_YEAR} and {MAX_YEAR}")
    return d.isoformat()  # always zero-padded YYYY-MM-DD (strftime isn't, for years < 1000)


def _parse_date(s: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:  # full ISO timestamps
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        raise ValueError("is not a recognised date (use YYYY-MM-DD)") from None


def cents(v) -> int:
    """'$1,234.5' / '12' / 12.34 / '(5.00)' -> integer cents, using Decimal so
    0.1 + 0.2 never becomes 30.000000000000004 cents."""
    if v in (None, ""):
        raise ValueError("is required")
    if isinstance(v, bool):
        raise ValueError("is not a valid amount")
    if isinstance(v, int):
        return _bounded(abs(v) * 100)
    s = str(_scalar(v)).strip().replace(",", "").replace("$", "").replace("€", "").replace("£", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError("is not a valid amount") from None
    if not d.is_finite():  # Decimal accepts "NaN" and "Infinity"
        raise ValueError("is not a valid amount")
    if neg:
        d = -d
    d = abs(d)  # bank exports often write debits as negatives
    if d >= MAX_CENTS:
        raise ValueError("is too large")
    return _bounded(int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _bounded(c: int) -> int:
    if c >= MAX_CENTS * 100:
        raise ValueError("is too large")
    return c


def optional_cents(v) -> int | None:
    return None if v in (None, "") else cents(v)


def number(v) -> float | int | None:
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        raise ValueError("is not a number")
    if isinstance(v, int):
        if abs(v) >= 2 ** 63:
            raise ValueError("is too large")
        return v
    if isinstance(v, float):
        f = v
    else:
        try:
            f = float(str(_scalar(v)).replace(",", ""))
        except ValueError:
            raise ValueError("is not a number") from None
    if not math.isfinite(f):  # NaN/inf would make the stored JSON invalid
        raise ValueError("must be a finite number")
    return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f


def boolean(v) -> int:
    if isinstance(v, bool):
        return int(v)
    if v in (None, ""):
        return 0
    s = str(_scalar(v)).strip().lower()
    if s in ("1", "true", "yes", "y", "on", "t", "x"):
        return 1
    if s in ("0", "false", "no", "n", "off", "f"):
        return 0
    raise ValueError("must be true or false")


def color(v) -> str:
    s = text(7)(v) or "#6b7280"
    if not COLOR_RE.match(s):
        raise ValueError("must be a #rrggbb colour")
    return s.lower()


def currency(v) -> str:
    s = (text(3)(v) or "USD").upper()
    if not re.fullmatch(r"[A-Z]{3}", s):
        raise ValueError("must be a 3-letter ISO currency code")
    return s


def fk(v) -> int | None:
    if v in (None, "", 0, "0"):
        return None
    if isinstance(v, bool):
        raise ValueError("must be an id")
    try:
        i = int(_scalar(v))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("must be an id") from None
    if not 1 <= i < 2 ** 63:
        raise ValueError("must be a positive id")
    return i


# ------------------------------------------------------------------ schema

class Field:
    def __init__(self, coerce: Callable[[Any], Any], required: bool = False):
        self.coerce = coerce
        self.required = required


class Schema:
    def __init__(self, **fields: Field):
        self.fields = fields

    def clean(self, data: dict, *, partial: bool = False) -> dict:
        """Validate `data`. With partial=True (PATCH), only supplied keys are
        checked and required-ness is only enforced for keys that are present."""
        out, errors = {}, {}
        for name, field in self.fields.items():
            if name not in data:
                if field.required and not partial:
                    errors[name] = "is required"
                continue
            try:
                val = field.coerce(data[name])
            except ValueError as e:
                errors[name] = str(e)
                continue
            if field.required and val in (None, ""):
                errors[name] = "is required"
                continue
            out[name] = val
        unknown = set(data) - set(self.fields) - {"id", "created_at", "updated_at", "tags"}
        if unknown:
            errors["_"] = f"unknown field(s): {', '.join(sorted(unknown))}"
        if errors:
            raise ValidationError(errors)
        return out


CONTACT = Schema(
    first_name=Field(text(100), required=True),
    last_name=Field(text(100)),
    company=Field(text(200)),
    job_title=Field(text(200)),
    email=Field(email),
    phone=Field(phone),
    address=Field(text(500)),
    city=Field(text(100)),
    region=Field(text(100)),
    postal_code=Field(text(20)),
    country=Field(text(100)),
    birthday=Field(iso_date),
    website=Field(url),
    notes=Field(text(10000)),
    favorite=Field(boolean),
    photo_id=Field(fk),
)

EXPENSE = Schema(
    spent_on=Field(iso_date, required=True),
    amount_cents=Field(cents, required=True),
    currency=Field(currency),
    merchant=Field(text(200)),
    category_id=Field(fk),
    payment_method=Field(text(50)),
    description=Field(text(2000)),
    contact_id=Field(fk),
    receipt_picture_id=Field(fk),
)

CATEGORY = Schema(
    name=Field(text(60), required=True),
    color=Field(color),
    monthly_budget_cents=Field(optional_cents),
)

PICTURE_META = Schema(
    title=Field(text(200)),
    description=Field(text(5000)),
    album=Field(text(100)),
    taken_at=Field(iso_date),
)

TAG = Schema(name=Field(text(40), required=True), color=Field(color))


def slugify(s: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return slug or "section"


def field_key(s: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    if not key or key[0].isdigit():
        key = "f_" + key
    return key
