#!/usr/bin/env python3
"""
Productive.io Time Sync
========================
End-of-month tool that fills out your Productive timesheets automatically.

1. Fetches your org's holidays from the Productive API
2. Interactively asks if you had any days off — fetches available absence
   types (sick, vacation, PTO, etc.) from the API and lets you pick
3. You can enter multiple periods for each type, and multiple types
4. Fills remaining working days with time entries from your pre-assigned
   project bookings (up to 8h/day), skipping weekends and holidays

Usage:
    python sync_time_entries.py --whoami              # find your person ID
    python sync_time_entries.py --list-events         # list absence event types
    python sync_time_entries.py --list-calendars      # list holiday calendars
    python sync_time_entries.py --dry-run             # preview (default: previous month)
    python sync_time_entries.py --month 2025-06       # run for a specific month
    python sync_time_entries.py --no-prompts          # skip absence prompts
"""

import argparse
import json
import os
import sys
import time as time_mod
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Optional

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

API_BASE = "https://api.productive.io/api/v2"
API_TOKEN = os.getenv("PRODUCTIVE_API_TOKEN", "")
ORG_ID = os.getenv("PRODUCTIVE_ORG_ID", "")
PERSON_ID = os.getenv("PRODUCTIVE_PERSON_ID", "")
SUBSIDIARY_NAME = os.getenv("PRODUCTIVE_SUBSIDIARY_NAME", "PDUS")
COUNTRY_CODE = os.getenv("PRODUCTIVE_COUNTRY_CODE", "US")

HEADERS = {
    "Content-Type": "application/vnd.api+json",
    "X-Auth-Token": API_TOKEN,
    "X-Organization-Id": ORG_ID,
}

WORK_DAY_MINUTES = 480  # 8 hours
BATCH_SIZE = 10  # max concurrent API writes per batch
BATCH_DELAY = 0.25  # seconds to pause between batches (rate-limit safety)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _check_creds():
    missing = []
    if not API_TOKEN:
        missing.append("PRODUCTIVE_API_TOKEN")
    if not ORG_ID:
        missing.append("PRODUCTIVE_ORG_ID")
    if missing:
        sys.exit(
            f"Missing env vars: {', '.join(missing)}\n"
            "Copy .env.example → .env and fill in your values."
        )


def api_get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{API_BASE}/{path}"
    resp = requests.get(url, headers=HEADERS, params=params or {})
    resp.raise_for_status()
    return resp.json()


def api_get_all(path: str, params: Optional[dict] = None) -> list:
    """Paginated GET — returns every record."""
    params = dict(params or {})
    params.setdefault("page[size]", 200)
    params.setdefault("page[number]", 1)
    all_data = []
    while True:
        body = api_get(path, params)
        all_data.extend(body.get("data", []))
        meta = body.get("meta", {})
        if meta.get("current_page", 1) >= meta.get("total_pages", 1):
            break
        params["page[number]"] = meta["current_page"] + 1
    return all_data


def api_post(path: str, payload: dict) -> dict:
    url = f"{API_BASE}/{path}"
    resp = requests.post(url, headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def weekdays_in_range(start: date, end: date) -> list[date]:
    """All Mon–Fri dates in [start, end] inclusive."""
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def parse_date(value: str) -> Optional[date]:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def parse_date_input(
    raw: str, month_start: date, month_end: date
) -> tuple[list[tuple[date, date]], list[str]]:
    """Parse flexible date input into (start, end) pairs.

    Accepts comma-separated tokens, each being:
      - A bare day number:   ``3``          → single day
      - A day-number range:  ``3-5``        → range
      - A full ISO date:     ``2026-04-03`` → single day

    Returns (pairs, errors) where *pairs* is a list of (start, end) tuples
    and *errors* is a list of human-readable problems for bad tokens.
    """
    pairs: list[tuple[date, date]] = []
    errors: list[str] = []
    year, month = month_start.year, month_start.month

    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue

        # Try full ISO date first (YYYY-MM-DD has ≥2 hyphens)
        if token.count("-") >= 2:
            d = parse_date(token)
            if d and month_start <= d <= month_end:
                pairs.append((d, d))
            else:
                errors.append(f"'{token}' is not a valid date in this month")
            continue

        # Day range: "3-5"
        if "-" in token:
            parts = token.split("-", 1)
            try:
                d1 = date(year, month, int(parts[0].strip()))
                d2 = date(year, month, int(parts[1].strip()))
            except (ValueError, OverflowError):
                errors.append(f"'{token}' is not a valid day range")
                continue
            if d1 > d2:
                errors.append(f"'{token}' start is after end")
            elif d1 < month_start or d2 > month_end:
                errors.append(f"'{token}' is outside {month_start} – {month_end}")
            else:
                pairs.append((d1, d2))
            continue

        # Single day number: "3"
        try:
            d = date(year, month, int(token))
        except (ValueError, OverflowError):
            errors.append(f"'{token}' is not a valid day")
            continue
        if month_start <= d <= month_end:
            pairs.append((d, d))
        else:
            errors.append(f"'{token}' is outside {month_start} – {month_end}")

    return pairs, errors


def fmt_min(mins: int) -> str:
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}m" if m else f"{h}h"


# ---------------------------------------------------------------------------
# Holidays
# ---------------------------------------------------------------------------


