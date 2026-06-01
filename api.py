from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sync_time_entries import (
    BATCH_SIZE,
    COUNTRY_CODE,
    PERSON_ID,
    SUBSIDIARY_NAME,
    WORK_DAY_MINUTES,
    _check_creds,
    api_get_all,
    build_time_entries,
    create_absence_booking,
    create_time_entry,
    deduplicate,
    fetch_budget_bookings,
    fetch_events,
    fetch_existing_absences,
    fetch_holidays,
    filter_events_by_subsidiary,
    fmt_min,
    get_service_name,
    parse_date_input,
    resolve_holiday_calendar_id,
    resolve_subsidiary_id,
    weekdays_in_range,
)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class InitMonthRequest(BaseModel):
    month: Optional[str] = None


class ParseDatesRequest(BaseModel):
    raw: str
    month_start: str
    month_end: str


class AbsenceInput(BaseModel):
    event_id: str
    event_name: str
    start: str
    end: str
    note: str = ""
    work_days: list[str] = []


class PreviewRequest(BaseModel):
    month_start: str
    month_end: str
    subsidiary_id: Optional[str] = None
    absences: list[AbsenceInput] = []
    skip_dates: list[str] = []


class ExecuteAbsence(BaseModel):
    event_id: str
    start: str
    end: str
    note: str = ""


class ExecuteTimeEntry(BaseModel):
    date: str
    service_id: str
    task_id: Optional[str] = None
    time: int
    note: str = ""


class ExecuteRequest(BaseModel):
    absences: list[ExecuteAbsence] = []
    time_entries: list[ExecuteTimeEntry] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _month_bounds(month_str: str) -> tuple[date, date]:
    y, m = map(int, month_str.split("-"))
    month_start = date(y, m, 1)
    if m == 12:
        month_end = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(y, m + 1, 1) - timedelta(days=1)
    return month_start, month_end


