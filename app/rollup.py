"""
Disagreement rollup logic (issue #17).

Combines per-leg, per-source scan_results status values (Maps/GTFS) into the
rollup values used by the calendar/kiosk views. Pure, DB-free — plain values
in, plain values out.
"""

from typing import Literal

LegStatus = Literal["NORMAL", "DISRUPTED", "UNKNOWN"]   # existing per-source status values
LegRollup = Literal[
    "agree_clear", "agree_disrupted",
    "disagree_gtfs_flags", "disagree_maps_flags",   # split so the modal can say which side flagged it
    "clear", "disrupted", "clear_gtfs_only", "unknown",
]
DayRollup = Literal["disagree", "disrupted", "clear_gtfs_only", "clear", "unknown"]
# "future"/"not_monitored" remain caller-decided tiers, added on top of this return value —
# not part of this function's own output space.

_KNOWN = {"NORMAL", "DISRUPTED"}


def leg_rollup_status(
    maps_status: LegStatus | None,
    gtfs_status: LegStatus | None,
    maps_expected: bool,
) -> LegRollup:
    """Combine one leg's Maps and GTFS scan_results.status into a single rollup value.

    `maps_status`/`gtfs_status` is None when no row exists for that source on this
    route/date/direction/leg (either it hasn't been scanned yet, or the route's scan_source
    never configured that source at all — both collapse to "not known" here).

    `maps_expected` must reflect the route's *configuration* (scan_source in
    ('maps', 'both')), not row presence — this is what makes `clear_gtfs_only` mean "Maps
    confirmation was expected and didn't happen" rather than "this route never uses Maps".
    """
    maps_known = maps_status in _KNOWN
    gtfs_known = gtfs_status in _KNOWN

    if maps_known and gtfs_known:
        if maps_status == "NORMAL" and gtfs_status == "NORMAL":
            return "agree_clear"
        if maps_status == "DISRUPTED" and gtfs_status == "DISRUPTED":
            return "agree_disrupted"
        return "disagree_gtfs_flags" if gtfs_status == "DISRUPTED" else "disagree_maps_flags"

    if maps_known:  # gtfs missing or UNKNOWN
        return "clear" if maps_status == "NORMAL" else "disrupted"

    if gtfs_known:  # maps missing or UNKNOWN
        if gtfs_status == "DISRUPTED":
            return "disrupted"
        return "clear_gtfs_only" if maps_expected else "clear"

    return "unknown"


_DISAGREE = {"disagree_gtfs_flags", "disagree_maps_flags"}
_DISRUPTED_TIER = {"disrupted", "agree_disrupted"}


def day_rollup_status(leg_rollups: list[LegRollup]) -> DayRollup:
    """Combine every scanned leg's rollup (both directions, both legs) for one calendar day
    into the single status the calendar cell renders. Caller passes `[]` only for a date that
    has no applicable legs on any source at all (see integration notes) — that returns
    "unknown" here, but the caller should usually treat a wholly-inapplicable date as
    "future"/"not_monitored" before ever calling this.
    """
    if not leg_rollups:
        return "unknown"
    if any(r in _DISAGREE for r in leg_rollups):
        return "disagree"
    if any(r in _DISRUPTED_TIER for r in leg_rollups):
        return "disrupted"
    if "clear_gtfs_only" in leg_rollups:
        # weakest-link: even one GTFS-only-confirmed leg means the day as a whole isn't
        # fully Maps-confirmed, regardless of how many other legs are "clear"/"agree_clear"
        return "clear_gtfs_only"
    if all(r in ("clear", "agree_clear") for r in leg_rollups):
        return "clear"
    return "unknown"
