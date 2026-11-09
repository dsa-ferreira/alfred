"""``alfred_calculate`` (change add-utility-tools)."""

from __future__ import annotations

import json
import time

import pytest

from alfred.tools.base import AttemptContext, ToolOutcome, TurnInput
from alfred.tools.calculator import CALCULATE, CALCULATE_TOOL, CalculatorTools

OWNER = TurnInput("owner", "what's 347 times 28.5?", 1)


async def calc(**arguments: object) -> ToolOutcome:
    return await CalculatorTools().call(CALCULATE, json.dumps(arguments), AttemptContext(OWNER))


async def result(**arguments: object) -> str:
    outcome = await calc(**arguments)
    assert outcome.ok, outcome.text
    return outcome.text


async def error(**arguments: object) -> str:
    outcome = await calc(**arguments)
    assert not outcome.ok, outcome.text
    assert outcome.text.startswith("Error: ")
    return outcome.text


# --- offered -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "turn",
    [OWNER, TurnInput("ask", "x", 2, "disk-check"), TurnInput("reminder", "x", 3)],
)
async def test_offered_on_every_turn(turn: TurnInput) -> None:
    tools = CalculatorTools()
    assert await tools.open(turn) is tools
    assert tools.definitions == [CALCULATE_TOOL]


def test_description_steers_the_model() -> None:
    description = CALCULATE_TOOL["function"]["description"]
    assert description.startswith("Use for any arithmetic")
    assert "remainder, not percent" in description and "/100" in description
    assert "floz" in description and "degF" in description


# --- exact arithmetic ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("347 * 28.5", "347 * 28.5 = 9889.5"),
        ("0.1 + 0.2", "0.1 + 0.2 = 0.3"),
        ("2 ** 64", "2 ** 64 = 18446744073709551616"),
        ("round(sqrt(2), 3)", "round(sqrt(2), 3) = 1.414"),
        ("7 / 7", "7 / 7 = 1"),
        ("0.15 * 80", "0.15 * 80 = 12"),
        ("17 // 5 + 17 % 5", "17 // 5 + 17 % 5 = 5"),
        ("-(3 - 5)", "-(3 - 5) = 2"),
        ("max(1, 4, 2) - min(3, 2)", "max(1, 4, 2) - min(3, 2) = 2"),
        ("floor(2.7) + ceil(2.1)", "floor(2.7) + ceil(2.1) = 5"),
        ("log10(1000)", "log10(1000) = 3"),
        ("round(log(e) + exp(0) + abs(-1), 2)", "round(log(e) + exp(0) + abs(-1), 2) = 3"),
        ("2 * pi", "2 * pi = 6.28318530718"),
        ("1e20 * 3", "1e20 * 3 = 3e+20"),
    ],
)
async def test_exact_arithmetic(expression: str, expected: str) -> None:
    assert await result(expression=expression) == expected


async def test_number_as_expression() -> None:
    assert await result(expression=42) == "42 = 42"


# --- never executed as code --------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('ls')",
        "abs.__class__",
        "(lambda: 1)()",
        "[x for x in ()]",
        "(1, 2)",
        "'a' * 3",
        "True + 1",
        "open('/etc/passwd')",
        "round(2.5, ndigits=1)",
        "max(*[1, 2])",
        "1 if 1 else 2",
        "1 < 2",
    ],
)
async def test_not_executed(expression: str) -> None:
    assert "not" in await error(expression=expression)


async def test_unknown_name_is_named() -> None:
    text = await error(expression="x + 1")
    assert text.startswith("Error: x is not allowed.")


async def test_unknown_function_is_named() -> None:
    assert "the function open is not allowed" in await error(expression="open('x')")


async def test_unreadable_expression() -> None:
    assert "can't read the expression" in await error(expression="2 +* 3")


# --- limits ------------------------------------------------------------------------------


async def test_huge_power_is_refused_at_once() -> None:
    started = time.monotonic()
    assert await error(expression="9 ** 9 ** 9") == "Error: the number is too large."
    assert time.monotonic() - started < 1


@pytest.mark.parametrize(
    "expression",
    ["10 ** 1001", "10.0 ** 400", "(2 ** 3000) * (2 ** 3000)", "1.0000001 ** 1000000000000"],
)
async def test_too_large(expression: str) -> None:
    assert await error(expression=expression) == "Error: the number is too large."


