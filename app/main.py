"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = ZoneInfo("Asia/Kolkata")

STATUSES = {"PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"}
PRESENT_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
NON_PRESENT_STATUSES = {"ABSENT", "LEAVE"}

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


IST = timezone(timedelta(hours=5, minutes=30))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes when the application starts

    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )

    db.employees.create_index(
        [("joined_on", 1)],
        name="idx_employees_joined_on",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    #index for attendance queries
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="idx_attendance_date_emp_code",
    )
    
    db.employees.create_index(
    [("department", 1), ("joined_on", 1)],
    name="idx_employees_department_joined_on",
    )

    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="idx_employees_department_emp_code",
    )
    
    db.attendance_logs.create_index(
        [("date", 1), ("status", 1), ("emp_code", 1)],
        name="idx_attendance_date_status_emp_code",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("punch_in", -1)],
        name="idx_attendance_emp_punch_in",
    )

    db.attendance_logs.create_index(
        [("date", 1), ("late_minutes", 1), ("emp_code", 1)],
        name="idx_attendance_date_late_emp",
    )

    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #



def compute_late_minutes(punch_in: datetime, emp: dict) -> int:
    if punch_in is None:
        return 0

    local_in = as_utc(punch_in).astimezone(IST)
    shift_start = time.fromisoformat(emp["shift_start"])
    shift_end = time.fromisoformat(emp["shift_end"])

    # For overnight shifts, early-morning punches belong to
    # the previous day's shift and cannot be late.
    if shift_end <= shift_start and local_in.time() < shift_end:
        return 0

    shift_day = local_in.date()

    scheduled_start = datetime.combine(
        shift_day,
        shift_start,
        tzinfo=IST,
    )

    elapsed_seconds = (
        as_utc(punch_in) - scheduled_start.astimezone(UTC)
    ).total_seconds()

    # Late only when strictly more than 10 minutes after shift start.
    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    return round((punch_out - punch_in).total_seconds() / 3600, 2)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))

    shift_day = datetime.fromisoformat(date_str).date()
    scheduled_end = datetime.combine(
        shift_day,
        time(h, m),
        tzinfo=IST,
    )

    punch_out_utc = as_utc(punch_out)
    scheduled_end_utc = scheduled_end.astimezone(UTC)

    elapsed_seconds = (
        punch_out_utc - scheduled_end_utc
    ).total_seconds()

    # Overtime starts only after at least 30 minutes.
    if elapsed_seconds < 30 * 60:
        return 0

    return int(elapsed_seconds // 60)


def as_utc(value: datetime) -> datetime:
    """Treat naive MongoDB datetimes as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_ms(value):
    """Convert a datetime to integer epoch milliseconds."""
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def calculate_work_hours(punch_in, punch_out):
    seconds = int(
        (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    )
    hours = Decimal(seconds) / Decimal(3600)
    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def calculate_overtime(punch_out, attendance_date, emp):
    """Calculate whole overtime minutes after the scheduled shift end."""
    shift_start = time.fromisoformat(emp["shift_start"])
    shift_end = time.fromisoformat(emp["shift_end"])
    day = date.fromisoformat(attendance_date)

    end_day = day
    if shift_end <= shift_start:
        end_day += timedelta(days=1)

    shift_end_ist = datetime.combine(
        end_day, shift_end, tzinfo=IST
    )
    elapsed = (
        as_utc(punch_out) - shift_end_ist.astimezone(UTC)
    ).total_seconds()

    minutes = max(0, int(elapsed // 60))
    return minutes if minutes >= 30 else 0


def regularize_late_minutes(punch_in, emp):
    if punch_in is None:
        return 0

    local_in = as_utc(punch_in).astimezone(IST)
    shift_start = time.fromisoformat(emp["shift_start"])
    shift_end = time.fromisoformat(emp["shift_end"])

    shift_day = local_in.date()

    
    if shift_end <= shift_start and local_in.time() < shift_end:
        shift_day -= timedelta(days=1)

    scheduled_start = datetime.combine(
        shift_day, shift_start, tzinfo=IST
    )
    elapsed = (
        as_utc(punch_in) - scheduled_start.astimezone(UTC)
    ).total_seconds()

    return int(elapsed // 60) if elapsed > 10 * 60 else 0


def regularize_attendance_date(punch_in, emp):
    local_in = as_utc(punch_in).astimezone(IST)
    shift_start = time.fromisoformat(emp["shift_start"])
    shift_end = time.fromisoformat(emp["shift_end"])

    attendance_day = local_in.date()

    if shift_end <= shift_start and local_in.time() < shift_end:
        attendance_day -= timedelta(days=1)

    return attendance_day.isoformat()


def regularize_public_record(record):
    record.pop("_id", None)

    record["punch_in"] = epoch_ms(record.get("punch_in"))
    record["punch_out"] = epoch_ms(record.get("punch_out"))

    record.setdefault("work_hours", None)
    record.setdefault("late_minutes", 0)
    record.setdefault("overtime_minutes", 0)
    record.setdefault("half_day", False)
    record.setdefault("history", [])

    for entry in record["history"]:
        if entry.get("at") is not None:
            entry["at"] = epoch_ms(entry["at"])

        for change in entry.get("changes", {}).values():
            for key in ("from", "to"):
                value = change.get(key)
                if isinstance(value, datetime):
                    change[key] = epoch_ms(value)

    return record


def validate_month(month: str):
    """Validate YYYY-MM and return the first and last day."""
    try:
        if (
            len(month) != 7
            or month[4] != "-"
            or not month[:4].isdigit()
            or not month[5:].isdigit()
        ):
            raise ValueError

        year = int(month[:4])
        month_num = int(month[5:])

        if not 1 <= month_num <= 12:
            raise ValueError

        last_day = monthrange(year, month_num)[1]
        return date(year, month_num, 1), date(year, month_num, last_day)

    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format",
        )

def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="Date must use YYYY-MM-DD format",
        )

    if parsed.isoformat() != value:
        raise HTTPException(
            status_code=422,
            detail="Date must use YYYY-MM-DD format",
        )

    return parsed



def build_employee_monthly_pipeline(emp_code, effective_start, period_end):
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": effective_start.isoformat(),
                    "$lte": period_end.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_date_obj": {
                    "$dateFromString": {
                        "dateString": "$date",
                        "format": "%Y-%m-%d",
                    }
                }
            }
        },
        {
            "$addFields": {
                "_weekday": {"$isoDayOfWeek": "$_date_obj"}
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_STATUSES)]},
                                    {"$lte": ["$_weekday", 5]},
                                ]
                            },
                            {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {"$ifNull": ["$half_day", False]},
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
):
    period_start, period_end = validate_month(month)

    # Verify employee exists and retrieve joining date.
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    joined_on = date.fromisoformat(employee["joined_on"])

    # R7: Count Monday-Friday from the joining date.
    effective_start = max(period_start, joined_on)
    working_days = 0
    current_day = effective_start

    while current_day <= period_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    pipeline = build_employee_monthly_pipeline(
    emp_code,
    effective_start,
    period_end,
    )

    results = list(db.attendance_logs.aggregate(pipeline))

    metrics = results[0] if results else {
        "present_days": 0,
        "leave_days": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "total_overtime_minutes": 0,
    }

    present_days = metrics["present_days"]
    
    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = float(
            (
                Decimal(str(present_days))
                / Decimal(working_days)
                * Decimal(100)
            ).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )


    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": metrics["leave_days"],
        "late_count": metrics["late_count"],
        "total_late_minutes": metrics["total_late_minutes"],
        "total_overtime_minutes": metrics["total_overtime_minutes"],
        "attendance_pct": attendance_pct,
    }
# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        min_length=3,
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)

    shift_start: str = Field(
        default="09:30",
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
    )

    joined_on: str

    @field_validator("name", "department", "email")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Field cannot be blank")
        return value

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date")

        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must be different")
        return self

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        gt=100000000000,
        le=4102444800000
    )
    status: Literal[
    "PRESENT",
    "WFH",
    "ON_DUTY",
    "ABSENT",
    "LEAVE"
] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def validate_punched_at(cls, value):
        if value is not None and value < 100000000000:
            raise ValueError("punched_at must be epoch milliseconds")
        return value

#punch_out model
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        gt=100000000000,
        le=4102444800000,
    )


#patch attendance done by me


class RegularizeIn(BaseModel):
    status: Optional[Literal[
        "PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"
    ]] = None

    punch_in: Optional[StrictInt] = Field(
        default=None, gt=100000000000, le=4102444800000
    )
    punch_out: Optional[StrictInt] = Field(
        default=None, gt=100000000000, le=4102444800000
    )

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    
    @field_validator("reason", "regularized_by")
    @classmethod
    def validate_nonblank_text(cls, value):
        if not value.strip():
            raise ValueError("Value must not be blank")
        return value


    @model_validator(mode="before")
    @classmethod
    def validate_regularization_input(cls, values):
        if isinstance(values, dict):
            if "status" in values and values["status"] is None:
                raise ValueError("status cannot be null")

            for field in ("punch_in", "punch_out"):
                if field in values and values[field] is None:
                    raise ValueError(f"{field} cannot be null")

        return values




# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            status_code=503,
            detail="MongoDB is unavailable"
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()

    # Store created_at as a UTC datetime with whole-second precision
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )
    
    # Never expose MongoDB internal ID in the API response
    doc.pop("_id", None)

    # Convert created_at to epoch milliseconds
    doc["created_at"] = epoch_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}

    if department:
        q["department"] = department

    skip = (page - 1) * page_size

    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    # Convert created_at to epoch milliseconds for the API response
    for employee in items:
        if employee.get("created_at") is not None:
            employee["created_at"] = epoch_ms(employee["created_at"])

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    # Convert epoch milliseconds to a UTC instant, then to IST.
    if body.punched_at is not None:
        ts_utc = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc
        )
    else:
        ts_utc = datetime.now(timezone.utc)

    # Truncate to whole seconds, as required by the assignment.
    ts_utc = ts_utc.replace(microsecond=0)

    ist = ZoneInfo("Asia/Kolkata")
    ts = ts_utc.astimezone(ist)

    # Calculate the attendance date in IST.
    d = ts.date().isoformat()

    # Overnight shift: early-morning punch-ins belong to the
    # previous day's shift when they occur before shift_end.
    if emp["shift_end"] <= emp["shift_start"]:
        punch_time = ts.strftime("%H:%M")
        if punch_time < emp["shift_end"]:
            from datetime import timedelta
            d = (ts.date() - timedelta(days=1)).isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_utc, emp),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        )

    # Return API fields, not MongoDB's internal ID.
    doc.pop("_id", None)

    # API instants are epoch milliseconds.
    doc["punch_in"] = int(ts_utc.timestamp() * 1000)
    doc["punch_out"] = None

    return doc



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    
    start = parse_date(date_from) if date_from is not None else None
    end = parse_date(date_to) if date_to is not None else None

    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    if status is not None and status not in STATUSES:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status",
        )
    q = {}

    if emp_code is not None:
        q["emp_code"] = emp_code

    if start is not None or end is not None:
        q["date"] = {}

        if start is not None:
            q["date"]["$gte"] = start.isoformat()

        if end is not None:
            q["date"]["$lte"] = end.isoformat()

    if status is not None:
        q["status"] = status

    # Count all records matching the filters.
    total = db.attendance_logs.count_documents(q)

    # Let MongoDB handle sorting and pagination.
    skip = (page - 1) * page_size
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    for d in docs:
        # Convert punch timestamps to epoch milliseconds.
        d["punch_in"] = epoch_ms(d.get("punch_in"))
        d["punch_out"] = epoch_ms(d.get("punch_out"))

        # Supply defaults for older records.
        d.setdefault("work_hours", None)
        d.setdefault("late_minutes", 0)
        d.setdefault("overtime_minutes", 0)
        d.setdefault("half_day", False)
        d["history"] = d.get("history") or []

        # Convert timestamps inside correction history.
        for entry in d["history"]:
            if entry.get("at") is not None:
                entry["at"] = epoch_ms(entry["at"])

            for change in entry.get("changes", {}).values():
                for key in ("from", "to"):
                    if isinstance(change.get(key), datetime):
                        change[key] = epoch_ms(change[key])

    return {
        "items": docs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


#patch attendance

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn,
):
    # Validate the calendar date strictly.
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
        if parsed_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must be in YYYY-MM-DD format",
        )

    # Check employee and attendance record.
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    supplied = body.model_dump(exclude_unset=True)
    reason = supplied["reason"]
    regularized_by = supplied["regularized_by"]

    old_status = record.get("status")
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    # Normalize stored datetimes and truncate to whole seconds.
    old_in = (
        as_utc(old_in).replace(microsecond=0)
        if old_in is not None else None
    )
    old_out = (
        as_utc(old_out).replace(microsecond=0)
        if old_out is not None else None
    )

    new_status = supplied.get("status", old_status)
    new_in = (
        datetime.fromtimestamp(supplied["punch_in"] / 1000, UTC)
        .replace(microsecond=0)
        if "punch_in" in supplied else old_in
    )
    new_out = (
        datetime.fromtimestamp(supplied["punch_out"] / 1000, UTC)
        .replace(microsecond=0)
        if "punch_out" in supplied else old_out
    )

    # ABSENT and LEAVE require null punch times.
    if new_status in ("ABSENT", "LEAVE"):
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="Cannot supply punch times with ABSENT or LEAVE",
            )
        new_in = None
        new_out = None

    # A presence status requires a punch-in.
    elif new_in is None:
        raise HTTPException(
            status_code=422,
            detail="A presence status requires punch_in",
        )

    # The punch-in must belong to the record's attendance date.
    if new_in is not None:
        calculated_date = regularize_attendance_date(new_in, emp)
        if calculated_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

    # A punch-out must be after punch-in and no more than 24 hours later.
    if new_out is not None:
        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out requires punch_in",
            )

        elapsed = (new_out - new_in).total_seconds()
        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="Punch-out must be after punch-in and within 24 hours",
            )

    # Recompute all derived fields from the final values.
    if new_status in ("ABSENT", "LEAVE"):
        derived = {
            "late_minutes": 0,
            "work_hours": None,
            "overtime_minutes": 0,
            "half_day": False,
        }
    else:
        late_minutes = regularize_late_minutes(new_in, emp)

        if new_out is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = calculate_work_hours(new_in, new_out)
            overtime_minutes = calculate_overtime(new_out, date, emp)
            half_day = work_hours < 4.50

        derived = {
            "late_minutes": late_minutes,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }

    final_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived,
    }

    # Compare old and new values to record only actual changes.
    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }

    changes = {}

    for field, new_value in final_values.items():
        old_value = old_values[field]

        if field in ("punch_in", "punch_out"):
            old_value = (
                as_utc(old_value).replace(microsecond=0)
                if old_value is not None else None
            )

        if old_value != new_value:
        # Store BSON-compatible UTC datetimes in MongoDB history.
        # Convert them to epoch milliseconds only when serializing
        # the API response.
            new_value_for_history = new_value

            changes[field] = {
                "from": old_value,
                "to": new_value_for_history,
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request does not change the attendance record",
        )

    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": regularized_by,
        "reason": reason,
        "changes": changes,
    }

    # Optimistic concurrency check: only update if the history is
    # still the version we originally read.
    
    old_history = record.get("history") or []

    concurrency_filter = {
        "_id": record["_id"],
    }

    if "history" in record:
        concurrency_filter["history"] = old_history
    else:
        concurrency_filter["history"] = {"$exists": False}

    result = db.attendance_logs.update_one(
        concurrency_filter,
        {
            "$set": final_values,
            "$push": {"history": history_entry},
        },
    )


    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the correction",
        )

    updated = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date,
    })

    return regularize_public_record(updated)


#post attendance/punch-out
@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    # Use the supplied instant or the current time.
    if body.punched_at is None:
        ts = datetime.now(UTC)
    else:
        ts = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=UTC,
        )

    # Truncate to whole seconds.
    ts = ts.replace(microsecond=0)

    # Find the most recent eligible punch-in, including one
    # that has already been closed.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", -1)],
    )

    
    
    if record is None:
        # Look for an existing punch-in, including one after ts.
        existing_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None},
            },
            sort=[("punch_in", -1)],
        )

        if existing_record is not None:
            existing_punch_in = as_utc(existing_record["punch_in"])

            if ts <= existing_punch_in:
                raise HTTPException(
                    status_code=422,
                    detail="Punch-out timestamp must be after punch-in",
                )

        raise HTTPException(
            status_code=404,
            detail="No punch-in found",
        )

    



    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Record is already punched out",
        )

    punch_in = as_utc(record["punch_in"])
    elapsed = (ts - punch_in).total_seconds()

    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in and within 24 hours",
        )

    work_hours = calculate_work_hours(punch_in, ts)
    overtime_minutes = calculate_overtime(
        ts, record["date"], emp
    )
    half_day = work_hours < 4.50

    # Atomic conditional update: only one concurrent request
    # can close a record whose punch_out is still null/missing.
    
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$or": [
                {"punch_out": None},
                {"punch_out": {"$exists": False}},
            ],
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )


    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Record is already punched out",
        )

    # Serialize the record for the HTTP API.
    updated.pop("_id", None)
    updated["punch_in"] = epoch_ms(updated.get("punch_in"))
    updated["punch_out"] = epoch_ms(updated.get("punch_out"))

    updated.setdefault("late_minutes", 0)
    updated.setdefault("overtime_minutes", 0)
    updated.setdefault("half_day", False)
    updated.setdefault("history", [])

    for entry in updated["history"]:
        if entry.get("at") is not None:
            entry["at"] = epoch_ms(entry["at"])

        for change in entry.get("changes", {}).values():
            for key in ("from", "to"):
                if isinstance(change.get(key), datetime):
                    change[key] = epoch_ms(change[key])

    return updated


def build_department_summary_pipeline(
    period_start: date,
    period_end: date,
    department: Optional[str] = None,
):
    month_end = period_end.isoformat()

    employee_match = {
        "joined_on": {"$lte": month_end}
    }

    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "employee_joined_on": "$joined_on",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                
"$and": [
    {
        "$eq": [
            "$emp_code",
            "$$employee_code",
        ]
    },
    {
        "$gte": [
            "$date",
            "$$employee_joined_on",
        ]
    },
    {
        "$gte": [
            "$date",
            period_start.isoformat(),
        ]
    },
    {
        "$lte": [
            "$date",
            month_end,
        ]
    },
]

                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_date_obj": {
                                "$dateFromString": {
                                    "dateString": "$date",
                                    "format": "%Y-%m-%d",
                                }
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_weekday": {
                                "$isoDayOfWeek": "$_date_obj"
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$lte": [
                                                        "$_weekday",
                                                        5,
                                                    ]
                                                },
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        {"$ifNull": ["$half_day", False]},
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$work_hours",
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        [
                                                            "PRESENT",
                                                            "WFH",
                                                            "ON_DUTY",
                                                        ],
                                                    ]
                                                },
                                                {
                                                    "$ne": [
                                                        "$work_hours",
                                                        None,
                                                    ]
                                                },
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {
                                    "$ifNull": [
                                        "$late_minutes",
                                        0,
                                    ]
                                }
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$status",
                                                "LEAVE",
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$status",
                                                "ON_DUTY",
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance_metrics",
            }
        },
        {
            "$addFields": {
                "_metrics": {
                    "$ifNull": [
                        {
                            "$arrayElemAt": [
                                "$attendance_metrics",
                                0,
                            ]
                        },
                        {
                            "present_days": 0,
                            "work_hours_sum": 0,
                            "work_hours_count": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                        },
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": "$_metrics.present_days"
                },
                "work_hours_sum": {
                    "$sum": "$_metrics.work_hours_sum"
                },
                "work_hours_count": {
                    "$sum": "$_metrics.work_hours_count"
                },
                "late_count": {
                    "$sum": "$_metrics.late_count"
                },
                "total_late_minutes": {
                    "$sum": "$_metrics.total_late_minutes"
                },
                "leave_count": {
                    "$sum": "$_metrics.leave_count"
                },
                "on_duty_count": {
                    "$sum": "$_metrics.on_duty_count"
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]
 
 
@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = None,
):
    period_start, period_end = validate_month(month)

    pipeline = build_department_summary_pipeline(
        period_start,
        period_end,
        department,
    )

    results = list(db.employees.aggregate(pipeline))

    items = []

    for result in results:
        work_hours_count = result["work_hours_count"]

        if work_hours_count == 0:
            avg_work_hours = None
        else:
            average = (
                Decimal(str(result["work_hours_sum"]))
                / Decimal(work_hours_count)
            )
            avg_work_hours = float(
                average.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

        items.append(
            {
                "department": result["_id"],
                "headcount": result["headcount"],
                "present_days": result["present_days"],
                "avg_work_hours": avg_work_hours,
                "late_count": result["late_count"],
                "total_late_minutes": result["total_late_minutes"],
                "leave_count": result["leave_count"],
                "on_duty_count": result["on_duty_count"],
            }
        )

    return {
        "month": month,
        "items": items,
    }


def build_late_leaderboard_pipeline(
    period_start: date,
    period_end: date,
    department: Optional[str] = None,
    limit: int = 10,
):
    employee_match = {}

    if department is not None:
        employee_match["department"] = department

    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code",
                        "employee_joined_on": "$joined_on",
                        },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            "$$employee_joined_on",
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            period_start.isoformat(),
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$date",
                                            period_end.isoformat(),
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "total_late_minutes": {
                                "$sum": {
                                    "$ifNull": [
                                        "$late_minutes",
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                 {"$ifNull": ["$late_minutes", 0]},
                                                    0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "late_metrics",
            }
        },
        {
            "$addFields": {
                "_metrics": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$late_metrics", 0]},
                        {
                            "total_late_minutes": 0,
                            "late_count": 0,
                        },
                    ]
                }
            }
        },
        {
            "$addFields": {
                "total_late_minutes": "$_metrics.total_late_minutes",
                "late_count": "$_metrics.late_count",
            }
        },
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1,
            }
        },
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    period_start, period_end = validate_month(month)

    pipeline = build_late_leaderboard_pipeline(
        period_start,
        period_end,
        department,
        limit,
    )

    items = list(db.employees.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


def get_department_trend_pipeline(department: str, from_date: date, to_date: date) -> list:
    start_datetime = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc)
    end_datetime = datetime.combine(to_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return [
        {"$documents": [{"date": start_datetime}]},
        {"$densify": {"field": "date", "range": {"step": 1, "unit": "day", "bounds": [start_datetime, end_datetime]}}},
        {"$addFields": {"_date_string": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date", "timezone": "UTC"}}, "_weekday": {"$isoDayOfWeek": "$date"}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_string": "$_date_string"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$day_string"]}]}}},
                    {"$project": {"_id": 0, "emp_code": 1}}
                ],
                "as": "_eligible_employees"
            }
        },
        {"$addFields": {"headcount": {"$size": "$_eligible_employees"}, "_eligible_codes": {"$map": {"input": "$_eligible_employees", "as": "employee", "in": "$$employee.emp_code"}}, "is_working_day": {"$lte": ["$_weekday", 5]}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day_string": "$_date_string", "eligible_codes": "$_eligible_codes"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [{"$eq": ["$date", "$$day_string"]}, {"$in": ["$emp_code", "$$eligible_codes"]}]}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_count": {"$sum": {"$cond": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},           
"late_count": {
    "$sum": {
        "$cond": [
            {
                "$gt": [
                    {"$ifNull": ["$late_minutes", 0]},
                    0
                ]
            },
            1,
            0
        ]
    }
}

                        }
                    }
                ],
                "as": "_daily_metrics"
            }
        },
        {"$addFields": {"_metrics": {"$ifNull": [{"$arrayElemAt": ["$_daily_metrics", 0]}, {"present_count": 0, "late_count": 0}]}}},
        {"$addFields": {"present_count": "$_metrics.present_count", "late_count": "$_metrics.late_count"}},
        {"$addFields": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$addFields": {"attendance_rate": {"$cond": [{"$eq": ["$attendance_rate", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$attendance_rate", 10000]}, 0.5]}}, 10000]}]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"_moving_avg_raw": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$addFields": {"moving_avg_7d": {"$cond": [{"$eq": ["$_moving_avg_raw", None]}, None, {"$divide": [{"$floor": {"$add": [{"$multiply": ["$_moving_avg_raw", 10000]}, 0.5]}}, 10000]}]}}},
        {"$sort": {"date": 1}},
        {"$project": {"_id": 0, "date": "$_date_string", "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    if (to_date - from_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    if db.employees.find_one(
        {"department": department},
        {"_id": 1},
    ) is None:
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    pipeline = get_department_trend_pipeline(
        department,
        from_date,
        to_date,
    )

    items = list(db.command("aggregate", 1, pipeline=pipeline)["cursor"]["firstBatch"])

    return {
        "department": department,
        "items": items,
    }

def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise HTTPException(
            status_code=422,
            detail="Month must use YYYY-MM format",
        )

    year, month = map(int, value.split("-"))
    start = date(year, month, 1)

    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    return start, end

def date_range_filter(start: date, end: date) -> dict:
    """Return an inclusive-start, exclusive-end ISO date string filter."""
    return {
        "$gte": start.isoformat(),
        "$lt": end.isoformat(),
    }



@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(
        ...,
        pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$",
    ),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    
    def explain_find(collection, query, sort):
        if not isinstance(sort, dict):
            raise HTTPException(
                status_code=500,
                detail="Internal error: sort must be a dictionary",
            )

        command = {
            "find": collection.name,
            "filter": query,
            "sort": sort,
            "skip": (page - 1) * page_size,
            "limit": page_size,
            "projection": {"_id": 0},
        }

        return db.command(
            "explain", 
            command,
            verbosity="executionStats",
        )


    def explain_aggregate(collection, pipeline):
        command = {
            "aggregate": collection.name,
            "pipeline": pipeline,
            "cursor": {},
        }
        return db.command(
            "explain",
            command,
            verbosity="executionStats",
        )

    collection_name = "attendance_logs"

    # 1. Attendance list
    if endpoint == "attendance_list":
        query = {}

        if emp_code is not None:
            query["emp_code"] = emp_code

        if date_from is not None:
            query.setdefault("date", {})["$gte"] = parse_date(
                date_from
            ).isoformat()

        if date_to is not None:
            query.setdefault("date", {})["$lte"] = parse_date(
                date_to
            ).isoformat()

        # if date_from and date_to and date_from > date_to:
        #     raise HTTPException(
        #         status_code=422,
        #         detail="date_from cannot be after date_to",
        #     )

        start = parse_date(date_from) if date_from is not None else None
        end = parse_date(date_to) if date_to is not None else None

        if start is not None and end is not None and start > end:
            raise HTTPException(
                status_code=422,
                detail="date_from must be on or before date_to",
            )
        
        if status is not None:
            if status not in {
                "PRESENT",
                "WFH",
                "ON_DUTY",
                "ABSENT",
                "LEAVE",
            }:
                raise HTTPException(
                    status_code=422,
                    detail="Invalid status",
                )
            query["status"] = status

        result = explain_find(
            db.attendance_logs,
            query,
            {"date": -1, "emp_code": 1},
        )

    # 2. Employee monthly analytics
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        employee = db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 0, "joined_on": 1},
        )

        if employee is None:
            raise HTTPException(
                status_code=404,
                detail="Employee not found",
            )

        period_start, period_end = validate_month(month)
        joined_on = date.fromisoformat(employee["joined_on"])
        effective_start = max(period_start, joined_on)

        pipeline = build_employee_monthly_pipeline(
            emp_code,
            effective_start,
            period_end,
        )

        result = explain_aggregate(
            db.attendance_logs,
            pipeline,
        )

    # 3. Department summary
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        period_start, period_end = validate_month(month)

        # This helper must contain the actual department-summary
        # aggregation pipeline used by the department summary route.
        pipeline = build_department_summary_pipeline(
            period_start,
            period_end,
            department,
        )

        result = explain_aggregate(
            db.employees,
            pipeline,
        )
        collection_name = "employees"

    # 4. Late leaderboard
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        period_start, period_end = validate_month(month)

        # This helper must contain the same aggregation pipeline
        # used by the actual late leaderboard route.
        pipeline = build_late_leaderboard_pipeline(
            period_start,
            period_end,
            department,
            limit,
        )

        result = explain_aggregate(
            db.employees,
            pipeline,
        )
        collection_name = "employees"

    # 5. Department trend
    else:
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from and to are required",
            )

        start = parse_date(from_date)
        end = parse_date(to_date)

        if end < start:
            raise HTTPException(
                status_code=422,
                detail="to cannot be before from",
            )

        if (end - start).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        if not db.employees.find_one(
            {"department": department},
            {"_id": 1},
        ):
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        pipeline = get_department_trend_pipeline(
            department,
            start,
            end,
        )

        # The trend pipeline starts with $documents, so its
        # aggregation command runs against the database.
        command = {
            "aggregate": 1,
            "pipeline": pipeline,
            "cursor": {},
        }

        result = db.command(
            "explain",
            command,
            verbosity="executionStats",
        )
        collection_name = "employees"

    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": result,
    }
