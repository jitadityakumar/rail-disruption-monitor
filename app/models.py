from typing import Optional
from pydantic import BaseModel, field_validator


class RouteCreate(BaseModel):
    name: str
    crs_sequence: list[str]
    scan_days: list[int]
    lookahead_weeks: int = 4
    threshold_pct: int = 20
    kiosk_visible: bool = True

    @field_validator("crs_sequence")
    @classmethod
    def validate_min_length(cls, v):
        if len(v) < 2:
            raise ValueError("crs_sequence must have at least 2 stations")
        return [c.upper() for c in v]

    @field_validator("scan_days")
    @classmethod
    def validate_scan_days(cls, v):
        for d in v:
            if d < 0 or d > 6:
                raise ValueError("scan_days must be integers 0 (Mon) – 6 (Sun)")
        return v


class RouteUpdate(BaseModel):
    name: Optional[str] = None
    scan_days: Optional[list[int]] = None
    lookahead_weeks: Optional[int] = None
    threshold_pct: Optional[int] = None
    kiosk_visible: Optional[bool] = None


class BaselineTrigger(BaseModel):
    baseline_date: str


class SlotSelection(BaseModel):
    duration_s: Optional[int] = None
    steps: list = []


class BaselineConfirm(BaseModel):
    baseline_date: str
    selections: dict[str, SlotSelection]


class RouteOut(BaseModel):
    id: int
    name: str
    crs_sequence: list[str]
    scan_days: list[int]
    lookahead_weeks: int
    threshold_pct: int
    kiosk_visible: bool
    last_scanned_at: Optional[str]
    created_at: str
    has_baseline: bool = False