def resolve_subsidiary_id(name: str) -> Optional[str]:
    """Look up a subsidiary by name (e.g. 'PDUS') and return its ID."""
    raw = api_get_all("subsidiaries", {"filter[status]": 1, "page[size]": 200})
    for sub in raw:
        if sub["attributes"].get("name", "").strip().lower() == name.strip().lower():
            return sub["id"]
    # Fallback: partial / case-insensitive match
    for sub in raw:
        if name.strip().lower() in sub["attributes"].get("name", "").strip().lower():
            return sub["id"]
    return None


def fetch_holiday_calendars() -> list[dict]:
    """Fetch all holiday calendars from the org.

    Returns a list of dicts:
        { "id": str, "name": str, "country": str, "state": str|None }
    """
    raw = api_get_all("holiday_calendars", {"page[size]": 200})
    calendars = []
    for cal in raw:
        a = cal["attributes"]
        calendars.append(
            {
                "id": cal["id"],
                "name": a.get("name", ""),
                "country": a.get("country", ""),
                "state": a.get("state"),
            }
        )
    return calendars


# Map short country codes to the set of names Productive might use for that
# country.  Keys are uppercase ISO-style codes; values are lowercase aliases.
_COUNTRY_ALIASES: dict[str, set[str]] = {
    "US": {"us", "usa", "united states", "united states of america"},
    "HR": {"hr", "croatia", "hrvatska"},
    "GB": {"gb", "uk", "united kingdom", "great britain"},
    "DE": {"de", "germany", "deutschland"},
    "CA": {"ca", "canada"},
    "AU": {"au", "australia"},
    "FR": {"fr", "france"},
    "NL": {"nl", "netherlands", "the netherlands"},
    "AT": {"at", "austria"},
    "ES": {"es", "spain"},
    "IT": {"it", "italy"},
    "PT": {"pt", "portugal"},
    "SE": {"se", "sweden"},
    "PL": {"pl", "poland"},
    "IE": {"ie", "ireland"},
}


def _aliases_for_code(code: str) -> set[str]:
    """Return the set of lowercase country name aliases for *code*."""
    code = code.strip().upper()
    if code in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[code]
    # For unknown codes, match against the code itself (e.g. "BR" matches "BR")
    return {code.lower()}


def resolve_holiday_calendar_id(country_code: str) -> Optional[str]:
    """Determine the holiday calendar ID for the given country.

    Resolution order:
    1. Exact match — calendar country matches a known alias for the code
    2. Fuzzy fallback — calendar name or country contains one of the aliases
    """
    aliases = _aliases_for_code(country_code)

    calendars = fetch_holiday_calendars()

    # Pass 1: exact country match
    for cal in calendars:
        if cal["country"].strip().lower() in aliases:
            return cal["id"]

    # Pass 2: any alias appears as a substring in name or country
    for cal in calendars:
        combined = f" {cal['name']} {cal['country']} ".lower()
        if any(f" {a} " in combined or combined.strip().startswith(a) for a in aliases):
            return cal["id"]

    return None


def fetch_holidays(
    month_start: date,
    month_end: date,
    holiday_calendar_id: Optional[str] = None,
) -> dict[date, str]:
    """Return {date: name} for holidays in the month.

    When *holiday_calendar_id* is provided the results are scoped to that
    calendar (i.e. the correct office). Otherwise all org holidays are returned.
    """
    params = {
        "filter[after]": (month_start - timedelta(days=1)).isoformat(),
        "filter[before]": (month_end + timedelta(days=1)).isoformat(),
        "page[size]": 200,
    }
    if holiday_calendar_id:
        params["filter[holiday_calendar_id]"] = holiday_calendar_id
    raw = api_get_all("holidays", params)
    holidays: dict[date, str] = {}
    for h in raw:
        d = date.fromisoformat(h["attributes"]["date"])
        if month_start <= d <= month_end:
            holidays[d] = h["attributes"]["name"]
    return holidays


# ---------------------------------------------------------------------------
# Events (absence types) — fetched live from the API
# ---------------------------------------------------------------------------

# Matches event names like "PDUS …", "PDHR …", "PDES …", "PDDE …", "PDMX …"
_OFFICE_PREFIX_LEN = 4


def _has_office_prefix(name: str) -> bool:
    """Return True if *name* starts with a PD** office prefix (e.g. 'PDUS ', 'PDHR ')."""
    return (
        len(name) > _OFFICE_PREFIX_LEN
        and name[:2] == "PD"
        and name[:_OFFICE_PREFIX_LEN].isalpha()
        and name[_OFFICE_PREFIX_LEN] == " "
    )


def filter_events_by_subsidiary(events: list[dict], subsidiary_name: str) -> list[dict]:
    """Keep only events that belong to *subsidiary_name* or are org-wide.

    Rules:
      - Events whose name starts with the subsidiary prefix (e.g. "PDUS ")
        are kept.
      - Events with a *different* office prefix (e.g. "PDHR ", "PDES ") are
        dropped.
      - Events with no office prefix at all (e.g. "Holiday", "Unpaid Leave")
        are considered org-wide and kept.
    """
    prefix = subsidiary_name.strip().upper()
    kept: list[dict] = []
    for ev in events:
        name = ev["name"]
        if _has_office_prefix(name):
            if name[: len(prefix)].upper() == prefix:
                kept.append(ev)
            # else: belongs to another office → skip
        else:
            kept.append(ev)  # org-wide
    return kept


