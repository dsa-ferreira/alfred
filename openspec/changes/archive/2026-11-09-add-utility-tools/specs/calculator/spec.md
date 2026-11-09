# Spec Delta

## Purpose

Lets the model get exact arithmetic results and unit conversions from alfred instead of working them out itself, which the brains alfred runs on do unreliably.

## ADDED Requirements

### Requirement: Calculator tool offered on every turn
alfred SHALL offer the model `alfred_calculate` on every turn: owner, `/ask` and reminder turns.

#### Scenario: Owner turn
- **WHEN** the owner sends a message
- **THEN** `alfred_calculate` is among the offered tools

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** `alfred_calculate` is among the offered tools

### Requirement: Exact arithmetic
`alfred_calculate` SHALL take an `expression` with numbers, `+ - * / // % **`, unary minus, parentheses and a fixed set of functions and constants (`sqrt`, `abs`, `round`, `min`, `max`, `floor`, `ceil`, `log`, `log10`, `exp`, `pi`, `e`). It SHALL return the expression and its result. Integer results SHALL be exact; other results SHALL be rounded to 12 significant digits with no float artefacts.

#### Scenario: Product
- **WHEN** the model calls `alfred_calculate` with expression `347 * 28.5`
- **THEN** the result is `9889.5`

#### Scenario: No float artefacts
- **WHEN** the expression is `0.1 + 0.2`
- **THEN** the result is `0.3`

#### Scenario: Large integer stays exact
- **WHEN** the expression is `2 ** 64`
- **THEN** the result is `18446744073709551616`

#### Scenario: Function
- **WHEN** the expression is `round(sqrt(2), 3)`
- **THEN** the result is `1.414`

### Requirement: Expressions are never executed as code
`alfred_calculate` SHALL accept only the elements listed in "Exact arithmetic". Any other syntax (names, attribute access, calls to anything else, strings, comprehensions, lambdas) SHALL be rejected with an error result that names what is not allowed. It SHALL NOT run the expression as code.

#### Scenario: Code injection
- **WHEN** the expression is `__import__('os').system('ls')`
- **THEN** the tool returns an error result and nothing is executed

#### Scenario: Unknown name
- **WHEN** the expression is `x + 1`
- **THEN** the tool returns an error result saying `x` is not allowed

### Requirement: Calculation limits
`alfred_calculate` SHALL reject, with an error result, an expression longer than 500 characters, an expression whose result would be too large to compute quickly (such as an exponent that is too big), and division by zero. It SHALL NOT stall the turn.

#### Scenario: Huge power
- **WHEN** the expression is `9 ** 9 ** 9`
- **THEN** the tool returns an error result at once, saying the number is too large

#### Scenario: Division by zero
- **WHEN** the expression is `1 / 0`
- **THEN** the tool returns an error result saying it divides by zero

### Requirement: Unit conversion
When given both `from_unit` and `to_unit`, `alfred_calculate` SHALL treat the expression's result as a quantity in `from_unit` and return it converted to `to_unit`, with both units in the result. It SHALL support at least length, mass, volume (including cups, tablespoons, teaspoons and fluid ounces), temperature, time, speed, energy and data size. Giving only one of the two units SHALL be an error.

#### Scenario: Temperature
- **WHEN** the model calls `alfred_calculate` with expression `350`, from_unit `degF` and to_unit `degC`
- **THEN** the result is 350 °F expressed as about `176.667` °C

#### Scenario: Converting a computed amount
- **WHEN** the expression is `2.5 * 3`, from_unit `lb` and to_unit `kg`
- **THEN** the result is 7.5 lb expressed as about `3.40194` kg

#### Scenario: Cooking volume
- **WHEN** the expression is `1.5`, from_unit `cup` and to_unit `ml`
- **THEN** the result is 1.5 cups expressed in millilitres

### Requirement: Unit errors
`alfred_calculate` SHALL return an error result naming the problem when a unit is unknown, or when the two units measure different things (such as volume and mass), and SHALL say in that case that a conversion between them needs more information.

#### Scenario: Volume to mass
- **WHEN** the expression is `1`, from_unit `cup` and to_unit `g`
- **THEN** the tool returns an error result saying cups (volume) cannot be converted to grams (mass) without more information, such as the ingredient's density

#### Scenario: Unknown unit
- **WHEN** from_unit is `smoots-per-fortnight-ish`
- **THEN** the tool returns an error result saying the unit is unknown
