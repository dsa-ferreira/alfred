# Spec Delta

## MODIFIED Requirements

### Requirement: Turn context size is logged
At the start of each turn, alfred SHALL log one line at INFO level with the size, in characters, of the context it sends: the system prompt and each server's instructions and the user's facts in it, the tool definitions in total, per MCP server and for alfred's own tools, and the history. The line SHALL identify the turn and SHALL NOT include message or prompt text.

#### Scenario: Owner turn with two servers
- **WHEN** an owner turn runs with cookbook and mcparr available and 30 history entries
- **THEN** the log has one line for that turn giving the system prompt size, cookbook's and mcparr's instruction sizes, the tool definition sizes for cookbook, mcparr and alfred's own tools with their total and tool count, and the size of the 30 history entries

#### Scenario: Unavailable server
- **WHEN** mcparr is unavailable for a turn
- **THEN** the turn's size line has no tool or instruction sizes for mcparr

#### Scenario: Nothing private logged
- **WHEN** a turn's size line is logged
- **THEN** it contains sizes and names only, not the text of the history, the prompt, the facts or the tool definitions

#### Scenario: Facts in the context
- **WHEN** facts in the context are on and the turn's user has 14 facts taking 1240 characters
- **THEN** the size line says the system prompt holds 14 facts in 1240 characters

#### Scenario: Facts off
- **WHEN** facts in the context are off
- **THEN** the size line says facts are off

#### Scenario: No facts yet
- **WHEN** facts in the context are on and the turn's user has no facts
- **THEN** the size line says the user has no facts

#### Scenario: Facts over budget
- **WHEN** the turn's user has 52 facts and 40 fit the budget
- **THEN** the size line says 40 of 52 facts are sent, with their size and that 12 were left out
