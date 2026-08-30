from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _validate_scan_days(v: list[int]) -> list[int]:
    if not v:
        raise ValueError("scan_days must not be empty")
    for d in v:
        if d < 0 or d > 6:
            raise ValueError("scan_days must be integers 0 (Mon) – 6 (Sun)")
    return v


KioskColor = Literal["blue", "yellow", "green", "purple", "orange", "teal"]


class StopPoint(BaseModel):
    id: str
    name: str

    @field_validator("id")
    @classmethod
    def reject_hub(cls, v):
        if v.upper().startswith("HUB"):
            raise ValueError(
                f"StopPoint id {v!r} is a multi-modal HUB group, not a concrete station -- "
                "TfL's JourneyResults rejects HUB ids outright (HTTP 300). Pick a specific "
                "child station from the search results instead."
            )
        return v


class RouteCreate(BaseModel):
    name: str = ""
    origin: StopPoint
    destination: StopPoint
    scan_days: list[int]
    lookahead_weeks: int = 4
    threshold_pct: int = 20
    kiosk_visible: bool = True
    kiosk_color: KioskColor = "blue"

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
    kiosk_color: Optional[KioskColor] = None

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


class ItineraryChoice(BaseModel):
    duration_s: int
    interchange_stops: list[str]
    leg_modes: list[str]
    steps: list


class BaselineConfirm(BaseModel):
    baseline_date: str
    outbound: ItineraryChoice
    return_: ItineraryChoice = Field(alias="return")
    model_config = {"populate_by_name": True}
