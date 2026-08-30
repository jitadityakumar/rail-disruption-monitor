import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _validate_hhmm(v: str) -> str:
    if not _TIME_RE.match(v):
        raise ValueError("time must be in HH:MM 24-hour format")
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
    departure_time: str = "08:00"
    return_time: str = "18:00"
    threshold_pct: int = 20
    kiosk_visible: bool = True
    kiosk_color: KioskColor = "blue"

    @field_validator("departure_time", "return_time")
    @classmethod
    def validate_time(cls, v):
        return _validate_hhmm(v)

    @field_validator("threshold_pct")
    @classmethod
    def validate_threshold(cls, v):
        if v <= 0:
            raise ValueError("threshold_pct must be greater than 0")
        return v


class RouteUpdate(BaseModel):
    name: Optional[str] = None
    departure_time: Optional[str] = None
    return_time: Optional[str] = None
    threshold_pct: Optional[int] = None
    kiosk_visible: Optional[bool] = None
    kiosk_color: Optional[KioskColor] = None

    @field_validator("departure_time", "return_time")
    @classmethod
    def validate_time(cls, v):
        if v is None:
            return v
        return _validate_hhmm(v)

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
