"""
Athlete settings MCP tools for Intervals.icu.

This module contains tools for reading the athlete profile thresholds (FTP, LTHR,
max HR, weight, zones per sport) and for updating sport-specific thresholds.
"""

from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.validation import resolve_athlete_id

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import mcp  # noqa: F401

config = get_config()


def _format_power_zones(ftp: Any, zones: Any) -> str | None:
    """Format power zones (upper bounds in % of FTP) as ranges, with watts when FTP is known."""
    if not isinstance(zones, list) or not zones:
        return None
    parts = []
    lower = 0
    for index, upper in enumerate(zones, 1):
        upper_label = "+" if isinstance(upper, (int, float)) and upper >= 999 else f"-{upper}"
        text = f"Z{index} {lower}{upper_label}%"
        if isinstance(ftp, (int, float)) and ftp and isinstance(upper, (int, float)):
            low_w = round(ftp * lower / 100)
            text += f" ({low_w}+ W)" if upper >= 999 else f" ({low_w}-{round(ftp * upper / 100)} W)"
        parts.append(text)
        lower = upper
    return ", ".join(parts)


def _format_hr_zones(zones: Any) -> str | None:
    """Format HR zones (upper bounds in bpm) as ranges."""
    if not isinstance(zones, list) or not zones:
        return None
    parts = []
    lower = 0
    for index, upper in enumerate(zones, 1):
        parts.append(f"Z{index} {lower}-{upper} bpm")
        lower = upper
    return ", ".join(parts)


def format_sport_settings(settings: dict[str, Any]) -> str:
    """Format one sport-settings block into a readable string."""
    types = settings.get("types") or []
    lines = [f"Sport settings (id {settings.get('id', 'N/A')}): {', '.join(map(str, types)) or 'N/A'}"]
    for key, label, unit in [
        ("ftp", "FTP", " W"),
        ("indoor_ftp", "Indoor FTP", " W"),
        ("w_prime", "W'", " J"),
        ("p_max", "Pmax", " W"),
        ("lthr", "LTHR", " bpm"),
        ("max_hr", "Max HR", " bpm"),
        ("threshold_pace", "Threshold pace", " m/s"),
        ("pace_units", "Pace units", ""),
    ]:
        if settings.get(key) is not None:
            lines.append(f"  {label}: {settings[key]}{unit}")
    power = _format_power_zones(settings.get("ftp"), settings.get("power_zones"))
    if power:
        lines.append(f"  Power zones: {power}")
    hr = _format_hr_zones(settings.get("hr_zones"))
    if hr:
        lines.append(f"  HR zones: {hr}")
    if isinstance(settings.get("pace_zones"), list) and settings["pace_zones"]:
        lines.append(f"  Pace zones (% threshold pace): {settings['pace_zones']}")
    return "\n".join(lines)


def format_athlete_settings(athlete: dict[str, Any], sport: str | None = None) -> str:
    """Format the athlete profile thresholds and per-sport settings."""
    lines = ["Athlete Settings:"]
    lines.append(f"ID: {athlete.get('id', 'N/A')}")
    weight = athlete.get("icu_weight") or athlete.get("weight")
    if weight is not None:
        lines.append(f"Weight: {weight} kg")
    if athlete.get("icu_resting_hr") is not None:
        lines.append(f"Resting HR: {athlete['icu_resting_hr']} bpm")
    if athlete.get("timezone"):
        lines.append(f"Timezone: {athlete['timezone']}")
    lines.append("")

    sport_settings = [s for s in athlete.get("sportSettings") or [] if isinstance(s, dict)]
    if sport:
        wanted = sport.strip().lower()
        sport_settings = [
            s for s in sport_settings if wanted in [str(t).lower() for t in s.get("types") or []]
        ]
        if not sport_settings:
            lines.append(f"No sport settings found for '{sport}'.")
    for settings in sport_settings:
        lines.append(format_sport_settings(settings))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@mcp.tool()
async def get_athlete_settings(
    athlete_id: str | None = None,
    api_key: str | None = None,
    sport: str | None = None,
) -> str:
    """Get athlete thresholds and zones from Intervals.icu.

    Returns weight, resting HR, timezone and, for every sport-settings block
    (e.g. Ride/VirtualRide, Run, Swim): FTP, indoor FTP, W', LTHR, max HR,
    threshold pace and power / heart-rate / pace zones.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        sport: Only show the settings block that covers this activity type, e.g. "Ride",
            "VirtualRide", "Run" (optional, defaults to all sports)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    result = await make_intervals_request(url=f"/athlete/{athlete_id_to_use}", api_key=api_key)
    if isinstance(result, dict) and "error" in result:
        return f"Error fetching athlete settings: {result.get('message', 'Unknown error')}"
    if not isinstance(result, dict) or not result:
        return f"No settings found for athlete {athlete_id_to_use}."
    return format_athlete_settings(result, sport)


@mcp.tool()
async def update_sport_settings(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    sport: str,
    ftp: int | None = None,
    indoor_ftp: int | None = None,
    lthr: int | None = None,
    max_hr: int | None = None,
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> str:
    """Update FTP / indoor FTP / LTHR / max HR of one sport on Intervals.icu.

    Only the values you pass are changed. Past activities are NOT reprocessed and
    HR zones are not recalculated (Intervals.icu recalcHrZones=false); new
    activities and planned workouts use the new values. The previous and new
    values are returned. Confirm with the user before changing thresholds.

    Args:
        sport: Activity type whose settings block to change, e.g. "Ride", "VirtualRide",
            "Run". Ride and VirtualRide usually share one block.
        ftp: New FTP in watts (optional)
        indoor_ftp: New indoor FTP in watts (optional)
        lthr: New lactate threshold heart rate in bpm (optional)
        max_hr: New maximum heart rate in bpm (optional)
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg
    if not sport or not sport.strip():
        return "Error: sport is required (e.g. Ride, Run)."

    changes = {
        key: value
        for key, value in {"ftp": ftp, "indoor_ftp": indoor_ftp, "lthr": lthr, "max_hr": max_hr}.items()
        if value is not None
    }
    if not changes:
        return "Error: Provide at least one of ftp, indoor_ftp, lthr, max_hr."
    invalid = [key for key, value in changes.items() if value <= 0]
    if invalid:
        return f"Error: {', '.join(invalid)} must be a positive number."

    url = f"/athlete/{athlete_id_to_use}/sport-settings/{sport.strip()}"
    before = await make_intervals_request(url=url, api_key=api_key)
    if isinstance(before, dict) and "error" in before:
        return f"Error fetching sport settings for {sport}: {before.get('message', 'Unknown error')}"
    before_dict = before if isinstance(before, dict) else {}

    result = await make_intervals_request(
        url=url,
        api_key=api_key,
        method="PUT",
        params={"recalcHrZones": "false"},
        data=changes,
    )
    if isinstance(result, dict) and "error" in result:
        return f"Error updating sport settings for {sport}: {result.get('message', 'Unknown error')}"

    after = result if isinstance(result, dict) else {}
    lines = [f"Updated sport settings for {sport}:"]
    for key in changes:
        lines.append(f"  {key}: {before_dict.get(key, 'N/A')} -> {after.get(key, changes[key])}")
    if after:
        lines.append("")
        lines.append(format_sport_settings(after))
    return "\n".join(lines)
