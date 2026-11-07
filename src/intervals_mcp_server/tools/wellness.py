"""
Wellness-related MCP tools for Intervals.icu.

This module contains tools for retrieving athlete wellness data and logging nutrition.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from intervals_mcp_server.api.client import make_intervals_request
from intervals_mcp_server.config import get_config
from intervals_mcp_server.utils.formatting import format_wellness_entry
from intervals_mcp_server.utils.validation import (
    resolve_athlete_id,
    resolve_date_params,
    validate_date,
)

# Import mcp instance from shared module for tool registration
from intervals_mcp_server.mcp_instance import mcp  # noqa: F401

config = get_config()


@mcp.tool()
async def get_wellness_data(
    athlete_id: str | None = None,
    api_key: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_all_fields: bool = False,
) -> str:
    """Get wellness data for an athlete from Intervals.icu.

    By default returns standard wellness fields (training metrics, vitals, sleep,
    subjective scores, etc.). Set include_all_fields=True to also include any
    additional or custom fields configured by the user in Intervals.icu.

    Args:
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
        start_date: Start date in YYYY-MM-DD format (optional, defaults to 30 days ago)
        end_date: End date in YYYY-MM-DD format (optional, defaults to today)
        include_all_fields: If True, include additional and custom fields beyond the standard set (optional, defaults to False)
    """
    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    start_date, end_date = resolve_date_params(start_date, end_date)

    params = {"oldest": start_date, "newest": end_date}

    result = await make_intervals_request(
        url=f"/athlete/{athlete_id_to_use}/wellness", api_key=api_key, params=params
    )

    if isinstance(result, dict) and "error" in result:
        return f"Error fetching wellness data: {result.get('message')}"

    if not result:
        return (
            f"No wellness data found for athlete {athlete_id_to_use} in the specified date range."
        )

    wellness_summary = "Wellness Data:\n\n"

    if isinstance(result, dict):
        for date_str, data in result.items():
            if isinstance(data, dict) and "date" not in data:
                data["date"] = date_str
            wellness_summary += format_wellness_entry(data, include_all_fields=include_all_fields) + "\n\n"
    elif isinstance(result, list):
        for entry in result:
            if isinstance(entry, dict):
                wellness_summary += format_wellness_entry(entry, include_all_fields=include_all_fields) + "\n\n"

    return wellness_summary


# Local argument name -> Intervals.icu wellness field
NUTRITION_FIELDS: dict[str, str] = {
    "kcal": "kcalConsumed",
    "carbs_g": "carbohydrates",
    "protein_g": "protein",
    "fat_g": "fatTotal",
}


def _round_half_up(value: float) -> int:
    """Round to the nearest integer with halves going up (2.5 -> 3, not banker's 2)."""
    return int(Decimal(repr(float(value))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fmt_num(value: float) -> str:
    """Format a number without a trailing .0 (105.0 -> '105', 2.5 -> '2.5')."""
    return f"{float(value):g}"


def compute_nutrition_totals(
    existing: dict[str, Any], deltas: dict[str, float]
) -> dict[str, int]:
    """Add deltas to the existing wellness nutrition fields.

    Missing/None existing values count as 0. Results are clamped at 0 and rounded to integers.

    Args:
        existing: The current wellness record (only the nutrition fields are read).
        deltas: Mapping of kcal/carbs_g/protein_g/fat_g to the values to add (may be negative).

    Returns:
        Mapping of Intervals.icu field name to the new integer total.
    """
    totals: dict[str, int] = {}
    for arg_name, field in NUTRITION_FIELDS.items():
        current = float(existing.get(field) or 0)
        totals[field] = _round_half_up(max(0.0, current + float(deltas.get(arg_name, 0))))
    return totals


def format_nutrition_note(
    note: str, kcal: float, carbs_g: float, protein_g: float, fat_g: float
) -> str:
    """Format a comment line as 'note (+kcal kcal, C/P/F g)'."""
    return (
        f"{note.strip()} ({float(kcal):+g} kcal, "
        f"{_fmt_num(carbs_g)}/{_fmt_num(protein_g)}/{_fmt_num(fat_g)} g)"
    )


def append_comment(existing: str | None, line: str) -> str:
    """Append a line to the existing comments text, keeping what is already there."""
    if not existing:
        return line
    return existing.rstrip("\n") + "\n" + line


@mcp.tool()
async def add_nutrition(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    date: str,
    kcal: float,
    carbs_g: float,
    protein_g: float,
    fat_g: float,
    note: str,
    athlete_id: str | None = None,
    api_key: str | None = None,
) -> str:
    """Add food intake to the day's nutrition totals in Intervals.icu wellness.

    Adds the given values to the existing kcalConsumed / carbohydrates / protein / fatTotal
    of the wellness record (missing values count as 0), clamps at 0, rounds to integers, and
    appends "note (+kcal kcal, C/P/F g)" as a new line to the wellness comments.
    Negative values can be used for corrections.

    Args:
        date: Date in YYYY-MM-DD format
        kcal: Energy in kcal to add (negative to correct)
        carbs_g: Carbohydrates in grams to add (negative to correct)
        protein_g: Protein in grams to add (negative to correct)
        fat_g: Fat in grams to add (negative to correct)
        note: Short description of the food, appended to the wellness comments
        athlete_id: The Intervals.icu athlete ID (optional, will use ATHLETE_ID from .env if not provided)
        api_key: The Intervals.icu API key (optional, will use API_KEY from .env if not provided)
    """
    try:
        validate_date(date)
    except ValueError as e:
        return f"Error: {e}"

    athlete_id_to_use, error_msg = resolve_athlete_id(athlete_id, config.athlete_id)
    if error_msg:
        return error_msg

    url = f"/athlete/{athlete_id_to_use}/wellness/{date}"

    current = await make_intervals_request(url=url, api_key=api_key)
    if isinstance(current, dict) and "error" in current:
        return f"Error fetching wellness data for {date}: {current.get('message')}"
    existing: dict[str, Any] = current if isinstance(current, dict) else {}

    deltas = {"kcal": kcal, "carbs_g": carbs_g, "protein_g": protein_g, "fat_g": fat_g}
    payload: dict[str, Any] = dict(compute_nutrition_totals(existing, deltas))
    payload["comments"] = append_comment(
        existing.get("comments"), format_nutrition_note(note, kcal, carbs_g, protein_g, fat_g)
    )

    result = await make_intervals_request(url=url, api_key=api_key, method="PUT", data=payload)
    if isinstance(result, dict) and "error" in result:
        return f"Error updating wellness data for {date}: {result.get('message')}"

    return (
        f"Nutrition updated for {date}:\n"
        f"- Calories: {payload['kcalConsumed']} kcal\n"
        f"- Carbohydrates: {payload['carbohydrates']} g\n"
        f"- Protein: {payload['protein']} g\n"
        f"- Fat: {payload['fatTotal']} g\n\n"
        f"Comments:\n{payload['comments']}"
    )