def fetch_events() -> list[dict]:
    """Fetch all active absence events from the org.

    Returns a list of dicts:
        { "id": str, "name": str, "absence_type": str, "paid": str, "limit": str }
    """
    raw = api_get_all("events", {"page[size]": 200})
    etype_map = {1: "Paid", 2: "Unpaid"}
    limit_map = {2: "Limited (days)", 3: "Limited (hours)", 4: "Unlimited"}

    events = []
    for ev in raw:
        a = ev["attributes"]
        if a.get("archived_at"):
            continue
        events.append(
            {
                "id": ev["id"],
                "name": a.get("name", "—"),
                "absence_type": a.get("absence_type", "—"),
                "paid": etype_map.get(a.get("event_type_id"), "—"),
                "limit": limit_map.get(a.get("limitation_type_id"), "—"),
            }
        )
    return events


def display_event_menu(events: list[dict]) -> None:
    """Print a numbered menu of absence event types."""
    print("\n   Available absence types:")
    for idx, ev in enumerate(events, 1):
        print(
            f"     [{idx}] {ev['name']:<25} "
            f"{ev['absence_type']:<12} {ev['paid']:<8} {ev['limit']}"
        )


def pick_event(events: list[dict]) -> Optional[dict]:
    """Let the user pick an event from the numbered menu. Returns the event dict or None."""
    while True:
        raw = input(f"\n   Select type [1-{len(events)}]: ").strip()
        try:
            choice = int(raw)
            if 1 <= choice <= len(events):
                return events[choice - 1]
        except ValueError:
            pass
        print(f"   ⚠️  Enter a number between 1 and {len(events)}.")


# ---------------------------------------------------------------------------
# Absence bookings — interactive prompt
# ---------------------------------------------------------------------------


def prompt_date_ranges(
    event_name: str, month_start: date, month_end: date
) -> list[dict]:
    """
    Ask the user for one or more date ranges for a single absence type.

    Returns list of { start, end, note, work_days }.
    Each item becomes one absence booking.
    """
    ranges: list[dict] = []
    last_day = month_end.day

    while True:
        print(f"\n   Enter {event_name} dates ({month_start} to {month_end}):")
        print(f"   Examples: 3  |  3-5  |  3, 7, 14-16  |  2026-04-03")

        while True:
            raw = input("   Dates: ").strip()
            if not raw:
                print("   ⚠️  Please enter at least one date.")
                continue
            pairs, errors = parse_date_input(raw, month_start, month_end)
            for err in errors:
                print(f"   ⚠️  {err}")
            if pairs:
                break
            print(
                f"   ⚠️  No valid dates. Enter day numbers (1-{last_day}), "
                f"ranges (3-5), or full dates (YYYY-MM-DD)."
            )

        note = input("   Comment: ").strip()

        for start, end in pairs:
            work_days = weekdays_in_range(start, end)
            if not work_days:
                rng = start.isoformat() if start == end else f"{start} → {end}"
                print(f"   ⚠️  {rng}: no weekdays, skipping")
                continue
            ranges.append(
                {"start": start, "end": end, "note": note, "work_days": work_days}
            )
            rng = start.isoformat() if start == end else f"{start} → {end}"
            print(
                f"   ✅ {rng}: {len(work_days)} day(s) — "
                f"{', '.join(d.isoformat() for d in work_days)}"
            )

        if input(f"\n   Add more {event_name} dates? [y/N] ").strip().lower() not in (
            "y",
            "yes",
        ):
            break

    return ranges


def prompt_all_absences(
    month_start: date, month_end: date, existing_absences: list[dict]
) -> list[dict]:
    """
    Top-level interactive absence prompt.

    Shows already-booked absences first, then lets the user add more.
    Fetches events from the API, displays them as a menu, lets the user
    pick a type, enter date ranges, then asks if they have other types
    of days off too.

    Returns a flat list of NEW absence dicts to create:
        { event_id, event_name, start, end, note, work_days }
    """
    display_existing_absences(existing_absences)

    answer = (
        input("\n🗓️  Any additional days off to book this month? [y/N] ").strip().lower()
    )
    if answer not in ("y", "yes"):
        return []

    print("\n⏳ Fetching absence types from Productive…")
    events = filter_events_by_subsidiary(fetch_events(), SUBSIDIARY_NAME)
    if not events:
        print(
            f"   No absence event types found for {SUBSIDIARY_NAME}.\n"
            f"   (Run --list-events to see all org events.)"
        )
        return []

    all_absences: list[dict] = []

    while True:
        display_event_menu(events)
        chosen = pick_event(events)
        if not chosen:
            break

        ranges = prompt_date_ranges(chosen["name"], month_start, month_end)
        for r in ranges:
            all_absences.append(
                {
                    "event_id": chosen["id"],
                    "event_name": chosen["name"],
                    "start": r["start"],
                    "end": r["end"],
                    "note": r["note"],
                    "work_days": r["work_days"],
                }
            )

        if input(
            "\n   Any other type of days off this month? [y/N] "
        ).strip().lower() not in ("y", "yes"):
            break

    return all_absences


