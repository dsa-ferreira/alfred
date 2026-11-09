"""``alfred_calculate``: exact arithmetic and unit conversion (change add-utility-tools).

The model works sums out badly, so alfred does them (design D3, D4):

```
expression --ast.parse--> whitelisted walk --> int (exact) or float (12 significant digits)
                                                  |
               from_unit/to_unit, checked --> pint Quantity(value, from).to(to)
```

The expression is never run as code: only numbers, the arithmetic operators,
``pi``, ``e`` and a few math functions are evaluated, and powers and results
that would be too large are refused before they are computed. pint only
ever sees unit names, never the number.
"""

from __future__ import annotations

import ast
import functools
import math
import operator
import re
from collections.abc import Callable
from typing import Any

import pint

from alfred.tools.base import AttemptContext, LocalToolset, ToolOutcome, parse_arguments

CALCULATE = "alfred_calculate"

MAX_CHARS = 500
MAX_DIGITS = 1000  # largest integer result, in decimal digits
MAX_UNIT_CHARS = 40
SIGNIFICANT_DIGITS = 12

FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log10": math.log10,
    "exp": math.exp,
}
CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}

_BINARY: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

ALLOWED = "numbers, + - * / // % **, parentheses, pi, e and the functions " + ", ".join(FUNCTIONS)

CALCULATE_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": CALCULATE,
        "description": (
            "Use for any arithmetic, percentage or unit conversion instead of working it "
            "out yourself: alfred computes it exactly. The expression uses numbers, "
            "+ - * / // ** and parentheses, % (remainder, not percent), pi, e and "
            f"{', '.join(FUNCTIONS)}. Write percentages as /100 (15% of 80 is "
            "0.15 * 80). To convert units, also pass from_unit and to_unit: the "
            "expression's result is taken in from_unit. Units such as cup, tbsp, tsp, "
            "floz, ml, l, g, kg, oz, lb, degC, degF, km, mi, km/h, mph, kcal, kJ, "
            "GB, MiB, hour."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "What to compute, such as 347 * 28.5 or round(sqrt(2), 3).",
                },
                "from_unit": {
                    "type": "string",
                    "description": "The unit of the expression's result, such as degF.",
                },
                "to_unit": {
                    "type": "string",
                    "description": "The unit to convert it to, such as degC.",
                },
            },
            "required": ["expression"],
        },
    },
}


class CalculationError(ValueError):
    """The calculation can't be done; the message is written for the model."""


# --- arithmetic (design D3) ------------------------------------------------------


def evaluate(expression: str) -> int | float:
    """The value of ``expression``. Raises :class:`CalculationError`."""
    if not expression.strip():
        raise CalculationError("the expression is empty.")
    if len(expression) > MAX_CHARS:
        raise CalculationError(f"the expression is too long (at most {MAX_CHARS} characters).")
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise CalculationError(f"can't read the expression. Use {ALLOWED}.") from None
    try:
        return _eval(tree.body)
    except CalculationError:
        raise
    except ZeroDivisionError:
        raise CalculationError("it divides by zero.") from None
    except OverflowError:
        raise CalculationError("the number is too large.") from None
    except RecursionError:
        raise CalculationError("the expression is nested too deeply.") from None
    except (ValueError, TypeError) as exc:
        raise CalculationError(f"it has no result ({exc}).") from None


def _eval(node: ast.AST) -> int | float:
    match node:
        case ast.Constant(value=value) if type(value) in (int, float):
            return _checked(value)
        case ast.UnaryOp(op=op, operand=operand) if type(op) in _UNARY:
            return _checked(_UNARY[type(op)](_eval(operand)))
        case ast.BinOp(left=left, op=ast.Pow(), right=right):
            return _power(_eval(left), _eval(right))
        case ast.BinOp(left=left, op=op, right=right) if type(op) in _BINARY:
            return _checked(_BINARY[type(op)](_eval(left), _eval(right)))
        case ast.Name(id=name):
            if name in CONSTANTS:
                return CONSTANTS[name]
            raise CalculationError(f"{name} is not allowed. Use {ALLOWED}.")
        case ast.Call(func=ast.Name(id=name), args=args, keywords=[]) if name in FUNCTIONS:
            if any(isinstance(a, ast.Starred) for a in args):
                raise CalculationError(f"{name} takes plain numbers, not unpacked lists.")
            return _checked(FUNCTIONS[name](*(_eval(a) for a in args)))
        case ast.Call(func=ast.Name(id=name)):
            raise CalculationError(f"the function {name} is not allowed. Use {ALLOWED}.")
        case ast.Constant(value=value):
            raise CalculationError(f"{value!r} is not a number. Use {ALLOWED}.")
        case _:
            raise CalculationError(
                f"{ast.unparse(node)!r} is not allowed. Use {ALLOWED}."
                if isinstance(node, ast.expr)
                else f"that is not allowed. Use {ALLOWED}."
            )


def _power(base: int | float, exponent: int | float) -> int | float:
    """``base ** exponent``, refused before it is computed if it would be too large."""
    whole = isinstance(base, int) and isinstance(exponent, int) and exponent > 0 and abs(base) > 1
    if whole and exponent * math.log10(abs(base)) > MAX_DIGITS:
        raise CalculationError("the number is too large.")
    result = base**exponent
    if isinstance(result, complex):
        raise CalculationError("it has no real result.")
    return _checked(result)


