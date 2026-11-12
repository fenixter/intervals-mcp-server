"""
Tests for event time/category/partial-update support, richer and compact get_events
output, weekly targets, bulk event creation, athlete sport settings, and the
activity formatting / update / delete tools.
"""

import asyncio
import os
import pathlib
import sys
from typing import Any

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.server import (  # pylint: disable=wrong-import-position
    add_events_bulk,
    add_or_update_event,
    delete_activity,
    get_athlete_settings,
    get_events,
    update_activity,
    update_sport_settings,
)
from intervals_mcp_server.tools.events import EventSpec  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.formatting import (  # pylint: disable=wrong-import-position
    format_activity_summary,
    format_event_compact,
    format_event_summary,
)
from intervals_mcp_server.utils.types import Step, WorkoutDoc  # pylint: disable=wrong-import-position
from intervals_mcp_server.utils.validation import (  # pylint: disable=wrong-import-position
    parse_local_datetime,
)


class FakeApi:
    """Records requests and replies from a queue (or a callable) of responses."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        response = self.responses.pop(0) if self.responses else {}
        return response(kwargs) if callable(response) else response


def _patch(monkeypatch, module: str, api: FakeApi) -> FakeApi:
    monkeypatch.setattr(f"intervals_mcp_server.tools.{module}.make_intervals_request", api)
    return api


def _echo(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {"id": 77, **kwargs["data"]}


FREERIDE_30 = WorkoutDoc(steps=[Step(freeride=True, duration=1800)])

STORED_RACE = {
    "id": 77,
    "start_date_local": "2026-12-30T20:00:00",
    "category": "RACE_B",
    "name": "TEST race",
    "type": "VirtualRide",
    "description": "- 30m freeride\n",
    "moving_time": 1800,
    "icu_training_load": 40,
    "workout_doc": {"steps": [{"freeride": True, "duration": 1800}]},
}


# --- 1. start date with time -------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-11-17", ("2026-11-17T00:00:00", False)),
        ("2026-11-17T19:35:00", ("2026-11-17T19:35:00", True)),
        ("2026-11-17T19:35", ("2026-11-17T19:35:00", True)),
        ("2026-11-17 19:35", ("2026-11-17T19:35:00", True)),
    ],
)
def test_parse_local_datetime_accepts_date_and_datetime(value, expected):
    assert parse_local_datetime(value) == expected


@pytest.mark.parametrize("value", ["17.11.2026", "2026-11-17T19:35:00Z", "2026-11-17T25:00"])
def test_parse_local_datetime_rejects_bad_values(value):
    with pytest.raises(ValueError):
        parse_local_datetime(value)


def test_create_event_with_time_sends_start_date_local(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi(_echo))
    result = asyncio.run(
        add_or_update_event(
            workout_type="VirtualRide", name="Evening", start_date="2026-11-17T19:35:00"
        )
    )
    assert "Successfully created event id: 77" in result
    assert api.calls[0]["method"] == "POST"
    assert api.calls[0]["data"]["start_date_local"] == "2026-11-17T19:35:00"


def test_create_event_date_only_keeps_midnight(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi(_echo))
    asyncio.run(add_or_update_event(workout_type="Ride", name="Old style", start_date="2026-11-17"))
    assert api.calls[0]["data"]["start_date_local"] == "2026-11-17T00:00:00"
    assert api.calls[0]["data"]["category"] == "WORKOUT"


def test_create_event_invalid_date_returns_error(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi())
    result = asyncio.run(add_or_update_event(workout_type="Ride", name="X", start_date="17/11/2026"))
    assert result.startswith("Error: Invalid date format")
    assert not api.calls


# --- 2. category -------------------------------------------------------------


def test_create_race_with_category(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi(_echo))
    result = asyncio.run(
        add_or_update_event(
            name="TEST race",
            category="race_b",
            start_date="2026-12-30T20:00:00",
            workout_type="VirtualRide",
            workout_doc=FREERIDE_30,
        )
    )
    sent = api.calls[0]["data"]
    assert sent["category"] == "RACE_B"
    assert sent["type"] == "VirtualRide"
    assert "freeride" in sent["description"]
    assert "Category: RACE_B" in result


def test_invalid_category_is_rejected(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi())
    result = asyncio.run(add_or_update_event(workout_type="Ride", name="X", category="RACE_D"))
    assert "Invalid category 'RACE_D'" in result
    assert "RACE_A" in result
    assert not api.calls


def test_create_without_name_is_rejected(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi())
    result = asyncio.run(add_or_update_event(workout_type="Ride", start_date="2026-12-01"))
    assert "name is required" in result
    assert not api.calls


# --- 3. richer get_events output ---------------------------------------------


def test_event_summary_shows_category_type_time_load():
    text = format_event_summary(STORED_RACE)
    assert "Date: 2026-12-30T20:00:00" in text
    assert "Category: RACE_B" in text
    assert "Type: VirtualRide" in text
    assert "Moving Time: 1800 seconds (0:30:00)" in text
    assert "Planned Load: 40" in text
    assert "Type: Other" not in text


def test_event_summary_shows_weekly_targets():
    target = {
        "id": 9,
        "start_date_local": "2026-12-28T00:00:00",
        "category": "TARGET",
        "type": "Ride",
        "name": "Weekly",
        "load_target": 450,
        "time_target": 36000,
        "distance_target": 250000.0,
    }
    text = format_event_summary(target)
    assert "Load Target: 450" in text
    assert "Time Target: 36000 seconds (10:00:00)" in text
    assert "Distance Target: 250000.0 meters" in text


def test_get_events_passes_category_filter(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi([STORED_RACE]))
    result = asyncio.run(
        get_events(start_date="2026-12-30", end_date="2026-12-30", category="race_b,race_a")
    )
    assert api.calls[0]["params"]["category"] == "RACE_B,RACE_A"
    assert "Category: RACE_B" in result


# --- 4. compact mode ---------------------------------------------------------


def test_compact_line_has_no_description():
    line = format_event_compact(STORED_RACE)
    assert line == "2026-12-30 20:00 | RACE_B | VirtualRide | TEST race | 0:30:00 | load 40 | id 77"


def test_get_events_compact_is_small(monkeypatch):
    long_description = "- 10m 65% \n" * 200
    events = [
        {**STORED_RACE, "id": i, "description": long_description, "category": "WORKOUT"}
        for i in range(150)
    ]
    _patch(monkeypatch, "events", FakeApi(events))
    full = asyncio.run(get_events(start_date="2026-11-13", end_date="2026-12-23"))
    _patch(monkeypatch, "events", FakeApi(events))
    compact = asyncio.run(get_events(start_date="2026-11-13", end_date="2026-12-23", compact=True))
    assert len(compact.splitlines()) == 151
    assert "65%" not in compact
    assert len(compact) < len(full) / 20


def test_get_events_without_description(monkeypatch):
    _patch(monkeypatch, "events", FakeApi([STORED_RACE]))
    result = asyncio.run(get_events(include_description=False))
    assert "Description:" not in result
    assert "Name: TEST race" in result


# --- 5. partial update -------------------------------------------------------


def test_update_with_only_start_date_keeps_other_fields(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi(dict(STORED_RACE), _echo))
    result = asyncio.run(add_or_update_event(event_id="77", start_date="2026-12-31T19:30:00"))
    get_call, put_call = api.calls
    assert get_call["url"] == "/athlete/i1/events/77"
    assert put_call["method"] == "PUT"
    assert put_call["url"] == "/athlete/i1/events/77"
    sent = put_call["data"]
    assert sent["start_date_local"] == "2026-12-31T19:30:00"
    assert sent["name"] == "TEST race"
    assert sent["category"] == "RACE_B"
    assert sent["type"] == "VirtualRide"
    assert sent["description"] == STORED_RACE["description"]
    assert "workout_doc" not in sent
    assert "Successfully updated event id: 77" in result


def test_update_with_date_only_keeps_time_of_day(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi(dict(STORED_RACE), _echo))
    asyncio.run(add_or_update_event(event_id="77", start_date="2027-01-05"))
    assert api.calls[1]["data"]["start_date_local"] == "2027-01-05T20:00:00"


def test_update_overrides_only_passed_fields(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi(dict(STORED_RACE), _echo))
    asyncio.run(add_or_update_event(event_id="77", name="Renamed", category="RACE_A"))
    sent = api.calls[1]["data"]
    assert sent["name"] == "Renamed"
    assert sent["category"] == "RACE_A"
    assert sent["start_date_local"] == "2026-12-30T20:00:00"


def test_update_reports_missing_event(monkeypatch):
    api = _patch(
        monkeypatch, "events", FakeApi({"error": True, "message": "404 Not Found"})
    )
    result = asyncio.run(add_or_update_event(event_id="404", start_date="2026-12-31"))
    assert "Error reading event 404" in result
    assert len(api.calls) == 1


def test_legacy_positional_call_still_works(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi(_echo))
    result = asyncio.run(add_or_update_event("Run", "Easy run", None, None, None, "2026-11-20"))
    assert "Successfully created event" in result
    assert api.calls[0]["data"]["type"] == "Run"


# --- 6. athlete settings -----------------------------------------------------

ATHLETE = {
    "id": "i1",
    "icu_weight": 72.5,
    "icu_resting_hr": 48,
    "timezone": "Europe/Kyiv",
    "sportSettings": [
        {
            "id": 1,
            "types": ["Ride", "VirtualRide"],
            "ftp": 280,
            "indoor_ftp": 270,
            "lthr": 165,
            "max_hr": 188,
            "power_zones": [55, 75, 90, 105, 120, 150, 999],
            "hr_zones": [135, 150, 160, 170, 188],
        },
        {"id": 2, "types": ["Run"], "lthr": 172, "max_hr": 192, "threshold_pace": 4.2},
    ],
}


def test_get_athlete_settings_shows_ftp_and_zones(monkeypatch):
    api = _patch(monkeypatch, "athlete", FakeApi(ATHLETE))
    result = asyncio.run(get_athlete_settings())
    assert api.calls[0]["url"] == "/athlete/i1"
    assert "Weight: 72.5 kg" in result
    assert "Ride, VirtualRide" in result
    assert "FTP: 280 W" in result
    assert "LTHR: 165 bpm" in result
    assert "Max HR: 188 bpm" in result
    assert "Z1 0-55% (0-154 W)" in result
    assert "Z7 150+% (420+ W)" in result
    assert "Z5 170-188 bpm" in result
    assert "Run" in result


def test_get_athlete_settings_filter_by_sport(monkeypatch):
    _patch(monkeypatch, "athlete", FakeApi(ATHLETE))
    result = asyncio.run(get_athlete_settings(sport="virtualride"))
    assert "FTP: 280 W" in result
    assert "Threshold pace" not in result


def test_update_sport_settings_changes_only_passed_values(monkeypatch):
    api = _patch(
        monkeypatch,
        "athlete",
        FakeApi(ATHLETE["sportSettings"][0], {**ATHLETE["sportSettings"][0], "ftp": 290}),
    )
    result = asyncio.run(update_sport_settings(sport="Ride", ftp=290))
    get_call, put_call = api.calls
    assert get_call["url"] == "/athlete/i1/sport-settings/Ride"
    assert put_call["method"] == "PUT"
    assert put_call["url"] == "/athlete/i1/sport-settings/Ride"
    assert put_call["data"] == {"ftp": 290}
    assert put_call["params"] == {"recalcHrZones": "false"}
    assert "ftp: 280 -> 290" in result


def test_update_sport_settings_requires_a_value(monkeypatch):
    api = _patch(monkeypatch, "athlete", FakeApi())
    assert "at least one of" in asyncio.run(update_sport_settings(sport="Ride"))
    assert "positive" in asyncio.run(update_sport_settings(sport="Ride", lthr=0))
    assert not api.calls


# --- 7. weekly targets -------------------------------------------------------


def test_create_weekly_target(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi(_echo))
    result = asyncio.run(
        add_or_update_event(
            category="TARGET",
            start_date="2026-12-28",
            workout_type="Ride",
            name="Weekly",
            load_target=450,
            time_target=36000,
        )
    )
    sent = api.calls[0]["data"]
    assert sent["category"] == "TARGET"
    assert sent["load_target"] == 450
    assert sent["time_target"] == 36000
    assert "Load Target: 450" in result


def test_update_weekly_target_load(monkeypatch):
    stored = {
        "id": 5,
        "category": "TARGET",
        "type": "Ride",
        "name": "Weekly",
        "start_date_local": "2026-12-28T00:00:00",
        "load_target": 450,
    }
    api = _patch(monkeypatch, "events", FakeApi(stored, _echo))
    asyncio.run(add_or_update_event(event_id="5", load_target=500))
    sent = api.calls[1]["data"]
    assert sent["load_target"] == 500
    assert sent["category"] == "TARGET"


# --- 8. activity formatting --------------------------------------------------


def test_activity_work_is_in_kilojoules_and_time_is_local():
    text = format_activity_summary(
        {
            "name": "ITT",
            "icu_joules": 506851,
            "start_date_local": "2026-11-08T07:30:00",
            "start_date": "2026-11-08T05:30:00Z",
            "timezone": "Europe/Kyiv",
        }
    )
    assert "Work: 506.9 kJ" in text
    assert "Kilojoules: 506851" not in text
    assert "Date: 2026-11-08 07:30:00 (local time, Europe/Kyiv)" in text


def test_activity_time_without_local_is_marked_utc():
    text = format_activity_summary({"name": "X", "startTime": "2024-01-01T08:00:00Z"})
    assert "Date: 2024-01-01 08:00:00 UTC" in text
    assert "Work: N/A" in text


# --- 9. bulk events ----------------------------------------------------------


def test_add_events_bulk_posts_one_request(monkeypatch):
    def reply(kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"id": i, **e} for i, e in enumerate(kwargs["data"], 1)]

    api = _patch(monkeypatch, "events", FakeApi(reply))
    result = asyncio.run(
        add_events_bulk(
            events=[
                EventSpec(name="Endurance", start_date="2026-12-01T18:00", workout_type="VirtualRide",
                          workout_doc=FREERIDE_30),
                {"name": "Weekly", "start_date": "2026-11-30", "category": "TARGET",  # type: ignore[list-item]
                 "workout_type": "Ride", "load_target": 450},
            ]
        )
    )
    assert len(api.calls) == 1
    assert api.calls[0]["url"] == "/athlete/i1/events/bulk"
    assert api.calls[0]["method"] == "POST"
    first, second = api.calls[0]["data"]
    assert first["start_date_local"] == "2026-12-01T18:00:00"
    assert "freeride" in first["description"]
    assert second["category"] == "TARGET"
    assert "Created 2 of 2 events" in result


def test_add_events_bulk_validates_before_sending(monkeypatch):
    api = _patch(monkeypatch, "events", FakeApi())
    result = asyncio.run(
        add_events_bulk(
            events=[
                EventSpec(name="Ok", start_date="2026-12-01"),
                EventSpec(name="Bad", start_date="tomorrow"),
            ]
        )
    )
    assert result.startswith("Error in event #2")
    assert not api.calls


# --- 10. activity update / delete -------------------------------------------


def test_update_activity_sends_only_passed_fields(monkeypatch):
    api = _patch(monkeypatch, "activities", FakeApi({"id": "i9", "name": "ITT", "race": True}))
    result = asyncio.run(update_activity("i9", name="ITT", race=True))
    assert api.calls[0]["url"] == "/activity/i9"
    assert api.calls[0]["method"] == "PUT"
    assert api.calls[0]["data"] == {"name": "ITT", "race": True}
    assert "Successfully updated activity i9" in result


def test_update_activity_requires_a_field(monkeypatch):
    api = _patch(monkeypatch, "activities", FakeApi())
    assert "at least one" in asyncio.run(update_activity("i9"))
    assert not api.calls


def test_delete_activity(monkeypatch):
    api = _patch(monkeypatch, "activities", FakeApi({"id": "i9"}))
    result = asyncio.run(delete_activity("i9"))
    assert api.calls[0]["method"] == "DELETE"
    assert api.calls[0]["url"] == "/activity/i9"
    assert "Successfully deleted activity i9" in result