def fetch_existing_absences(
    month_start: date, month_end: date, person_id: str
) -> list[dict]:
    """Fetch absence bookings already created for this person/month.

    Returns a list of dicts:
        { event_id, event_name, start, end, work_days, note, booking_id }
    """
    params = {
        "filter[person_id]": person_id,
        "filter[after]": (month_start - timedelta(days=1)).isoformat(),
        "filter[before]": (month_end + timedelta(days=1)).isoformat(),
        "filter[booking_type]": "event",
        "include": "event",
        "page[size]": 200,
    }
    raw = api_get_all("bookings", params)

    # Collect distinct event IDs so we can resolve their names.
    # api_get_all only collects "data", so we fetch event names individually.
    event_ids_seen: set[str] = set()
    for bk in raw:
        ev = bk.get("relationships", {}).get("event", {}).get("data")
        if ev:
            event_ids_seen.add(ev["id"])

    # Resolve event names (small set, usually ≤5 distinct events)
    event_names: dict[str, str] = {}
    for eid in event_ids_seen:
        try:
            body = api_get(f"events/{eid}")
            event_names[eid] = body["data"]["attributes"].get("name", f"Event {eid}")
        except Exception:
            event_names[eid] = f"Event {eid}"

    results: list[dict] = []
    for bk in raw:
        attrs = bk["attributes"]
        # Skip canceled bookings
        if attrs.get("canceled"):
            continue
        ev = bk.get("relationships", {}).get("event", {}).get("data")
        if not ev:
            continue
        bk_start = date.fromisoformat(attrs["started_on"])
        bk_end = date.fromisoformat(attrs["ended_on"])
        eff_start = max(bk_start, month_start)
        eff_end = min(bk_end, month_end)
        work_days = weekdays_in_range(eff_start, eff_end)
        if not work_days:
            continue
        results.append(
            {
                "event_id": ev["id"],
                "event_name": event_names.get(ev["id"], f"Event {ev['id']}"),
                "start": eff_start,
                "end": eff_end,
                "work_days": work_days,
                "note": attrs.get("note") or "",
                "booking_id": bk["id"],
            }
        )
    return results


def display_existing_absences(existing: list[dict]) -> None:
    """Print a summary of already-booked absences."""
    if not existing:
        return

    total_days = sum(len(a["work_days"]) for a in existing)
    print(f"\n   📋 Already booked this month ({total_days} day(s)):")

    by_event: dict[str, list[dict]] = {}
    for a in existing:
        by_event.setdefault(a["event_name"], []).append(a)

    for event_name, entries in by_event.items():
        days = sum(len(e["work_days"]) for e in entries)
        print(f"      {event_name}: {days} day(s)")
        for a in entries:
            rng = (
                a["start"].isoformat()
                if a["start"] == a["end"]
                else f"{a['start']} → {a['end']}"
            )
            day_list = ", ".join(d.isoformat() for d in a["work_days"])
            note = f"  ({a['note']})" if a["note"] else ""
            print(f"        {rng}: {day_list}{note}")


def create_absence_booking(
    event_id: str,
    person_id: str,
    start: date,
    end: date,
    time_per_day: int,
    note: str = "",
) -> dict:
    """POST an absence booking linked to an Event."""
    payload = {
        "data": {
            "type": "bookings",
            "attributes": {
                "started_on": start.isoformat(),
                "ended_on": end.isoformat(),
                "time": time_per_day,
                "booking_method_id": 1,
            },
            "relationships": {
                "event": {"data": {"type": "events", "id": str(event_id)}},
                "person": {"data": {"type": "people", "id": str(person_id)}},
                "origin": {"data": {"type": "bookings", "id": ""}},
            },
        }
    }
    if note:
        payload["data"]["attributes"]["note"] = note
    return api_post("bookings", payload)


# ---------------------------------------------------------------------------
# Time entries (from budget bookings)
# ---------------------------------------------------------------------------


def fetch_budget_bookings(
    month_start: date,
    month_end: date,
    person_id: str,
    subsidiary_id: Optional[str] = None,
) -> list:
    params = {
        "filter[person_id]": person_id,
        "filter[after]": (month_start - timedelta(days=1)).isoformat(),
        "filter[before]": (month_end + timedelta(days=1)).isoformat(),
        "filter[booking_type]": "service",
        "include": "service,task",
        "page[size]": 200,
    }
    if subsidiary_id:
        params["filter[person_subsidiary_id]"] = subsidiary_id
    return api_get_all("bookings", params)


def compute_daily_minutes(booking: dict, num_working_days: int) -> int:
    attrs = booking["attributes"]
    method = attrs.get("booking_method_id", 1)
    if method == 1:
        return attrs.get("time", 0) or 0
    elif method == 2:
        pct = attrs.get("percentage", 100) or 100
        return round(WORK_DAY_MINUTES * pct / 100)
    elif method == 3:
        total = attrs.get("total_time", 0) or 0
        return round(total / num_working_days) if num_working_days else 0
    return attrs.get("time", 0) or 0


