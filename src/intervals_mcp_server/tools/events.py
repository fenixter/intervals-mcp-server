"""
Event-related MCP tools for Intervals.icu.

This module contains tools for retrieving, creating, updating, and deleting athlete events.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import TypeAdapter

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.dates import get_default_end_date, get_default_future_end_date
from intervals_mcp_server.utils.formatting import (
    format_event_compact,
    format_event_details,
    format_event_summary,
)
from intervals_mcp_server.utils.types import WorkoutDoc
from intervals_mcp_server.utils.validation import (
    parse_local_datetime,
    resolve_activity_type,
    resolve_athlete_id,
    validate_date,
)

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import mcp  # noqa: F401

config = get_config()


EVENT_CATEGORIES: tuple[str, ...] = (
    "WORKOUT",
    "RACE_A",
    "RACE_B",
    "RACE_C",
    "NOTE",
    "PLAN",
    "HOLIDAY",
    "SICK",
    "INJURED",
    "SET_EFTP",
    "FITNESS_DAYS",
    "SEASON_START",
    "TARGET",
    "SET_FITNESS",
)
"""Event categories accepted by the Intervals.icu API (EventEx.category enum)."""

# Writable event fields carried over from the stored event when updating, so that
# a partial update (e.g. only a new start_date) keeps everything else intact.
_MERGE_FIELDS: tuple[str, ...] = (
    "start_date_local",
    "category",
    "name",
    "type",
    "description",
    "moving_time",
    "distance",
    "load_target",
    "time_target",
    "distance_target",
    "indoor",
    "color",
)


@dataclass
class EventSpec:  # pylint: disable=too-many-instance-attributes
    """One calendar event for add_events_bulk (same meaning as add_or_update_event args)."""

    name: str
    start_date: str
    workout_type: str | None = None
    category: str | None = None
    workout_doc: WorkoutDoc | None = None
    moving_time: int | None = None
    distance: int | None = None
    load_target: int | None = None
    time_target: int | None = None
    distance_target: float | None = None


def _normalize_category(category: str | None) -> str | None:
    """Upper-case and validate an event category (None stays None)."""
    if category is None:
        return None
    normalized = category.strip().upper()
    if normalized not in EVENT_CATEGORIES:
        raise ValueError(
            f"Invalid category '{category}'. Allowed: {', '.join(EVENT_CATEGORIES)}."
        )
    return normalized


def _prepare_event_data(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    name: str | None,
    workout_type: str | None,
    start_date: str,
    workout_doc: WorkoutDoc | None,
    moving_time: int | None,
    distance: int | None,
    category: str | None = None,
    load_target: int | None = None,
    time_target: int | None = None,
    distance_target: float | None = None,
) -> dict[str, Any]:
    """Prepare the payload for creating an event.

    ``start_date`` may be a date or a local date-time (see parse_local_datetime).
    Many arguments are required to match the Intervals.icu API event structure.
    """
    start_date_local, _ = parse_local_datetime(start_date)
    resolved_category = _normalize_category(category) or "WORKOUT"
    if not name and resolved_category != "TARGET":
        raise ValueError("name is required when creating an event.")
    data: dict[str, Any] = {
        "start_date_local": start_date_local,
        "category": resolved_category,
        "name": name,
        "description": str(workout_doc) if workout_doc else None,
        "type": resolve_activity_type(name, workout_type),
        "moving_time": moving_time,
        "distance": distance,
        "load_target": load_target,
        "time_target": time_target,
        "distance_target": distance_target,
    }
    return {key: value for key, value in data.items() if value is not None}


def _merge_event_update(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    existing: dict[str, Any],
    name: str | None,
    workout_type: str | None,
    start_date: str | None,
    workout_doc: WorkoutDoc | None,
    moving_time: int | None,
    distance: int | None,
    category: str | None = None,
    load_target: int | None = None,
    time_target: int | None = None,
    distance_target: float | None = None,
) -> dict[str, Any]:
    """Merge only the provided fields onto the stored event.

    A date-only ``start_date`` keeps the stored time of day, so moving a workout
    to another day does not reset its start time.
    """
    data = {key: existing[key] for key in _MERGE_FIELDS if existing.get(key) is not None}
    if start_date:
        start_date_local, has_time = parse_local_datetime(start_date)
        old = str(existing.get("start_date_local") or "")
        if not has_time and len(old) >= 19:
            start_date_local = start_date_local[:10] + old[10:19]
        data["start_date_local"] = start_date_local
    updates: dict[str, Any] = {
        "name": name,
        "type": workout_type,
        "category": _normalize_category(category),
        "description": str(workout_doc) if workout_doc else None,
        "moving_time": moving_time,
        "distance": distance,
        "load_target": load_target,
        "time_target": time_target,
        "distance_target": distance_target,
    }
    data.update({key: value for key, value in updates.items() if value is not None})
    return data


def _handle_event_response(
    result: dict[str, Any] | list[dict[str, Any]] | None,
    action: str,
    athlete_id: str,
    start_date: str,
) -> str:
    """Handle API response and format appropriate message."""
    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error {action} event: {error_message}"
    if not result:
        return f"No events {action} for athlete {athlete_id}."
    if isinstance(result, dict):
        return f"Successfully {action} event id: {result.get('id')}\n" + format_event_summary(
            result, include_description=False
        )
    return f"Event {action} successfully at {start_date}"


async def _delete_events_list(
    athlete_id: str, api_key: str | None, events: list[dict[str, Any]]
) -> list[int | str | None]:
    """Delete a list of events and return IDs of failed deletions.

    Args:
        athlete_id: The athlete ID.
        api_key: Optional API key.
        events: List of event dictionaries to delete.

    Returns:
        List of event IDs that failed to delete.
    """
    failed_events: list[int | str | None] = []
    for event in events:
        result = await make_intervals_request(
            url=f"/athlete/{athlete_id}/events/{event.get('id')}",
            api_key=api_key,
            method="DELETE",
        )
        if isinstance(result, dict) and "error" in result:
            failed_events.append(event.get("id"))
    return failed_events


@mcp.tool()
async def get_events(
    athlete_id: str | None = None,
    api_key: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    compact: bool = False,
    include_description: bool = True,
    category: str | None = None,
) -> str:
    """Get calendar events (planned workouts, races, notes, weekly targets...) from Intervals.icu

    Each event shows its date/start time (athlete's local time), ID, category, sport type,
    name, planned moving time, distance and load (icu_training_load), and for weekly
    targets (category TARGET) the load_target / time_target / distance_target.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        start_date: Start date in YYYY-MM-DD format (optional, defaults to today)
        end_date: End date in YYYY-MM-DD format (optional, defaults to 30 days from today)
        compact: If True, return one line per event ("date time | category | type | name |
            duration | load | targets | id") without descriptions or workout steps.
            Use this for long date ranges (several weeks or months). Defaults to False.
        include_description: If False, omit event descriptions/workout steps in the full
            format. Ignored when compact=True. Defaults to True.
        category: Optional category filter, e.g. "WORKOUT", "RACE_A", "TARGET", or a
            comma-separated list such as "RACE_A,RACE_B,RACE_C".
    """
    # Resolve athlete ID
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    # Parse date parameters (events use different defaults)
    if not start_date:
        start_date = get_default_end_date()
    if not end_date:
        end_date = get_default_future_end_date()

    # Call the Intervals.icu API
    params = {"oldest": start_date, "newest": end_date}
    if category:
        try:
            params["category"] = ",".join(
                str(_normalize_category(c)) for c in category.split(",") if c.strip()
            )
        except ValueError as e:
            return f"Error: {e}"

    result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/events", api_key=api_key, params=params
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching events: {error_message}"

    # Format the response
    if not result:
        return f"No events found for athlete {athlete_id_to_use} in the specified date range."

    # Ensure result is a list
    events = result if isinstance(result, list) else []

    if not events:
        return f"No events found for athlete {athlete_id_to_use} in the specified date range."

    event_dicts = [event for event in events if isinstance(event, dict)]
    if compact:
        return f"Events ({len(event_dicts)}):\n" + "\n".join(
            format_event_compact(event) for event in event_dicts
        )

    events_summary = "Events:\n\n"
    for event in event_dicts:
        events_summary += format_event_summary(event, include_description) + "\n\n"

    return events_summary


@mcp.tool()
async def get_event_by_id(
    event_id: str,
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> str:
    """Get detailed information for a specific event from Intervals.icu

    Args:
        event_id: The Intervals.icu event ID
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
    """
    # Resolve athlete ID
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    # Call the Intervals.icu API
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/events/{event_id}", api_key=api_key
    )

    if isinstance(result, dict) and "error" in result:
        error_message = result.get("message", "Unknown error")
        return f"Error fetching event details: {error_message}"

    # Format the response
    if not result:
        return f"No details found for event {event_id}."

    if not isinstance(result, dict):
        return f"Invalid event format for event {event_id}."

    return format_event_details(result)


@mcp.tool()
async def delete_event(
    event_id: str,
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> str:
    """Delete event for an athlete from Intervals.icu
    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        event_id: The Intervals.icu event ID
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not event_id:
        return "Error: No event ID provided."
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/events/{event_id}", api_key=api_key, method="DELETE"
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error deleting event: {result.get('message')}"
    return json.dumps(result, indent=2)


async def _fetch_events_for_deletion(
    athlete_id: str, api_key: str | None, start_date: str, end_date: str
) -> tuple[list[dict[str, Any]], str | None]:
    """Fetch events for deletion and return them with any error message.

    Args:
        athlete_id: The athlete ID.
        api_key: Optional API key.
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.

    Returns:
        Tuple of (events_list, error_message). error_message is None if successful.
    """
    params = {"oldest": validate_date(start_date), "newest": validate_date(end_date)}
    result = await make_intervals_request(
        url=f"/athlete/{athlete_id}/events", api_key=api_key, params=params
    )
    if isinstance(result, dict) and "error" in result:
        return [], f"Error deleting events: {result.get('message')}"
    events = result if isinstance(result, list) else []
    return events, None


@mcp.tool()
async def delete_events_by_date_range(
    start_date: str,
    end_date: str,
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> str:
    """Delete events for an athlete from Intervals.icu in the specified date range.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    events, error_msg = await _fetch_events_for_deletion(
        athlete_id_to_use, api_key, start_date, end_date
    )
    if error_msg:
        return error_msg

    failed_events = await _delete_events_list(athlete_id_to_use, api_key, events)
    deleted_count = len(events) - len(failed_events)
    return f"Deleted {deleted_count} events. Failed to delete {len(failed_events)} events: {failed_events}"


@mcp.tool()
async def add_or_update_event(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    workout_type: str | None = None,
    name: str | None = None,
    athlete_id: str | None = None,
    api_key: str | None = None,
    event_id: str | None = None,
    start_date: str | None = None,
    workout_doc: WorkoutDoc | None = None,
    moving_time: int | None = None,
    distance: int | None = None,
    category: str | None = None,
    load_target: int | None = None,
    time_target: int | None = None,
    distance_target: float | None = None,
) -> str:
    """Create or update a calendar event (planned workout, race, weekly target...) on Intervals.icu.

    Create (no event_id): name is required (except for category TARGET); workout_type
    defaults to a type guessed from the name (fallback Ride) and category to WORKOUT.
    Update (event_id given): the stored event is read first and ONLY the arguments you pass
    are changed; name, workout_type, category, steps etc. are kept. E.g. to move a workout
    pass just event_id and start_date.

    Many arguments are required as this MCP tool function maps directly to the Intervals.icu API parameters.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        event_id: The Intervals.icu event ID to update (optional; omit to create a new event)
        start_date: Date "YYYY-MM-DD" or local date-time "YYYY-MM-DDTHH:MM[:SS]" in the
            athlete's local time (no timezone suffix). Optional, defaults to today when creating.
            A date without time creates the event at 00:00 (no specific time); when updating,
            a date without time keeps the event's existing time of day.
        name: Name of the event (required when creating, optional when updating)
        workout_doc: steps as a list of Step objects (optional, but necessary to define workout steps)
        workout_type: Sport type, e.g. Ride, VirtualRide, Run, Swim, Walk, Row (optional)
        moving_time: Total expected moving time of the workout in seconds (optional)
        distance: Total expected distance of the workout in meters (optional)
        category: Event category (optional, defaults to WORKOUT on create). One of:
            WORKOUT, RACE_A, RACE_B, RACE_C (race priorities), NOTE, PLAN, HOLIDAY, SICK,
            INJURED, SET_EFTP, FITNESS_DAYS, SEASON_START, TARGET (weekly target), SET_FITNESS.
        load_target: Training load target, for category TARGET (optional)
        time_target: Moving time target in seconds, for category TARGET (optional)
        distance_target: Distance target in meters, for category TARGET (optional)

    Example - B race at 20:00 local time:
        {"name": "Club TT", "category": "RACE_B", "start_date": "2026-12-30T20:00:00",
         "workout_type": "VirtualRide",
         "workout_doc": {"steps": [{"freeride": true, "duration": 1800}]}}

    Example - move an existing event to another day/time (everything else kept):
        {"event_id": "12345", "start_date": "2026-12-31T19:30:00"}

    Example - weekly target (put it on the Monday of the week):
        {"category": "TARGET", "start_date": "2026-12-28", "workout_type": "Ride",
         "name": "Weekly", "load_target": 450, "time_target": 36000}

    Example workout_doc:
        "workout_doc": {
            "description": "High-intensity workout for increasing VO2 max",
            "steps": [
                {"power": {"value": 80, "units": "%ftp"}, "duration": 900, "warmup": true},
                {"reps": 2, "text": "High-intensity intervals", "steps": [
                    {"power": {"value": 110, "units": "%ftp"}, "distance": 500, "text": "High-intensity"},
                    {"power": {"value": 80, "units": "%ftp"}, "duration": 90, "text": "Recovery"}
                ]},
                {"power": {"value": 80, "units": "%ftp"}, "duration": 600, "cooldown": true},
                {"text": ""}
            ]
        }

    Step properties:
        distance: Distance of step in meters
            {"distance": 5000}
        duration: Duration of step in seconds
            {"duration": 1800}
        power/hr/pace/cadence: Define step intensity
            Percentage of FTP: {"power": {"value": 80, "units": "%ftp"}}
            Absolute power: {"power": {"value": 200, "units": "w"}}
            Heart rate: {"hr": {"value": 75, "units": "%hr"}}
            Heart rate (LTHR): {"hr": {"value": 85, "units": "%lthr"}}
            Cadence: {"cadence": {"value": 90, "units": "cadence"}}
            Pace by ftp: {"pace": {"value": 80, "units": "%pace"}}
            Pace by zone: {"pace": {"value": 2, "units": "pace_zone"}}
            Zone by power: {"power": {"value": 2, "units": "power_zone"}}
            Zone by heart rate: {"hr": {"value": 2, "units": "hr_zone"}}
        Ranges: Specify ranges for power, heart rate, or cadence:
            {"power": {"start": 80, "end": 90, "units": "%ftp"}}
        Ramps: Instead of a range, indicate a gradual change in intensity (useful for ERG workouts):
            {"ramp": true, "power": {"start": 80, "end": 90, "units": "%ftp"}}
        Repeats: include the reps property and add nested steps
            {"reps": 3,
             "steps": [
                {"power": {"value": 110, "units": "%ftp"}, "distance": 500, "text": "High-intensity"},
                {"power": {"value": 80, "units": "%ftp"}, "duration": 90, "text": "Recovery"}
            ]}
        Free Ride: Include freeride to indicate a segment without ERG control, optionally with a suggested power range:
            {"freeride": true, "power": {"value": 80, "units": "%ftp"}}
        Comments and Labels: Add descriptive text to label steps:
            {"text": "Warmup"}

    How to use steps:
    - Set distance or duration as appropriate for step
    - Use "reps" with nested steps to define repeat intervals (as in example above)
    - Define one of "power", "hr" or "pace" to define step intensity
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    try:
        if event_id:
            existing = await make_intervals_request(
                url=f"/athlete/{athlete_id_to_use}/events/{event_id}", api_key=api_key
            )
            if isinstance(existing, dict) and "error" in existing:
                return f"Error reading event {event_id} before update: {existing.get('message')}"
            if not isinstance(existing, dict) or not existing:
                return f"Error: event {event_id} not found."
            event_data = _merge_event_update(
                existing, name, workout_type, start_date, workout_doc, moving_time, distance,
                category, load_target, time_target, distance_target,
            )
        else:
            event_data = _prepare_event_data(
                name, workout_type, start_date or datetime.now().strftime("%Y-%m-%d"),
                workout_doc, moving_time, distance,
                category, load_target, time_target, distance_target,
            )
        return await _create_or_update_event_request(
            athlete_id_to_use, api_key, event_data, event_data.get("start_date_local", ""), event_id
        )
    except ValueError as e:
        return f"Error: {e}"


@mcp.tool()
async def add_events_bulk(
    events: list[EventSpec],
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> str:
    """Create many calendar events in one call (Intervals.icu bulk endpoint).

    Use this to put a multi-week plan on the calendar instead of calling
    add_or_update_event dozens of times. Every item takes the same fields and formats
    as add_or_update_event when creating: name, start_date ("YYYY-MM-DD" or local
    "YYYY-MM-DDTHH:MM[:SS]"), workout_type, category (default WORKOUT), workout_doc,
    moving_time, distance, load_target, time_target, distance_target.
    All items are validated before anything is sent; nothing is created if one is invalid.

    Args:
        events: List of events to create.
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)

    Example:
        {"events": [
            {"name": "Endurance", "start_date": "2026-12-01T18:00", "workout_type": "VirtualRide",
             "workout_doc": {"steps": [{"duration": 3600, "power": {"value": 65, "units": "%ftp"}}]}},
            {"name": "Weekly", "category": "TARGET", "start_date": "2026-11-30",
             "workout_type": "Ride", "load_target": 450}
        ]}
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not events:
        return "Error: No events provided."

    adapter = TypeAdapter(EventSpec)
    payload: list[dict[str, Any]] = []
    for index, item in enumerate(events, 1):
        try:
            spec = item if isinstance(item, EventSpec) else adapter.validate_python(item)
            payload.append(
                _prepare_event_data(
                    spec.name, spec.workout_type, spec.start_date, spec.workout_doc,
                    spec.moving_time, spec.distance, spec.category,
                    spec.load_target, spec.time_target, spec.distance_target,
                )
            )
        except ValueError as e:  # pydantic.ValidationError is a ValueError
            return f"Error in event #{index}: {e}"

    result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/events/bulk",
        api_key=api_key,
        method="POST",
        data=payload,
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error creating events: {result.get('message', 'Unknown error')}"
    created = [e for e in result if isinstance(e, dict)] if isinstance(result, list) else []
    lines = [f"Created {len(created)} of {len(payload)} events:"]
    lines.extend(format_event_compact(event) for event in created)
    return "\n".join(lines)


@mcp.tool()
async def add_or_update_note(
    name: str,
    description: str,
    start_date: str | None = None,
    color: str | None = "green",
    athlete_id: str | None = None,
    api_key: str | None = None,
    event_id: str | None = None,
) -> str:
    """Add or update a plain text note (category NOTE) on the Intervals.icu calendar.

    Args:
        name: Title of the note
        description: Plain text content of the note
        start_date: Date in YYYY-MM-DD format (optional, defaults to today)
        color: Color of the note (e.g. green, orange, red, blue)
        athlete_id: The Intervals.icu athlete ID (optional)
        api_key: The Intervals.icu API key (optional)
        event_id: The Intervals.icu event ID (optional, for updates)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    if not start_date:
        start_date = datetime.now().strftime("%Y-%m-%d")

    try:
        validated_date = validate_date(start_date)
        event_data = {
            "category": "NOTE",
            "name": name,
            "description": description,
            "start_date_local": validated_date + "T00:00:00",
            "color": color
        }

        return await _create_or_update_event_request(
            athlete_id_to_use, api_key, event_data, validated_date, event_id
        )
    except ValueError as e:
        return f"Error: {e}"


async def _create_or_update_event_request(
    athlete_id: str,
    api_key: str | None,
    event_data: dict[str, Any],
    start_date: str,
    event_id: str | None,
) -> str:
    """Create or update an event via API request.

    Args:
        athlete_id: The athlete ID.
        api_key: Optional API key.
        event_data: Prepared event data dictionary.
        start_date: Start date string for response formatting.
        event_id: Optional event ID for updates.

    Returns:
        Formatted response string.
    """
    url = f"/athlete/{athlete_id}/events"
    if event_id:
        url += f"/{event_id}"
    result = await make_intervals_request(
        url=url,
        api_key=api_key,
        data=event_data,
        method="PUT" if event_id else "POST",
    )
    action = "updated" if event_id else "created"
    return _handle_event_response(result, action, athlete_id, start_date)
