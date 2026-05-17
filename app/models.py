from typing import Optional
from pydantic import BaseModel, field_validator


def _validate_scan_days(v: list[int]) -> list[int]:
    if not v:
        raise ValueError("scan_days must not be empty")
    for d in v:
        if d < 0 or d > 6:
            raise ValueError("scan_days must be integers 0 (Mon) – 6 (Sun)")
    return v


class RouteCreate(BaseModel):
    name: str = ""
    origin_crs: str
    change_crs: Optional[str] = None
    destination_crs: str
    scan_days: list[int]
    lookahead_weeks: int = 4
    threshold_pct: int = 20
    kiosk_visible: bool = True

    @field_validator("origin_crs", "destination_crs")
    @classmethod
    def upper_crs(cls, v):
        return v.upper()

    @field_validator("change_crs")
    @classmethod
    def upper_change_crs(cls, v):
        return v.upper() if v else v

    @field_validator("scan_days")
    @classmethod
    def validate_scan_days(cls, v):
        return _validate_scan_days(v)

    @field_validator("lookahead_weeks")
    @classmethod
    def validate_lookahead(cls, v):
        if v < 1:
            raise ValueError("lookahead_weeks must be at least 1")
        return v

    @field_validator("threshold_pct")
    @classmethod
    def validate_threshold(cls, v):
        if v <= 0:
            raise ValueError("threshold_pct must be greater than 0")
        return v


class RouteUpdate(BaseModel):
    name: Optional[str] = None
    scan_days: Optional[list[int]] = None
    lookahead_weeks: Optional[int] = None
    threshold_pct: Optional[int] = None
    kiosk_visible: Optional[bool] = None

    @field_validator("scan_days")
    @classmethod
    def validate_scan_days(cls, v):
        if v is None:
            return v
        return _validate_scan_days(v)

    @field_validator("lookahead_weeks")
    @classmethod
    def validate_lookahead(cls, v):
        if v is not None and v < 1:
            raise ValueError("lookahead_weeks must be at least 1")
        return v

    @field_validator("threshold_pct")
    @classmethod
    def validate_threshold(cls, v):
        if v is not None and v <= 0:
            raise ValueError("threshold_pct must be greater than 0")
        return v


class BaselineTrigger(BaseModel):
    baseline_date: str


class LegSelection(BaseModel):
    duration_s: Optional[int] = None
    steps: list = []
    arr_stop: Optional[str] = None


class BaselineConfirm(BaseModel):
    baseline_date: str
    outbound_leg1: LegSelection
    outbound_leg2: Optional[LegSelection] = None
    return_leg1: LegSelection
    return_leg2: Optional[LegSelection] = None
