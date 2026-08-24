"""Deterministic helpers: time, numbers, text. Standard library only.

Everything here must be pure and reproducible. In particular we never use the
builtin ``hash()`` (PYTHONHASHSEED-dependent) for anything that reaches output.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_INDEX = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}


# ---------------------------------------------------------------- time

def parse_iso(value) -> datetime | None:
    """Parse the ISO-8601 shapes the dataset actually uses. Never raises."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                parsed = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def now_utc_iso(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(UTC)
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def ist(moment: datetime) -> datetime:
    return moment.astimezone(IST)


def _moment(value):
    """Coerce whatever the dataset gave us into an aware datetime, or ``None``.

    Every time helper below goes through this. Payload dates arrive as bare
    strings (``"2026-10-31"``, ``"2026-11-05T18:00:00+05:30"``) as often as they
    arrive parsed, and a formatter that only accepted ``datetime`` would raise
    ``AttributeError`` deep inside a playbook instead of degrading to ``None``.
    """
    return value if isinstance(value, datetime) and value.tzinfo else parse_iso(value)


def day_delta(target, reference) -> int | None:
    """Whole days from ``reference`` to ``target`` (positive = future), IST calendar."""
    target, reference = _moment(target), _moment(reference)
    if target is None or reference is None:
        return None
    return (ist(target).date() - ist(reference).date()).days


def fmt_slot(moment) -> str | None:
    """'Wed 5 Nov, 6pm' — the shape Indian merchants and customers read fastest."""
    moment = _moment(moment)
    if moment is None:
        return None
    local = ist(moment)
    hour12 = local.hour % 12 or 12
    suffix = "am" if local.hour < 12 else "pm"
    minutes = f":{local.minute:02d}" if local.minute else ""
    return (f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}, "
            f"{hour12}{minutes}{suffix}")


def fmt_day(moment) -> str | None:
    """'Wed 5 Nov' — for date-only references."""
    moment = _moment(moment)
    if moment is None:
        return None
    local = ist(moment)
    return f"{_WEEKDAYS[local.weekday()]} {local.day} {_MONTHS[local.month - 1]}"


def fmt_date_plain(moment) -> str | None:
    """'5 Nov' / '15 Dec' — for deadlines where the weekday adds nothing."""
    moment = _moment(moment)
    if moment is None:
        return None
    local = ist(moment)
    return f"{local.day} {_MONTHS[local.month - 1]}"


def weekday_name(moment) -> str | None:
    moment = _moment(moment)
    if moment is None:
        return None
    return _WEEKDAYS[ist(moment).weekday()]


def is_weekend(moment) -> bool:
    moment = _moment(moment)
    return moment is not None and ist(moment).weekday() >= 5


def month_matches_range(month_range: str, moment) -> bool:
    """True when ``moment`` falls inside dataset month labels.

    Handles the four shapes present in seasonal_beats: 'Jan', 'Apr-Jun',
    'Nov-Feb' (wrapping), and 'Feb 14'.
    """
    moment = _moment(moment)
    if moment is None or not isinstance(month_range, str):
        return False
    local = ist(moment)
    tokens = re.findall(r"[A-Za-z]{3}", month_range)
    months = [_MONTH_INDEX[t.lower()] for t in tokens if t.lower() in _MONTH_INDEX]
    if not months:
        return False
    if len(months) == 1:
        return local.month == months[0]
    start, end = months[0], months[-1]
    if start <= end:
        return start <= local.month <= end
    return local.month >= start or local.month <= end  # wraps the year


# ---------------------------------------------------------------- numbers