def build_time_entries(
    bookings: list,
    month_start: date,
    month_end: date,
    skip_dates: set[date],
) -> list[dict]:
    """Expand budget bookings into per-day time entry dicts, skipping non-work dates.

    Enforces an 8h (WORK_DAY_MINUTES) cap per day — if adding an entry would
    push a day over the limit, that entry is silently dropped.
    """
    entries = []
    day_totals: dict[date, int] = {}  # running total of minutes per day
    skipped_reasons: dict[str, int] = {}
    capped_days: set[date] = set()

    for bk in bookings:
        attrs = bk["attributes"]
        rels = bk["relationships"]

        bk_start = date.fromisoformat(attrs["started_on"])
        bk_end = date.fromisoformat(attrs["ended_on"])
        eff_start = max(bk_start, month_start)
        eff_end = min(bk_end, month_end)

        eligible_days = [
            d for d in weekdays_in_range(eff_start, eff_end) if d not in skip_dates
        ]
        if not eligible_days:
            skipped_reasons["no eligible days"] = (
                skipped_reasons.get("no eligible days", 0) + 1
            )
            continue

        full_range_days = weekdays_in_range(bk_start, bk_end)
        daily_min = compute_daily_minutes(bk, len(full_range_days))
        if daily_min <= 0:
            skipped_reasons["zero minutes"] = skipped_reasons.get("zero minutes", 0) + 1
            continue

        svc_data = rels.get("service", {}).get("data")
        if not svc_data:
            note = attrs.get("note") or ""
            label = note[:40] if note else f"booking {bk['id']}"
            skipped_reasons[f"no service link ({label})"] = (
                skipped_reasons.get(f"no service link ({label})", 0) + 1
            )
            continue
        task_data = rels.get("task", {}).get("data")

        for day in eligible_days:
            current = day_totals.get(day, 0)
            if current + daily_min > WORK_DAY_MINUTES:
                capped_days.add(day)
                continue
            day_totals[day] = current + daily_min
            entries.append(
                {
                    "date": day,
                    "service_id": svc_data["id"],
                    "task_id": task_data["id"] if task_data else None,
                    "person_id": PERSON_ID,
                    "time": daily_min,
                    "note": attrs.get("note") or "",
                }
            )

    if skipped_reasons:
        print(f"   ⚠️  Skipped {sum(skipped_reasons.values())} booking(s):")
        for reason, count in skipped_reasons.items():
            print(f"      {count}× {reason}")

    if capped_days:
        print(
            f"   ⚠️  {len(capped_days)} day(s) capped at "
            f"{fmt_min(WORK_DAY_MINUTES)} — extra entries dropped:"
        )
        for d in sorted(capped_days):
            print(
                f"      {d.isoformat()} {d.strftime('%a')}: {fmt_min(day_totals.get(d, 0))}"
            )

    return entries


def deduplicate(planned: list, existing: list) -> tuple[list, int]:
    """Remove planned entries whose (date, service_id) already exists."""
    existing_keys = set()
    for te in existing:
        a = te["attributes"]
        svc = te["relationships"].get("service", {}).get("data")
        existing_keys.add((a["date"], svc["id"] if svc else None))

    kept, skipped = [], 0
    for entry in planned:
        if (entry["date"].isoformat(), entry["service_id"]) in existing_keys:
            skipped += 1
        else:
            kept.append(entry)
    return kept, skipped


def create_time_entry(entry: dict) -> dict:
    """POST a single time entry."""
    relationships = {
        "person": {"data": {"type": "people", "id": str(entry["person_id"])}},
        "service": {"data": {"type": "services", "id": str(entry["service_id"])}},
    }
    if entry.get("task_id"):
        relationships["task"] = {"data": {"type": "tasks", "id": str(entry["task_id"])}}

    payload = {
        "data": {
            "type": "time-entries",
            "attributes": {
                "date": entry["date"].isoformat(),
                "time": entry["time"],
                "billable_time": entry["time"],
                "note": entry.get("note", ""),
            },
            "relationships": relationships,
        }
    }
    return api_post("time_entries", payload)


# ---------------------------------------------------------------------------
# Batch execution helpers
# ---------------------------------------------------------------------------


def _run_batch(
    items: list,
    create_fn,
    label_fn,
    total: int,
    offset: int = 0,
) -> tuple[int, int]:
    """Execute API creates concurrently in batches of BATCH_SIZE.

    *create_fn(item)* → API response (or raises).
    *label_fn(index, item)* → human-readable progress tag for printing.
    Returns (ok_count, fail_count).
    """
    ok, fail = 0, 0

    for batch_start in range(0, len(items), BATCH_SIZE):
        batch = items[batch_start : batch_start + BATCH_SIZE]
        futures: dict = {}

        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
            for j, item in enumerate(batch):
                idx = offset + batch_start + j + 1
                futures[pool.submit(create_fn, item)] = (idx, item)

            for future in as_completed(futures):
                idx, item = futures[future]
                tag = label_fn(idx, item)
                try:
                    res = future.result()
                    res_id = res.get("data", {}).get("id", "?")
                    print(f"{tag}  ✅ id {res_id}")
                    ok += 1
                except requests.exceptions.HTTPError as e:
                    detail = ""
                    if e.response is not None:
                        try:
                            detail = json.dumps(e.response.json(), indent=2)
                        except Exception:
                            detail = e.response.text
                    print(f"{tag}  ❌ FAILED: {e}")
                    if detail:
                        for line in detail.split("\n"):
                            print(f"         {line}")
                    fail += 1

        # Pause between batches (not after the last one)
        if batch_start + BATCH_SIZE < len(items):
            time_mod.sleep(BATCH_DELAY)

    return ok, fail


# ---------------------------------------------------------------------------
# Name resolution helpers
# ---------------------------------------------------------------------------


