# Spec Delta

## Purpose

Lets the model get calendar arithmetic and the current time in other timezones from alfred, computed in the owner's timezone, instead of working out weekdays and day counts itself.

## ADDED Requirements

### Requirement: Date tool offered on every turn
alfred SHALL offer the model `alfred_date` on every turn: owner, `/ask` and reminder turns.

#### Scenario: Owner turn
- **WHEN** the owner sends a message
- **THEN** `alfred_date` is among the offered tools

#### Scenario: Reminder turn
- **WHEN** a reminder turn runs
- **THEN** `alfred_date` is among the offered tools

### Requirement: Dates the tool accepts
`alfred_date` SHALL accept dates as `YYYY-MM-DD` or `today`, where `today` is the current date in the configured timezone. An unreadable or impossible date SHALL be an error result that names the accepted forms.

#### Scenario: Today
- **WHEN** it is 00:30 on Tuesday 10 November 2026 in `Europe/Lisbon`, the configured timezone, and the model passes `today`
- **THEN** the tool uses 10 November 2026

#### Scenario: Impossible date
- **WHEN** the model passes `2027-02-30`
- **THEN** the tool returns an error result saying the date is not valid

### Requirement: Days between two dates
With `op` `between`, `alfred_date` SHALL take `from` and `to` dates and return both dates with their weekdays and the number of days from one to the other, also as weeks and days. When `to` is before `from`, it SHALL say the count is in the past rather than return a bare negative number.

#### Scenario: Days until Christmas
- **WHEN** today is Monday 9 November 2026 and the model calls `between` from `today` to `2026-12-25`
- **THEN** the result says Friday 25 December 2026 is 46 days (6 weeks and 4 days) after Monday 9 November 2026

#### Scenario: Date in the past
- **WHEN** today is 9 November 2026 and the model calls `between` from `today` to `2026-10-01`
- **THEN** the result says 1 October 2026 was 39 days before 9 November 2026

### Requirement: Shift a date
With `op` `shift`, `alfred_date` SHALL take a `date` and any of `days`, `weeks` and `months` (whole numbers, possibly negative) and return the resulting date with its weekday. Adding months to a day that the target month lacks SHALL give that month's last day. A result outside years 1 to 9999 SHALL be an error result.

#### Scenario: A hundred days on
- **WHEN** the model calls `shift` with date `2026-11-09` and days `100`
- **THEN** the result is Wednesday 17 February 2027

#### Scenario: Month end
- **WHEN** the model calls `shift` with date `2027-01-31` and months `1`
- **THEN** the result is Sunday 28 February 2027

### Requirement: Weekday of a date
With `op` `weekday`, `alfred_date` SHALL take a `date` and return its weekday with the full date.

#### Scenario: Weekday lookup
- **WHEN** the model calls `weekday` with date `2027-03-03`
- **THEN** the result says Wednesday 3 March 2027

### Requirement: Current time in a timezone
With `op` `now_in`, `alfred_date` SHALL return the current date and time in the given IANA `zone`, and how far ahead of or behind the owner's timezone it is. With no `zone`, it SHALL return the current date and time in the owner's timezone. An unknown zone SHALL be an error result asking for an IANA name such as `Asia/Tokyo`.

#### Scenario: Time in Tokyo
- **WHEN** it is 14:05 on Monday 9 November 2026 in `Europe/Lisbon`, the configured timezone, and the model calls `now_in` with zone `Asia/Tokyo`
- **THEN** the result says it is 23:05 on Monday 9 November 2026 in Tokyo, 9 hours ahead of the owner

#### Scenario: Owner's own time
- **WHEN** the model calls `now_in` with no zone at 14:05 Lisbon time
- **THEN** the result says it is 14:05 on Monday 9 November 2026

#### Scenario: Unknown zone
- **WHEN** the model calls `now_in` with zone `Tokyo`
- **THEN** the tool returns an error result asking for an IANA timezone name such as `Asia/Tokyo`

### Requirement: Unknown operation
`alfred_date` SHALL return an error result listing the valid operations when `op` is missing or not one of `between`, `shift`, `weekday` and `now_in`, or when a value that operation needs is missing.

#### Scenario: Bad op
- **WHEN** the model calls `alfred_date` with op `diff`
- **THEN** the tool returns an error result listing `between`, `shift`, `weekday` and `now_in`
