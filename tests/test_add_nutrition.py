"""
Tests for the add_nutrition tool and its pure helpers.
"""

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("API_KEY", "test")
os.environ.setdefault("ATHLETE_ID", "i1")

from intervals_mcp_server.tools.wellness import (  # pylint: disable=wrong-import-position
    add_nutrition,
    append_comment,
    compute_nutrition_totals,
    format_nutrition_note,
)

ZERO = {"kcal": 0, "carbs_g": 0, "protein_g": 0, "fat_g": 0}


def test_totals_missing_fields_count_as_zero():
    result = compute_nutrition_totals({}, {"kcal": 105, "carbs_g": 27, "protein_g": 1.3, "fat_g": 0.4})
    assert result == {"kcalConsumed": 105, "carbohydrates": 27, "protein": 1, "fatTotal": 0}


def test_totals_none_fields_count_as_zero():
    existing = {"kcalConsumed": None, "carbohydrates": None, "protein": None, "fatTotal": None}
    result = compute_nutrition_totals(existing, {**ZERO, "kcal": 200})
    assert result == {"kcalConsumed": 200, "carbohydrates": 0, "protein": 0, "fatTotal": 0}


def test_totals_add_to_existing():
    existing = {"kcalConsumed": 1500, "carbohydrates": 200, "protein": 90, "fatTotal": 50}
    result = compute_nutrition_totals(
        existing, {"kcal": 450, "carbs_g": 60, "protein_g": 25, "fat_g": 12}
    )
    assert result == {"kcalConsumed": 1950, "carbohydrates": 260, "protein": 115, "fatTotal": 62}


def test_totals_negative_correction_and_clamp_at_zero():
    existing = {"kcalConsumed": 300, "carbohydrates": 40, "protein": 10, "fatTotal": 5}
    result = compute_nutrition_totals(
        existing, {"kcal": -100, "carbs_g": -50, "protein_g": -10, "fat_g": -5.4}
    )
    assert result == {"kcalConsumed": 200, "carbohydrates": 0, "protein": 0, "fatTotal": 0}


def test_totals_round_half_up_after_sum():
    # 0.3 + 0.2 = 0.5 -> 1 (sum first, then round; halves go up, not banker's rounding)
    existing = {"kcalConsumed": 0.3, "carbohydrates": 2.25, "protein": 10.4, "fatTotal": 1.5}
    result = compute_nutrition_totals(
        existing, {"kcal": 0.2, "carbs_g": 0.25, "protein_g": 0.05, "fat_g": 1.0}
    )
    assert result == {"kcalConsumed": 1, "carbohydrates": 3, "protein": 10, "fatTotal": 3}


def test_format_note_positive_and_negative():
    assert format_nutrition_note("Banana", 105, 27, 1.3, 0.4) == "Banana (+105 kcal, 27/1.3/0.4 g)"
    assert format_nutrition_note("Fix", -50, -10, 0, 0) == "Fix (-50 kcal, -10/0/0 g)"


def test_append_comment():
    assert append_comment(None, "a") == "a"
    assert append_comment("", "a") == "a"
    assert append_comment("Slept well", "a") == "Slept well\na"
    assert append_comment("line1\n", "a") == "line1\na"


def test_add_nutrition_puts_only_nutrition_fields(monkeypatch):
    calls = []
    existing = {
        "id": "2026-11-07",
        "kcalConsumed": 1000,
        "carbohydrates": 120,
        "protein": None,
        "weight": 72.5,
        "restingHR": 48,
        "comments": "Felt good",
    }

    async def fake_request(*_args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("method", "GET") == "GET":
            return existing
        return {**existing, **kwargs["data"]}

    monkeypatch.setattr("intervals_mcp_server.tools.wellness.make_intervals_request", fake_request)
    result = asyncio.run(add_nutrition("2026-11-07", 450, 60, 25, 12.5, "Pasta"))

    assert [c.get("method", "GET") for c in calls] == ["GET", "PUT"]
    assert calls[0]["url"] == "/athlete/i1/wellness/2026-11-07"
    assert calls[1]["url"] == "/athlete/i1/wellness/2026-11-07"
    assert calls[1]["data"] == {
        "kcalConsumed": 1450,
        "carbohydrates": 180,
        "protein": 25,
        "fatTotal": 13,
        "comments": "Felt good\nPasta (+450 kcal, 60/25/12.5 g)",
    }
    assert "- Calories: 1450 kcal" in result
    assert "- Fat: 13 g" in result
    assert "Felt good\nPasta (+450 kcal, 60/25/12.5 g)" in result


def test_add_nutrition_get_error_skips_put(monkeypatch):
    calls = []

    async def fake_request(*_args, **kwargs):
        calls.append(kwargs)
        return {"error": True, "message": "401 Unauthorized"}

    monkeypatch.setattr("intervals_mcp_server.tools.wellness.make_intervals_request", fake_request)
    result = asyncio.run(add_nutrition("2026-11-07", 100, 10, 5, 2, "Snack"))
    assert len(calls) == 1
    assert "Error fetching wellness data" in result


def test_add_nutrition_invalid_date(monkeypatch):
    async def fake_request(*_args, **_kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr("intervals_mcp_server.tools.wellness.make_intervals_request", fake_request)
    result = asyncio.run(add_nutrition("07-11-2026", 100, 10, 5, 2, "Snack"))
    assert result.startswith("Error:")