def _checked(value: Any) -> int | float:
    if isinstance(value, complex):
        raise CalculationError("it has no real result.")
    if isinstance(value, int) and value.bit_length() > MAX_DIGITS * 3.33:
        raise CalculationError("the number is too large.")
    if isinstance(value, float) and not math.isfinite(value):
        raise CalculationError("the number is too large.")
    return value


def format_number(value: int | float) -> str:
    """Integers exactly; other numbers to 12 significant digits, without float noise."""
    if isinstance(value, int):
        return str(value)
    if value.is_integer() and abs(value) < 10**15:
        return str(int(value))
    return format(value, f".{SIGNIFICANT_DIGITS}g")


# --- units (design D4) -----------------------------------------------------------

# Letters, °, spaces, / * ^, and a digit only as a one-digit exponent (m^2, m**3).
_UNIT = re.compile(r"(?:[^\W\d]|[° /*^]|(?<=\^)\d|(?<=\*\*)\d)+")

_DIMENSIONS = {
    "[length]": "length",
    "[length] ** 2": "area",
    "[length] ** 3": "volume",
    "[mass]": "mass",
    "[temperature]": "temperature",
    "[time]": "time",
    "[length] / [time]": "speed",
    "[mass] * [length] ** 2 / [time] ** 2": "energy",
    "dimensionless": "a count or data size",
}


@functools.cache
def registry() -> pint.UnitRegistry:
    """pint's units, loaded once (about half a second)."""
    return pint.UnitRegistry()


def check_unit(unit: str) -> str:
    value = unit.strip()
    if not value or len(value) > MAX_UNIT_CHARS or not _UNIT.fullmatch(value):
        raise CalculationError(
            f"{unit!r} is not a unit alfred accepts: use a unit name such as cup, kg or degC "
            "(numbers only as a one-digit power, such as m^2)."
        )
    return value


def convert(value: int | float, from_unit: str, to_unit: str) -> tuple[str, str]:
    """``value`` in ``from_unit`` and in ``to_unit``, each as ``<number> <unit>``."""
    source, target = check_unit(from_unit), check_unit(to_unit)
    units = registry()
    try:
        quantity = units.Quantity(value, source)
        converted = quantity.to(target)
    except pint.DimensionalityError:
        raise CalculationError(_mismatch(units, source, target)) from None
    except (pint.UndefinedUnitError, pint.DefinitionSyntaxError) as exc:
        name = getattr(exc, "unit_names", None) or (source, target)
        unknown = name if isinstance(name, str) else " or ".join(repr(n) for n in name)
        raise CalculationError(f"unknown unit {unknown}.") from None
    except (pint.PintError, OverflowError, ZeroDivisionError) as exc:
        raise CalculationError(f"can't convert {source} to {target} ({exc}).") from None
    magnitude = _checked(converted.magnitude)
    return (
        f"{format_number(value)} {quantity.units}",
        f"{format_number(magnitude)} {converted.units}",
    )


def _mismatch(units: pint.UnitRegistry, source: str, target: str) -> str:
    quantities = [units.Quantity(1, u) for u in (source, target)]
    what = [_DIMENSIONS.get(str(q.dimensionality), "") for q in quantities]
    named = [
        f"{q.units} ({w})" if w else str(q.units) for q, w in zip(quantities, what, strict=True)
    ]
    message = f"{named[0]} can't be converted to {named[1]} without more information"
    if set(what) == {"volume", "mass"}:
        message += ", such as the ingredient's density"
    return message + "."


# --- the tool --------------------------------------------------------------------


def calculate(arguments: dict[str, Any]) -> str:
    """The tool's answer. Raises :class:`CalculationError`."""
    expression = arguments.get("expression")
    if isinstance(expression, bool) or not isinstance(expression, str | int | float):
        raise CalculationError("expression is required: what to compute, such as 2 * 3.")
    expression = str(expression)
    from_unit = arguments.get("from_unit") or None
    to_unit = arguments.get("to_unit") or None
    if (from_unit is None) != (to_unit is None):
        raise CalculationError("to convert units, pass both from_unit and to_unit.")
    if from_unit is not None and not (isinstance(from_unit, str) and isinstance(to_unit, str)):
        raise CalculationError("from_unit and to_unit must be text, such as degF and degC.")

    value = evaluate(expression)
    result = format_number(value)
    head = expression.strip()
    if from_unit is None:
        return f"{head} = {result}"
    source, target = convert(value, from_unit, to_unit)  # type: ignore[arg-type]
    if head == result:
        return f"{source} = {target}"
    return f"{head} = {source} = {target}"


class CalculatorTools(LocalToolset):
    """``alfred_calculate``, on every turn."""

    definitions = [CALCULATE_TOOL]

    async def call(self, name: str, raw_arguments: Any, attempt: AttemptContext) -> ToolOutcome:
        arguments = parse_arguments(raw_arguments)
        if isinstance(arguments, str):
            return ToolOutcome(False, arguments)
        try:
            return ToolOutcome(True, calculate(arguments))
        except CalculationError as exc:
            return ToolOutcome(False, f"Error: {exc}")