def to_number(value):
    """Best-effort numeric coercion; returns None for anything non-numeric."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("₹", "").strip()
        if not re.fullmatch(r"[-+]?\d*\.?\d+%?", cleaned):
            return None
        try:
            if cleaned.endswith("%"):
                return float(cleaned[:-1]) / 100.0
            return float(cleaned)
        except ValueError:
            return None
    return None


def inr(amount) -> str | None:
    """Indian-grouped rupee string: 4999 -> '₹4,999', 149999 -> '₹1,49,999'."""
    number = to_number(amount)
    if number is None:
        return None
    negative = number < 0
    text = str(int(round(abs(number))))
    if len(text) > 3:
        head, tail = text[:-3], text[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        text = ",".join(groups + [tail])
    return ("-₹" if negative else "₹") + text


def grouped(amount) -> str | None:
    """Indian-grouped plain integer: 2410 -> '2,410'."""
    rupees = inr(amount)
    return None if rupees is None else rupees.lstrip("₹-")


def pct(fraction, digits: int = 1) -> str | None:
    """0.021 -> '2.1%'. Accepts a fraction; whole percents drop the decimal."""
    number = to_number(fraction)
    if number is None:
        return None
    value = number * 100.0
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}%"
    return f"{value:.{digits}f}%"


def pct_points(fraction, digits: int = 0) -> str | None:
    """Signed delta as percent, e.g. -0.5 -> '-50%', 0.15 -> '+15%'."""
    number = to_number(fraction)
    if number is None:
        return None
    value = number * 100.0
    sign = "+" if value > 0 else ""
    if abs(value - round(value)) < 0.5:
        return f"{sign}{int(round(value))}%"
    return f"{sign}{value:.{digits}f}%"


def plural(count, singular: str, many: str | None = None) -> str:
    number = to_number(count)
    if number is None:
        return singular
    return singular if abs(number - 1) < 1e-9 else (many or singular + "s")


# ---------------------------------------------------------------- text

_WS = re.compile(r"[ \t ]+")
_NEWLINES = re.compile(r"\n{3,}")


def squash(text: str) -> str:
    """Collapse runs of spaces without destroying deliberate line breaks."""
    if not text:
        return ""
    lines = [_WS.sub(" ", line).strip() for line in str(text).split("\n")]
    return _NEWLINES.sub("\n\n", "\n".join(lines)).strip()


def join_sentences(parts) -> str:
    """Join non-empty fragments into prose, keeping explicit blank-line breaks."""
    out = ""
    for raw in parts:
        if raw is None:
            continue
        part = squash(raw)
        if not part:
            continue
        wants_break = str(raw).startswith("\n") or out.endswith("\n")
        if not out:
            out = part
        elif wants_break:
            out = out.rstrip("\n") + "\n\n" + part.lstrip("\n")
        else:
            out += " " + part
    return squash(out)


def normalise(text: str) -> str:
    """Lowercase, punctuation-stripped form used for matching inbound replies."""
    if not text:
        return ""
    return re.sub(r"[^a-z0-9 ]+", " ", str(text).lower()).strip()


def normalise_tight(text: str) -> str:
    """Whitespace-free normal form: catches auto-replies that vary only in spacing."""
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def humanise_slug(slug) -> str:
    """'delivery_late' -> 'delivery late'; 'ctr_below_peer_median' -> 'ctr below peer median'."""
    return re.sub(r"[_\-:]+", " ", str(slug or "")).strip()


def sentence_case(text: str) -> str:
    text = str(text or "").strip()
    return text[:1].upper() + text[1:] if text else text


def first_clause(text, max_words: int = 18) -> str:
    """Trim a dataset summary to its leading claim, kept verbatim (no paraphrase)."""
    text = squash(text or "")
    if not text:
        return ""
    for stop in (". ", "; ", " — ", " – "):
        index = text.find(stop)
        if 0 < index < 200:
            text = text[:index]
            break
    words = text.split()
    if len(words) > max_words:
        text = " ".join(words[:max_words])
    return text.rstrip(" .,;:—–")


def stable_id(*parts, length: int = 8) -> str:
    """Deterministic short id (never the salted builtin hash)."""
    seed = "|".join(str(p) for p in parts)
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:length]


def slug_token(text, max_len: int = 22) -> str:
    """Readable ascii token for conversation ids: 'Dr. Meera' -> 'dr_meera'."""
    token = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return token[:max_len].strip("_") or "x"


def iso_week(moment) -> str:
    moment = _moment(moment)
    if moment is None:
        return "na"
    year, week, _ = ist(moment).isocalendar()
    return f"{year}-W{week:02d}"


def month_key(moment) -> str:
    moment = _moment(moment)
    if moment is None:
        return "na"
    local = ist(moment)
    return f"{local.year}-{local.month:02d}"


def dedupe(items):
    """Order-preserving dedupe for hashable items."""
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def get_path(obj, *path, default=None):
    """Safe nested lookup: get_path(merchant, 'identity', 'city')."""
    current = obj
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, (list, tuple)) and isinstance(key, int) and -len(current) <= key < len(current):
            current = current[key]
        else:
            return default
        if current is None:
            return default
    return current