async def test_large_but_allowed() -> None:
    assert await result(expression="10 ** 999") == "10 ** 999 = 1" + "0" * 999


async def test_division_by_zero() -> None:
    assert await error(expression="1 / 0") == "Error: it divides by zero."
    assert await error(expression="5 % 0") == "Error: it divides by zero."


async def test_too_long() -> None:
    assert "too long" in await error(expression="1 + " * 200 + "1")


@pytest.mark.parametrize("expression", ["sqrt(-1)", "(-8) ** (1 / 3)", "log(0)", "max()"])
async def test_no_result(expression: str) -> None:
    assert "no" in await error(expression=expression)


@pytest.mark.parametrize("arguments", [{}, {"expression": ""}, {"expression": True}])
async def test_expression_required(arguments: dict[str, object]) -> None:
    outcome = await CalculatorTools().call(CALCULATE, json.dumps(arguments), AttemptContext(OWNER))
    assert not outcome.ok


async def test_invalid_json() -> None:
    outcome = await CalculatorTools().call(CALCULATE, "{not json", AttemptContext(OWNER))
    assert not outcome.ok and "not valid JSON" in outcome.text


# --- unit conversion ---------------------------------------------------------------------


async def test_temperature() -> None:
    assert await result(expression="350", from_unit="degF", to_unit="degC") == (
        "350 degree_Fahrenheit = 176.666666667 degree_Celsius"
    )


async def test_degree_sign() -> None:
    assert await result(expression="350", from_unit="°F", to_unit="°C") == (
        "350 degree_Fahrenheit = 176.666666667 degree_Celsius"
    )


async def test_converting_a_computed_amount() -> None:
    assert await result(expression="2.5 * 3", from_unit="lb", to_unit="kg") == (
        "2.5 * 3 = 7.5 pound = 3.401942775 kilogram"
    )


async def test_cooking_volume() -> None:
    assert await result(expression="1.5", from_unit="cup", to_unit="ml") == (
        "1.5 cup = 354.88235475 milliliter"
    )
    assert await result(expression="1", from_unit="floz", to_unit="ml") == (
        "1 fluid_ounce = 29.5735295625 milliliter"
    )
    assert await result(expression="1", from_unit="tbsp", to_unit="tsp") == (
        "1 tablespoon = 3 teaspoon"
    )


@pytest.mark.parametrize(
    ("from_unit", "to_unit", "expected"),
    [
        ("km", "mi", "10 kilometer = 6.21371192237 mile"),
        ("km/h", "mph", "10 kilometer / hour = 6.21371192237 mile_per_hour"),
        ("hour", "minute", "10 hour = 600 minute"),
        ("kcal", "kJ", "10 kilocalorie = 41.84 kilojoule"),
        ("GB", "MiB", "10 gigabyte = 9536.74316406 mebibyte"),
        ("m^2", "cm^2", "10 meter ** 2 = 100000 centimeter ** 2"),
    ],
)
async def test_other_units(from_unit: str, to_unit: str, expected: str) -> None:
    assert await result(expression="10", from_unit=from_unit, to_unit=to_unit) == expected


async def test_one_unit_only() -> None:
    assert "pass both from_unit and to_unit" in await error(expression="1", from_unit="m")
    assert "pass both from_unit and to_unit" in await error(expression="1", to_unit="m")


# --- unit errors ---------------------------------------------------------------------------


async def test_volume_to_mass() -> None:
    assert await error(expression="1", from_unit="cup", to_unit="g") == (
        "Error: cup (volume) can't be converted to gram (mass) without more information, "
        "such as the ingredient's density."
    )


async def test_different_things() -> None:
    assert await error(expression="1", from_unit="km", to_unit="hour") == (
        "Error: kilometer (length) can't be converted to hour (time) without more information."
    )


async def test_unknown_unit() -> None:
    assert await error(expression="1", from_unit="blargs", to_unit="m") == (
        "Error: unknown unit 'blargs'."
    )


@pytest.mark.parametrize(
    "unit", ["smoots-per-fortnight-ish", "m**99999999", "m^22", "__import__('os')", "x" * 41]
)
async def test_unit_refused_before_pint(unit: str) -> None:
    assert "is not a unit alfred accepts" in await error(
        expression="1", from_unit=unit, to_unit="m"
    )
