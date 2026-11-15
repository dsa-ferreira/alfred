# Spec Delta

## MODIFIED Requirements

### Requirement: Switching brains from the chat
In turns started by the admin's message, with at least two brains and the admin's allow-list matching it, the model SHALL be offered a tool that sets the current brain, accepting only a configured brain name. Its description SHALL say the user may call it a model or a brain. The switch SHALL apply alfred-wide from the next turn, for every user. The tool SHALL NOT be offered on any other user's turns, whatever their allow-list.

#### Scenario: Switch by alias
- **WHEN** the brains are `gemma` and `bonsai`, alfred is on `gemma`, and the admin says "set your brain to bonsai"
- **THEN** the model calls the tool with `bonsai`, this turn's reply is written by `gemma`, and the next turn of any user is answered by `bonsai`

#### Scenario: Unknown brain
- **WHEN** the model calls the tool with a name that is not a configured brain
- **THEN** the current brain is unchanged and the tool result is an error listing the configured brains

#### Scenario: Already current
- **WHEN** the model calls the tool with the current brain
- **THEN** the current brain is unchanged and the tool result says it was already in use

#### Scenario: Only one brain
- **WHEN** there is only one brain
- **THEN** the tool is not offered

#### Scenario: Ask turns can't switch
- **WHEN** an `/ask` turn runs
- **THEN** the tool is not offered

#### Scenario: Not the admin
- **WHEN** `ana` is not the admin, her allow-list is `["*"]`, and she says "switch to bonsai"
- **THEN** the tool is not offered on her turn and the current brain is unchanged

### Requirement: Model knows the current brain
The system prompt of every turn SHALL name the current brain and its model tag. When the brain tool is offered on the turn, it SHALL also name the other configured brains, so the model can tell the admin which brains are available.

#### Scenario: Owner asks
- **WHEN** alfred is on `bonsai` (`bonsai:8b`), the other brain is `gemma`, and the admin asks "which brain are you using?"
- **THEN** the system prompt the model receives says it is running on `bonsai` (`bonsai:8b`) and that `gemma` is also available

#### Scenario: Another user asks
- **WHEN** alfred is on `bonsai` (`bonsai:8b`) and `ana`, who is not the admin, asks the same
- **THEN** the system prompt names `bonsai` (`bonsai:8b`) and no other brains

#### Scenario: After a switch
- **WHEN** the admin switched from `gemma` to `bonsai` in the previous turn
- **THEN** the next turn's system prompt names `bonsai` as the current brain

### Requirement: Fallback to the startup brain
When a model request on a brain other than the startup brain fails with a non-retryable model error, alfred SHALL make the startup brain current and send the fallback notice to the turn's user, and also to the admin when the turn is not the admin's. An unreachable AI server SHALL NOT trigger the fallback.

#### Scenario: Model can't be used
- **WHEN** alfred is on `bonsai` and the model server answers HTTP 400 that the model does not support tools
- **THEN** the current brain becomes the startup brain and the turn's user receives the fallback notice naming `bonsai` and the startup brain

#### Scenario: Fallback on another user's turn
- **WHEN** the fallback happens on `ana`'s turn
- **THEN** both `ana` and the admin receive the fallback notice

#### Scenario: AI server unreachable
- **WHEN** alfred is on `bonsai` and the model server can't be reached
- **THEN** the current brain stays `bonsai` and the turn is retried as before

#### Scenario: Startup brain fails
- **WHEN** alfred is on the startup brain and a model request fails with a non-retryable error
- **THEN** the turn fails as before, with no fallback notice

#### Scenario: Ask turn falls back
- **WHEN** an `/ask` turn for the admin on `bonsai` hits a non-retryable model error
- **THEN** the startup brain becomes current and the admin receives the fallback notice