def get_service_name(service_id: str) -> str:
    try:
        body = api_get(f"services/{service_id}")
        return body["data"]["attributes"].get("name", f"Service {service_id}")
    except Exception:
        return f"Service {service_id}"


# ---------------------------------------------------------------------------
# Utility commands
# ---------------------------------------------------------------------------


def cmd_whoami():
    _check_creds()
    try:
        body = api_get("user")
        user = body.get("data", {})
        attrs = user.get("attributes", {})
        name = f"{attrs.get('first_name', '')} {attrs.get('last_name', '')}".strip()
        pid = (
            user.get("relationships", {})
            .get("person", {})
            .get("data", {})
            .get("id", "N/A")
        )
        print(f"  Authenticated as:  {name}")
        print(f"  Person ID:         {pid}")

        # Show subsidiary info
        sub_id = resolve_subsidiary_id(SUBSIDIARY_NAME)
        if sub_id:
            print(f"  Subsidiary:        {SUBSIDIARY_NAME} (id {sub_id})")
        else:
            print(f"  Subsidiary:        ⚠️  '{SUBSIDIARY_NAME}' not found")

        # Show holiday calendar info
        hc_id = resolve_holiday_calendar_id(COUNTRY_CODE)
        if hc_id:
            print(f"  Holiday calendar:  id {hc_id} (country {COUNTRY_CODE})")
        else:
            print(
                f"  Holiday calendar:  ⚠️  no match for country '{COUNTRY_CODE}'\n"
                f"                     Run --list-calendars to check, or adjust "
                f"PRODUCTIVE_COUNTRY_CODE in .env"
            )

        print(f"\n  Add to your .env:  PRODUCTIVE_PERSON_ID={pid}")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            print(
                "Could not resolve from /user. Check your person ID in the Productive URL."
            )
        else:
            raise


def cmd_list_calendars():
    _check_creds()
    print("\n  Fetching holiday calendars…\n")
    calendars = fetch_holiday_calendars()
    if not calendars:
        print("  No holiday calendars found.")
        return

    print(f"  {'ID':<10} {'Name':<35} {'Country':<20} {'State'}")
    print(f"  {'─' * 10} {'─' * 35} {'─' * 20} {'─' * 20}")
    for cal in calendars:
        state = cal["state"] or "—"
        print(f"  {cal['id']:<10} {cal['name']:<35} {cal['country']:<20} {state}")
    print(
        f"\n  Current PRODUCTIVE_COUNTRY_CODE: {COUNTRY_CODE}\n"
        f"  To change, set PRODUCTIVE_COUNTRY_CODE in .env (e.g. US, HR, GB, DE)"
    )