def _serialize_absence(a: dict) -> dict:
    return {
        **a,
        "start": str(a["start"]) if isinstance(a["start"], date) else a["start"],
        "end": str(a["end"]) if isinstance(a["end"], date) else a["end"],
        "work_days": [
            str(d) if isinstance(d, date) else d for d in a.get("work_days", [])
        ],
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/api/config")
def get_config():
    errors: list[str] = []
    try:
        _check_creds()
    except SystemExit:
        errors.append(
            "Missing required environment variables (API_TOKEN, ORG_ID, PERSON_ID)"
        )
    return {
        "person_id": PERSON_ID,
        "subsidiary_name": SUBSIDIARY_NAME,
        "country_code": COUNTRY_CODE,
        "work_day_minutes": WORK_DAY_MINUTES,
        "errors": errors,
    }


@app.post("/api/init-month")
def init_month(body: InitMonthRequest):
    warnings: list[str] = []

    if body.month:
        month_start, month_end = _month_bounds(body.month)
    else:
        from calendar import monthrange

        today = date.today()
        _, last_day = monthrange(today.year, today.month)
        days_left = last_day - today.day
        if days_left < 5:
            year, month = today.year, today.month
        else:
            first_of_this = date(today.year, today.month, 1)
            prev = first_of_this - timedelta(days=1)
            year, month = prev.year, prev.month
        month_start = date(year, month, 1)
        _, last = monthrange(year, month)
        month_end = date(year, month, last)

    month_str = month_start.strftime("%Y-%m")

    subsidiary_id = resolve_subsidiary_id(SUBSIDIARY_NAME)
    if subsidiary_id is None and SUBSIDIARY_NAME:
        warnings.append(f"subsidiary not found: {SUBSIDIARY_NAME}")

    holiday_calendar_id = resolve_holiday_calendar_id(COUNTRY_CODE)
    if holiday_calendar_id is None and COUNTRY_CODE:
        warnings.append(f"holiday calendar not found for {COUNTRY_CODE}")

    holidays = fetch_holidays(month_start, month_end, holiday_calendar_id)
    existing = fetch_existing_absences(month_start, month_end, PERSON_ID)

    return {
        "month": month_str,
        "month_start": str(month_start),
        "month_end": str(month_end),
        "subsidiary_id": subsidiary_id,
        "holiday_calendar_id": holiday_calendar_id,
        "holidays": {str(k): v for k, v in holidays.items()},
        "existing_absences": [_serialize_absence(a) for a in existing],
        "warnings": warnings,
    }


@app.get("/api/events")
def get_events():
    events = fetch_events()
    filtered = filter_events_by_subsidiary(events, SUBSIDIARY_NAME)
    return filtered


@app.post("/api/parse-dates")
def parse_dates(body: ParseDatesRequest):
    month_start = _date(body.month_start)
    month_end = _date(body.month_end)
    ranges, errors = parse_date_input(body.raw, month_start, month_end)
    result = []
    for start, end in ranges:
        work_days = weekdays_in_range(start, end)
        result.append(
            {
                "start": str(start),
                "end": str(end),
                "work_days": [str(d) for d in work_days],
            }
        )
    return {"ranges": result, "errors": errors}


@app.post("/api/preview")
def preview(body: PreviewRequest):
    month_start = _date(body.month_start)
    month_end = _date(body.month_end)
    skip_dates = {_date(d) for d in body.skip_dates}

    bookings = fetch_budget_bookings(
        month_start, month_end, PERSON_ID, body.subsidiary_id
    )
    planned_entries = build_time_entries(bookings, month_start, month_end, skip_dates)

    existing_te = api_get_all(
        "time_entries",
        {
            "filter[person_id]": PERSON_ID,
            "filter[after]": (month_start - timedelta(days=1)).isoformat(),
            "filter[before]": (month_end + timedelta(days=1)).isoformat(),
            "page[size]": 200,
        },
    )
    new_entries, te_skipped = deduplicate(planned_entries, existing_te)

    # Resolve service names and serialize
    time_entries_out = []
    for e in new_entries:
        time_entries_out.append(
            {
                "date": str(e["date"]) if isinstance(e["date"], date) else e["date"],
                "service_id": e["service_id"],
                "service_name": get_service_name(e["service_id"]),
                "task_id": e["task_id"],
                "person_id": e.get("person_id", PERSON_ID),
                "time": e["time"],
                "time_display": fmt_min(e["time"]),
                "note": e.get("note", ""),
            }
        )

    absences_out = [a.model_dump() for a in body.absences]

    all_weekdays = weekdays_in_range(month_start, month_end)
    holiday_weekdays = sum(
        1
        for d in skip_dates
        if d in set(all_weekdays)
        and d not in {_date(wd) for a in body.absences for wd in a.work_days}
    )
    absence_days = sum(len(a.work_days) for a in body.absences)
    total_ops = len(body.absences) + len(time_entries_out)

    return {
        "absences": absences_out,
        "time_entries": time_entries_out,
        "te_skipped": te_skipped,
        "summary": {
            "total_weekdays": len(all_weekdays),
            "holiday_weekdays": holiday_weekdays,
            "absence_days": absence_days,
            "filled_days": len(time_entries_out),
            "skipped": te_skipped,
            "total_ops": total_ops,
            "batches": math.ceil(total_ops / BATCH_SIZE) if BATCH_SIZE else 1,
        },
    }


@app.post("/api/execute")
def execute(body: ExecuteRequest):
    results: list[dict] = []
    absence_ok = 0
    absence_fail = 0
    te_ok = 0
    te_fail = 0

    for a in body.absences:
        start = _date(a.start)
        end = _date(a.end)
        try:
            resp = create_absence_booking(
                a.event_id,
                PERSON_ID,
                start,
                end,
                WORK_DAY_MINUTES,
                a.note,
            )
            absence_ok += 1
            results.append(
                {
                    "type": "absence",
                    "label": f"Absence {a.start} → {a.end}",
                    "status": "ok",
                    "id": str(resp.get("id", "")),
                    "error": None,
                }
            )
        except Exception as exc:
            absence_fail += 1
            results.append(
                {
                    "type": "absence",
                    "label": f"Absence {a.start} → {a.end}",
                    "status": "failed",
                    "id": None,
                    "error": str(exc),
                }
            )

    for te in body.time_entries:
        entry = {
            "date": _date(te.date),
            "service_id": te.service_id,
            "task_id": te.task_id,
            "person_id": PERSON_ID,
            "time": te.time,
            "note": te.note,
        }
        try:
            resp = create_time_entry(entry)
            te_ok += 1
            results.append(
                {
                    "type": "time_entry",
                    "label": f"TE {te.date} {fmt_min(te.time)}",
                    "status": "ok",
                    "id": str(resp.get("id", "")),
                    "error": None,
                }
            )
        except Exception as exc:
            te_fail += 1
            results.append(
                {
                    "type": "time_entry",
                    "label": f"TE {te.date} {fmt_min(te.time)}",
                    "status": "failed",
                    "id": None,
                    "error": str(exc),
                }
            )

    return {
        "absence_ok": absence_ok,
        "absence_fail": absence_fail,
        "te_ok": te_ok,
        "te_fail": te_fail,
        "results": results,
    }


# Static files mounted last so API routes take priority
app.mount("/", StaticFiles(directory="static", html=True), name="static")
