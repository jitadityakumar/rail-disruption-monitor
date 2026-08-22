import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from rollup import day_rollup_status, leg_rollup_status  # noqa: E402


# --- leg_rollup_status ----------------------------------------------------

def test_both_normal_is_agree_clear():
    assert leg_rollup_status("NORMAL", "NORMAL", maps_expected=True) == "agree_clear"


def test_both_disrupted_is_agree_disrupted():
    assert leg_rollup_status("DISRUPTED", "DISRUPTED", maps_expected=True) == "agree_disrupted"


def test_maps_normal_gtfs_disrupted_is_disagree_gtfs_flags():
    assert leg_rollup_status("NORMAL", "DISRUPTED", maps_expected=True) == "disagree_gtfs_flags"


def test_maps_disrupted_gtfs_normal_is_disagree_maps_flags():
    assert leg_rollup_status("DISRUPTED", "NORMAL", maps_expected=True) == "disagree_maps_flags"


def test_maps_only_normal_is_clear():
    assert leg_rollup_status("NORMAL", None, maps_expected=True) == "clear"


def test_maps_only_disrupted_is_disrupted():
    assert leg_rollup_status("DISRUPTED", None, maps_expected=True) == "disrupted"


def test_gtfs_only_normal_maps_expected_is_clear_gtfs_only():
    assert leg_rollup_status(None, "NORMAL", maps_expected=True) == "clear_gtfs_only"


def test_gtfs_only_normal_maps_not_expected_is_clear():
    assert leg_rollup_status(None, "NORMAL", maps_expected=False) == "clear"


def test_gtfs_only_disrupted_is_disrupted():
    assert leg_rollup_status(None, "DISRUPTED", maps_expected=True) == "disrupted"
    assert leg_rollup_status(None, "DISRUPTED", maps_expected=False) == "disrupted"


def test_maps_unknown_gtfs_normal_maps_expected_falls_back_to_clear_gtfs_only():
    assert leg_rollup_status("UNKNOWN", "NORMAL", maps_expected=True) == "clear_gtfs_only"


def test_maps_unknown_gtfs_disrupted_falls_back_to_disrupted():
    assert leg_rollup_status("UNKNOWN", "DISRUPTED", maps_expected=True) == "disrupted"


def test_gtfs_unknown_maps_normal_falls_back_to_clear():
    assert leg_rollup_status("NORMAL", "UNKNOWN", maps_expected=True) == "clear"


def test_gtfs_unknown_maps_disrupted_falls_back_to_disrupted():
    assert leg_rollup_status("DISRUPTED", "UNKNOWN", maps_expected=True) == "disrupted"


def test_both_explicitly_unknown_is_unknown():
    assert leg_rollup_status("UNKNOWN", "UNKNOWN", maps_expected=True) == "unknown"


def test_neither_present_is_unknown():
    assert leg_rollup_status(None, None, maps_expected=True) == "unknown"


# --- day_rollup_status ------------------------------------------------------

def test_day_disagree_outranks_disrupted_and_agree_disrupted():
    assert day_rollup_status(["disagree_gtfs_flags", "agree_disrupted", "disrupted"]) == "disagree"


def test_day_disrupted_leg_outranks_clear_gtfs_only():
    assert day_rollup_status(["disrupted", "clear_gtfs_only"]) == "disrupted"


def test_day_mixed_clear_and_clear_gtfs_only_is_clear_gtfs_only():
    assert day_rollup_status(["clear", "agree_clear", "clear_gtfs_only"]) == "clear_gtfs_only"


def test_day_all_agree_clear_is_clear():
    assert day_rollup_status(["clear", "agree_clear"]) == "clear"


def test_day_all_agree_disrupted_is_disrupted():
    assert day_rollup_status(["agree_disrupted", "disrupted"]) == "disrupted"


def test_day_empty_leg_list_is_unknown():
    assert day_rollup_status([]) == "unknown"