def cmd_list_events():
    _check_creds()
    print(f"\n  Fetching absence events for {SUBSIDIARY_NAME}…\n")
    all_events = fetch_events()
    events = filter_events_by_subsidiary(all_events, SUBSIDIARY_NAME)
    if not events:
        print("  No events found.")
        return

    print(f"  {'ID':<10} {'Name':<30} {'Type':<12} {'Paid?':<10} {'Limit'}")
    print(f"  {'─' * 10} {'─' * 30} {'─' * 12} {'─' * 10} {'─' * 20}")
    for ev in events:
        print(
            f"  {ev['id']:<10} {ev['name']:<30} "
            f"{ev['absence_type']:<12} "
            f"{ev['paid']:<10} "
            f"{ev['limit']}"
        )
    filtered_out = len(all_events) - len(events)
    if filtered_out:
        print(f"\n  ({filtered_out} event(s) from other offices hidden)")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def print_plan(
    absences: list[dict],
    time_entries: list[dict],
    holidays: dict[date, str],
    service_names: dict[str, str],
    month_start: date,
    month_end: date,
    te_skipped: int,
):
    all_weekdays = weekdays_in_range(month_start, month_end)
    absence_day_count = sum(len(a["work_days"]) for a in absences)
    holiday_weekdays = sum(1 for d in holidays if d in set(all_weekdays))
    te_unique_dates = len(set(e["date"] for e in time_entries))

    print(f"\n{'=' * 72}")
    print(f"  PLAN FOR {month_start.strftime('%B %Y').upper()}")
    print(f"{'=' * 72}")

    # -- Holidays --
    if holidays:
        print(f"\n  🎄 Holidays ({len(holidays)}):")
        for d in sorted(holidays):
            print(f"     {d.isoformat()}  {d.strftime('%a')}  {holidays[d]}")

    # -- Absences grouped by event type --
    if absences:
        by_event: dict[str, list[dict]] = {}
        for a in absences:
            by_event.setdefault(a["event_name"], []).append(a)

        for event_name, entries in by_event.items():
            event_id = entries[0]["event_id"]
            total_days = sum(len(e["work_days"]) for e in entries)
            print(
                f"\n  🗓️  {event_name} → {len(entries)} absence booking(s), "
                f"{total_days} day(s)  (event {event_id})"
            )
            for a in entries:
                rng = (
                    a["start"].isoformat()
                    if a["start"] == a["end"]
                    else f"{a['start']} → {a['end']}"
                )
                note = a["note"] or "—"
                print(f"     {rng:<28} {len(a['work_days'])} day(s)   {note}")

    # -- Time entries --
    if time_entries:
        by_svc: dict[str, list] = {}
        for e in time_entries:
            by_svc.setdefault(e["service_id"], []).append(e)

        print("\n  📁 Time entries from project bookings:")
        for sid, entries in sorted(by_svc.items()):
            sname = service_names.get(sid, f"Service {sid}")
            total = sum(e["time"] for e in entries)
            print(f"\n     {sname}  (service {sid})")
            print(f"     {len(entries)} entries, {fmt_min(total)}")
            print(f"     {'Date':<14} {'Day':<6} {'Time':<10} {'Task':<10} Note")
            print(f"     {'─' * 56}")
            for e in sorted(entries, key=lambda x: x["date"]):
                tsk = e.get("task_id") or "—"
                note = e.get("note") or "—"
                if len(note) > 30:
                    note = note[:30] + "…"
                print(
                    f"     {e['date'].isoformat():<14} {e['date'].strftime('%a'):<6} "
                    f"{fmt_min(e['time']):<10} {tsk:<10} {note}"
                )

    # -- Summary --
    print(f"\n{'─' * 72}")
    print(f"  📊 {month_start.strftime('%B %Y')} breakdown:")
    print(f"     Total weekdays:       {len(all_weekdays)}")
    if holiday_weekdays:
        print(f"     Holidays:            -{holiday_weekdays}")
    if absence_day_count:
        print(f"     Days off:            -{absence_day_count}")
    print(f"     Filled by bookings:   {te_unique_dates}")
    if te_skipped:
        print(f"     Already existed:      {te_skipped} (skipped)")
    print(f"{'─' * 72}")

    total_ops = len(absences) + len(time_entries)
    num_batches = (total_ops + BATCH_SIZE - 1) // BATCH_SIZE
    print(
        f"\n  Will create: {len(absences)} absence booking(s) "
        f"+ {len(time_entries)} time entry/entries"
        f"\n  {total_ops} API calls in {num_batches} batch(es) of up to {BATCH_SIZE}\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Fill out your Productive.io timesheet for a month."
    )
    parser.add_argument(
        "--month",
        type=str,
        default=None,
        help="YYYY-MM (default: previous month, or current month in its last 5 days)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only, don't create anything."
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip final confirmation."
    )
    parser.add_argument(
        "--no-prompts", action="store_true", help="Skip absence prompts."
    )
    parser.add_argument("--whoami", action="store_true", help="Look up your person ID.")
    parser.add_argument(
        "--list-events", action="store_true", help="List absence events."
    )
    parser.add_argument(
        "--list-calendars",
        action="store_true",
        help="List holiday calendars (to find your calendar ID).",
    )
    args = parser.parse_args()

    _check_creds()

    if args.whoami:
        cmd_whoami()
        return
    if args.list_events:
        cmd_list_events()
        return
    if args.list_calendars:
        cmd_list_calendars()
        return

    if not PERSON_ID:
        sys.exit(
            "PRODUCTIVE_PERSON_ID not set.\nRun: python sync_time_entries.py --whoami"
        )

    # -- Resolve month --
    if args.month:
        try:
            y, m = args.month.split("-")
            year, month = int(y), int(m)
        except (ValueError, IndexError):
            sys.exit("--month must be YYYY-MM (e.g. 2025-06)")
    else:
        today = date.today()
        _, last_day = monthrange(today.year, today.month)
        days_left = last_day - today.day
        if days_left < 5:
            # Near end of month — fill out the current month
            year, month = today.year, today.month
        else:
            # Default to previous month (the one you're retroactively filling)
            first_of_this = date(today.year, today.month, 1)
            prev = first_of_this - timedelta(days=1)
            year, month = prev.year, prev.month

    _, last = monthrange(year, month)
    month_start = date(year, month, 1)
    month_end = date(year, month, last)

    print(f"\n📅 {month_start.strftime('%B %Y')}")
    print(f"   {month_start} → {month_end}  ·  Person {PERSON_ID}")

    # ── 0. Resolve subsidiary & holiday calendar ─────────────────────────
    print(f"\n⏳ Resolving subsidiary '{SUBSIDIARY_NAME}'…")
    subsidiary_id = resolve_subsidiary_id(SUBSIDIARY_NAME)
    if subsidiary_id:
        print(f"   ✅ {SUBSIDIARY_NAME} → subsidiary {subsidiary_id}")
    else:
        print(
            f"   ⚠️  Subsidiary '{SUBSIDIARY_NAME}' not found — "
            "holidays & bookings will not be filtered by office."
        )

    print(f"⏳ Resolving holiday calendar for country '{COUNTRY_CODE}'…")
    holiday_calendar_id = resolve_holiday_calendar_id(COUNTRY_CODE)
    if holiday_calendar_id:
        print(f"   ✅ Holiday calendar {holiday_calendar_id}")
    else:
        print(
            f"   ⚠️  No holiday calendar matched country '{COUNTRY_CODE}' — "
            "will fetch all org holidays.\n"
            "       Run --list-calendars to check, or adjust "
            "PRODUCTIVE_COUNTRY_CODE in .env"
        )

    # ── 1. Fetch holidays ────────────────────────────────────────────────
    print("\n⏳ Fetching holidays…")
    holidays = fetch_holidays(month_start, month_end, holiday_calendar_id)
    if holidays:
        print(
            f"   {len(holidays)} holiday(s) found: "
            f"{', '.join(holidays[d] for d in sorted(holidays))}"
        )
    else:
        print("   No holidays this month.")

    skip_dates: set[date] = set(holidays.keys())

    # ── 2. Fetch existing absence bookings ───────────────────────────────
    print("\n⏳ Checking existing absence bookings…")
    existing_absences = fetch_existing_absences(month_start, month_end, PERSON_ID)
    if existing_absences:
        total_days = sum(len(a["work_days"]) for a in existing_absences)
        print(
            f"   {len(existing_absences)} booking(s), {total_days} day(s) already booked"
        )
        for a in existing_absences:
            skip_dates.update(a["work_days"])
    else:
        print("   No existing absence bookings.")

    # ── 3. Absence prompts (interactive, event types fetched live) ───────
    absences: list[dict] = []

    if not args.no_prompts:
        absences = prompt_all_absences(month_start, month_end, existing_absences)
        for a in absences:
            skip_dates.update(a["work_days"])

    # ── 4. Fetch budget bookings & build time entries ────────────────────
    print("\n⏳ Fetching project bookings…")
    budget_bookings = fetch_budget_bookings(
        month_start, month_end, PERSON_ID, subsidiary_id
    )
    print(f"   {len(budget_bookings)} booking(s)")

    time_entries = build_time_entries(
        budget_bookings, month_start, month_end, skip_dates
    )

    # ── 5. Deduplicate ───────────────────────────────────────────────────
    print("\n⏳ Checking for existing time entries…")
    existing_te = api_get_all(
        "time_entries",
        {
            "filter[person_id]": PERSON_ID,
            "filter[after]": (month_start - timedelta(days=1)).isoformat(),
            "filter[before]": (month_end + timedelta(days=1)).isoformat(),
            "page[size]": 200,
        },
    )
    print(f"   {len(existing_te)} existing")

    time_entries, te_skipped = deduplicate(time_entries, existing_te)
    if te_skipped:
        print(f"   Skipping {te_skipped} duplicate(s)")

    # ── 6. Resolve service names ─────────────────────────────────────────
    service_names = {}
    for sid in set(e["service_id"] for e in time_entries):
        service_names[sid] = get_service_name(sid)

    # ── 7. Preview ───────────────────────────────────────────────────────
    if not absences and not time_entries:
        print("\n✅ Nothing to create.")
        return

    print_plan(
        absences,
        time_entries,
        holidays,
        service_names,
        month_start,
        month_end,
        te_skipped,
    )

    if args.dry_run:
        print("🔒 Dry run — nothing created. Remove --dry-run to execute.\n")
        return

    # ── 8. Confirm ───────────────────────────────────────────────────────
    total_ops = len(absences) + len(time_entries)
    num_batches = (total_ops + BATCH_SIZE - 1) // BATCH_SIZE
    if not args.yes:
        if input(
            f"Proceed? ({total_ops} calls, {num_batches} batch(es)) [y/N] "
        ).strip().lower() not in (
            "y",
            "yes",
        ):
            print("Cancelled.\n")
            return

    # ── 9. Create absence bookings ───────────────────────────────────────
    absence_ok, absence_fail = 0, 0

    if absences:
        print(
            f"\n🗓️  Creating {len(absences)} absence booking(s) "
            f"(batches of {BATCH_SIZE})…\n"
        )

        def _create_absence(a):
            return create_absence_booking(
                a["event_id"],
                PERSON_ID,
                a["start"],
                a["end"],
                WORK_DAY_MINUTES,
                a["note"],
            )

        def _absence_label(idx, a):
            rng = (
                a["start"].isoformat()
                if a["start"] == a["end"]
                else f"{a['start']} → {a['end']}"
            )
            return (
                f"   [{idx}/{len(absences)}] 🗓️  {a['event_name']}: "
                f"{rng}  ({len(a['work_days'])} days)"
            )

        absence_ok, absence_fail = _run_batch(
            absences, _create_absence, _absence_label, len(absences)
        )

    # ── 10. Create time entries ──────────────────────────────────────────
    te_ok, te_fail = 0, 0

    if time_entries:
        print(
            f"\n🚀 Creating {len(time_entries)} time entries "
            f"(batches of {BATCH_SIZE})…\n"
        )

        def _te_label(idx, entry):
            sname = service_names.get(entry["service_id"], entry["service_id"])
            return (
                f"   [{idx}/{len(time_entries)}] 📁 {entry['date']}  "
                f"{sname}  {fmt_min(entry['time'])}"
            )

        te_ok, te_fail = _run_batch(
            time_entries, create_time_entry, _te_label, len(time_entries)
        )

    # ── Done ─────────────────────────────────────────────────────────────
    total_ok = absence_ok + te_ok
    total_fail = absence_fail + te_fail

    print(f"\n{'=' * 72}")
    print("  ✅ Done!")
    if absences:
        print(
            f"     Absence bookings:  {absence_ok} created"
            f"{f', {absence_fail} failed' if absence_fail else ''}"
        )
    if time_entries or te_skipped:
        print(
            f"     Time entries:      {te_ok} created"
            f"{f', {te_fail} failed' if te_fail else ''}"
            f"{f', {te_skipped} skipped' if te_skipped else ''}"
        )
    print(f"     Total: {total_ok} created, {total_fail} failed")
    print(f"{'=' * 72}\n")


if __name__ == "__main__":
    main()
