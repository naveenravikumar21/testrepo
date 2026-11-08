"""Fare watches: what to monitor, read from GitHub issues created with the "Fare watch" form."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
SHORT_DAYS = [d[:3] for d in WEEKDAYS]

# Frequency choice -> default weekdays (0 = Monday). Explicitly ticked days override these.
FREQUENCIES = {
    "daily": ("Every day", [0, 1, 2, 3, 4, 5, 6]),
    "3x": ("3 times a week", [0, 2, 4]),
    "2x": ("2 times a week", [0, 3]),
    "1x": ("Once a week", [0]),
}

# Convenience: common Indian city names -> airport codes (IATA codes always work too).
CITY_CODES = {
    "jaipur": "JAI", "kochi": "COK", "cochin": "COK", "delhi": "DEL", "new delhi": "DEL", "mumbai": "BOM",
    "bombay": "BOM", "bengaluru": "BLR", "bangalore": "BLR", "chennai": "MAA", "madras": "MAA",
    "hyderabad": "HYD", "kolkata": "CCU", "calcutta": "CCU", "pune": "PNQ", "ahmedabad": "AMD", "goa": "GOI",
    "trivandrum": "TRV", "thiruvananthapuram": "TRV", "kozhikode": "CCJ", "calicut": "CCJ", "kannur": "CNN",
    "lucknow": "LKO", "udaipur": "UDR", "jodhpur": "JDH", "indore": "IDR", "coimbatore": "CJB",
}

NO_RESPONSE = {"", "_no response_", "none", "n/a", "-"}
MAX_STOPS_CHOICES = {"non-stop": 0, "nonstop": 0, "direct": 0, "up to 1": 1, "1 stop": 1, "any": 2, "up to 2": 2}


@dataclass
class Watch:
    id: str                          # issue number as text, or "local"
    title: str
    origin: str
    destination: str
    dates: list[date]
    days: list[int]                  # weekdays to check (0 = Monday)
    frequency: str                   # human label
    target: float | None             # book-now price in INR
    max_stops: int
    url: str = ""
    issue_number: int | None = None
    body_hash: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def route(self) -> str:
        return f"{self.origin} → {self.destination}"

    @property
    def days_label(self) -> str:
        if len(self.days) == 7:
            return "every day"
        return ", ".join(SHORT_DAYS[d] for d in sorted(self.days))

    def active_dates(self, today: date) -> list[date]:
        return [d for d in self.dates if d >= today]

    def is_due(self, today: date) -> bool:
        return today.weekday() in self.days and bool(self.active_dates(today))

    def estimate_searches_30d(self, today: date) -> int:
        """Searches this watch will use in the next 30 days (1 per date per check, plus ~1 confirmation
        per date per week as a generous allowance for seller-price checks)."""
        total = 0
        for i in range(30):
            day = today + timedelta(days=i)
            if day.weekday() in self.days:
                total += sum(1 for d in self.dates if d >= day)
        confirmations = sum(min(30, max(0, (d - today).days)) for d in self.dates if d >= today) // 7
        return total + confirmations

    def summary_lines(self) -> list[str]:
        return [
            f"Route: {self.route}",
            "Travel dates: " + ", ".join(d.strftime("%a %d %b %Y") for d in self.dates),
            f"Checks: {self.days_label if len(self.days) == 7 else f'{self.frequency} ({self.days_label})'} "
            "at about 7 AM IST",
            f"Book-now price: {'₹{:,.0f}'.format(self.target) if self.target else 'not set'}",
            f"Stops: {['non-stop only', 'up to 1 stop', 'up to 2 stops'][self.max_stops]}",
        ]


# ---- parsing -------------------------------------------------------------------------------------
def _sections(body: str) -> dict[str, str]:
    """Split an issue-form body ('### Label' headings) into {normalised label: value}."""
    out: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in (body or "").splitlines():
        m = re.match(r"^#{2,4}\s+(.*)$", line.strip())
        if m:
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current, buf = m.group(1).strip().lower(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def _field(sections: dict[str, str], *keywords: str) -> str:
    for label, value in sections.items():
        if any(k in label for k in keywords):
            return "" if value.strip().lower() in NO_RESPONSE else value.strip()
    return ""


def parse_airport(value: str, default: str) -> tuple[str, str | None]:
    v = value.strip()
    if not v:
        return default, None
    if v.lower() in CITY_CODES:
        return CITY_CODES[v.lower()], None
    m = re.search(r"\(([A-Za-z]{3})\)", v)  # "Jaipur (JAI)"
    if m:
        return m.group(1).upper(), None
    if re.fullmatch(r"[A-Za-z]{3}", v):
        return v.upper(), None
    for name, c in CITY_CODES.items():
        if name in v.lower():
            return c, None
    return default, f"Couldn't understand the airport '{v}'. Use a 3-letter code like JAI, COK, DEL, BOM."


_DATE_FORMATS = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y"]
_DATE_FORMATS_NO_YEAR = ["%d %b", "%d %B", "%b %d", "%B %d", "%d/%m"]


def parse_one_date(text: str, today: date) -> date:
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text.strip(), flags=re.I).replace(",", " ")
    t = re.sub(r"\s+", " ", t).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS_NO_YEAR:
        try:
            d = datetime.strptime(f"{t} {today.year}", f"{fmt} %Y").date()
            return d if d >= today else d.replace(year=today.year + 1)
        except ValueError:
            pass
    raise ValueError(f"Couldn't read the date '{text.strip()}'. Use a format like 2027-01-04 or 4 Jan 2027.")


def parse_dates(text: str, today: date) -> tuple[list[date], list[str]]:
    dates, errors = set(), []
    for chunk in re.split(r"[,;\n]|\band\b", text or ""):
        chunk = chunk.strip().strip("-•* ")
        if not chunk:
            continue
        parts = re.split(r"\s+(?:to|till|until|through)\s+|\s*(?:–|—|\.\.)\s*", chunk, maxsplit=1)
        try:
            if len(parts) == 2:
                start, end = parse_one_date(parts[0], today), parse_one_date(parts[1], today)
                if end < start:
                    start, end = end, start
                if (end - start).days > 60:
                    errors.append(f"The range '{chunk}' is longer than 60 days.")
                    continue
                dates.update(start + timedelta(days=i) for i in range((end - start).days + 1))
            else:
                dates.add(parse_one_date(chunk, today))
        except ValueError as exc:
            errors.append(str(exc))
    return sorted(dates), errors


def parse_frequency(value: str) -> tuple[str, list[int]]:
    v = value.lower()
    if "every" in v or "daily" in v:
        key = "daily"
    elif "3" in v or "three" in v or "thrice" in v:
        key = "3x"
    elif "2" in v or "two" in v or "twice" in v:
        key = "2x"
    elif "once" in v or "1" in v or "one" in v or "week" in v:
        key = "1x"
    else:
        key = "daily"
    return FREQUENCIES[key]


def parse_days(value: str) -> list[int]:
    days = []
    for line in (value or "").splitlines():
        m = re.match(r"^\s*-\s*\[[xX]\]\s*(\w+)", line)
        if m:
            name = m.group(1)[:3].title()
            if name in SHORT_DAYS:
                days.append(SHORT_DAYS.index(name))
    return sorted(set(days))


def parse_price(value: str) -> float | None:
    m = re.search(r"\d[\d,]*(?:\.\d+)?", value or "")
    if not m:
        return None
    price = float(m.group(0).replace(",", ""))
    return price if price > 0 else None


def parse_stops(value: str, default: int) -> int:
    v = (value or "").lower()
    for key, n in MAX_STOPS_CHOICES.items():
        if key in v:
            return n
    return default


def watch_from_issue(issue: dict, config, today: date) -> Watch:
    body = issue.get("body") or ""
    s = _sections(body)
    errors = []
    origin, err = parse_airport(_field(s, "from"), config.defaults.origin)
    if err:
        errors.append(err)
    destination, err = parse_airport(_field(s, "to", "destination"), config.defaults.destination)
    if err:
        errors.append(err)
    if origin == destination:
        errors.append("From and To are the same airport.")
    dates, date_errors = parse_dates(_field(s, "travel date", "dates", "date"), today)
    errors += date_errors
    if not dates and not date_errors:
        errors.append("No travel dates found. Example: 2027-01-04, 2027-01-05 or 2 Jan 2027 to 6 Jan 2027.")
    if len(dates) > config.budget.max_dates_per_watch:
        errors.append(f"{len(dates)} dates is too many for one watch (max {config.budget.max_dates_per_watch}); "
                      "it would use up the free monthly searches quickly.")
    frequency, days = parse_frequency(_field(s, "how often", "frequency"))
    ticked = parse_days(_field(s, "check on", "days"))
    if ticked:
        days = ticked
        frequency = f"{len(ticked)} time{'s' if len(ticked) > 1 else ''} a week" if len(ticked) < 7 else "Every day"
    target = parse_price(_field(s, "book-now", "price", "target"))
    if target is None and config.defaults.target_fare_inr:
        target = config.defaults.target_fare_inr
    return Watch(
        id=str(issue["number"]),
        title=issue.get("title", ""),
        origin=origin,
        destination=destination,
        dates=dates,
        days=days,
        frequency=frequency,
        target=target,
        max_stops=min(parse_stops(_field(s, "stops"), config.defaults.max_stops), config.defaults.max_stops),
        url=issue.get("html_url", ""),
        issue_number=int(issue["number"]),
        body_hash=hashlib.sha256(body.encode()).hexdigest()[:16],
        errors=errors,
    )


def local_watch(config) -> Watch | None:
    if not config.local_dates:
        return None
    return Watch(id="local", title="Local watch", origin=config.defaults.origin,
                 destination=config.defaults.destination, dates=list(config.local_dates),
                 days=list(range(7)), frequency="Every day", target=config.local_target or None,
                 max_stops=config.defaults.max_stops)


def is_watch_issue(issue: dict, config) -> bool:
    if "pull_request" in issue:
        return False
    title_ok = (issue.get("title") or "").strip().lower().startswith(config.github.watch_title_prefix.lower())
    label_ok = any((l.get("name") if isinstance(l, dict) else l) == config.github.watch_label
                   for l in issue.get("labels") or [])
    return title_ok or label_ok


def is_trusted_author(issue: dict, config) -> bool:
    assoc = (issue.get("author_association") or "").upper()
    if assoc in config.github.allowed_author_associations:
        return True
    owner = (config.github.repository or "").split("/")[0].lower()
    return ((issue.get("user") or {}).get("login") or "").lower() == owner


def watch_from_row(row) -> Watch:
    """Rebuild a Watch from the database (for reports and estimates)."""
    import json
    return Watch(id=row["id"], title=row["title"] or "", origin=row["origin"], destination=row["destination"],
                 dates=[date.fromisoformat(x) for x in json.loads(row["dates_json"] or "[]")],
                 days=json.loads(row["days_json"] or "[]"), frequency=row["frequency"] or "",
                 target=row["target"], max_stops=row["max_stops"] if row["max_stops"] is not None else 2,
                 url=row["url"] or "", issue_number=int(row["id"]) if str(row["id"]).isdigit() else None,
                 body_hash=row["body_hash"] or "")
