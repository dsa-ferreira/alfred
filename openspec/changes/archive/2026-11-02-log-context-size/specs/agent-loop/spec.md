# Spec Delta

## ADDED Requirements

### Requirement: Turn context size is logged
At the start of each turn, alfred SHALL log one line at INFO level with the size, in characters, of the context it sends: the system prompt and each server's instructions in it, the tool definitions in total, per MCP server and for alfred's own tools, and the history. The line SHALL identify the turn and SHALL NOT include message or prompt text.

#### Scenario: Owner turn with two servers
- **WHEN** an owner turn runs with cookbook and mcparr available and 30 history entries
- **THEN** the log has one line for that turn giving the system prompt size, cookbook's and mcparr's instruction sizes, the tool definition sizes for cookbook, mcparr and alfred's own tools with their total and tool count, and the size of the 30 history entries

#### Scenario: Unavailable server
- **WHEN** mcparr is unavailable for a turn
- **THEN** the turn's size line has no tool or instruction sizes for mcparr

#### Scenario: Nothing private logged
- **WHEN** a turn's size line is logged
- **THEN** it contains sizes and names only, not the text of the history, the prompt or the tool definitions

### Requirement: Largest tool definitions are logged at DEBUG
When the log level is DEBUG, each turn SHALL also log its five largest tool definitions by size, each with its model-facing name and size in characters, largest first.

#### Scenario: Debug logging
- **WHEN** the log level is DEBUG and a turn offers 40 tools
- **THEN** the log lists that turn's five largest tool definitions by name and size, largest first

#### Scenario: Info logging
- **WHEN** the log level is INFO
- **THEN** the largest tool definitions are not logged

### Requirement: Model request token counts are logged
After each model request in a turn, alfred SHALL log at INFO level the prompt and completion token counts the model server reported, with the turn and the request's number within the turn. If the reply has no token counts, the line SHALL say they were not reported, and the turn SHALL continue normally.

#### Scenario: Tool-using turn
- **WHEN** a turn makes two model requests and the server reports 6812 then 7240 prompt tokens
- **THEN** the log has a line for request 1 with 6812 prompt tokens and a line for request 2 with 7240 prompt tokens, each with its completion token count

#### Scenario: Server without token counts
- **WHEN** the model server's reply has no token counts
- **THEN** the request's line says the token counts were not reported and the turn's answer is unaffected
