# Spec Delta

## Purpose

Lets the owner switch, from the chat, which configured model ("brain") alfred's turns use, ask which one is in use, and fall back safely to the startup brain when the chosen one fails.

## ADDED Requirements

### Requirement: Configured brains
The brains SHALL be the `[ai.models]` aliases, each naming a model tag, plus the `[ai] model` tag under its own tag as name when no alias names it. The startup brain SHALL be the one whose tag is `[ai] model`.

#### Scenario: Startup model has an alias
- **WHEN** `[ai] model = "gemma4:26b-a4b-it-qat"` and `[ai.models]` has `gemma = "gemma4:26b-a4b-it-qat"` and `bonsai = "bonsai:8b"`
- **THEN** the brains are `gemma` and `bonsai`, and alfred starts on `gemma`

#### Scenario: Startup model without an alias
- **WHEN** `[ai] model = "qwen3:14b"` and `[ai.models]` has only `bonsai = "bonsai:8b"`
- **THEN** the brains are `qwen3:14b` and `bonsai`, and alfred starts on `qwen3:14b`

#### Scenario: No list
- **WHEN** the file has no `[ai.models]`
- **THEN** the only brain is the `[ai] model` tag

### Requirement: Switching brains from the chat
In owner turns with at least two brains, the model SHALL be offered a tool that sets the current brain, accepting only a configured brain name. Its description SHALL say the owner may call it a model or a brain. The switch SHALL apply from the next turn.

#### Scenario: Switch by alias
- **WHEN** the brains are `gemma` and `bonsai`, alfred is on `gemma`, and the owner says "set your brain to bonsai"
- **THEN** the model calls the tool with `bonsai`, this turn's reply is written by `gemma`, and the owner's next message is answered by `bonsai`

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

### Requirement: Current brain is not persisted
The current brain SHALL be kept only in memory. When alfred starts, the current brain SHALL be the startup brain.

#### Scenario: Restart
- **WHEN** the owner switched to `bonsai` and alfred is restarted
- **THEN** turns use the startup brain again

### Requirement: Model knows the current brain
The system prompt of every turn SHALL name the current brain and its model tag, and the other configured brains, so the model can tell the owner which brain is in use.

#### Scenario: Owner asks
- **WHEN** alfred is on `bonsai` (`bonsai:8b`), the other brain is `gemma`, and the owner asks "which brain are you using?"
- **THEN** the system prompt the model receives says it is running on `bonsai` (`bonsai:8b`) and that `gemma` is also available

#### Scenario: After a switch
- **WHEN** the owner switched from `gemma` to `bonsai` in the previous turn
- **THEN** the next turn's system prompt names `bonsai` as the current brain

### Requirement: Summaries ignore the chosen brain
Conversation summaries SHALL use the configured summary model (`[memory] summary_model`, else `[ai] model`), whatever the current brain is.

#### Scenario: Ending a conversation on another brain
- **WHEN** alfred is on `bonsai`, `[ai] model` is `gemma4:26b-a4b-it-qat`, no summary model is set, and the owner ends the conversation
- **THEN** the conversation is summarised with `gemma4:26b-a4b-it-qat`

### Requirement: Fallback to the startup brain
When a model request on a brain other than the startup brain fails with a non-retryable model error, alfred SHALL make the startup brain current and send the owner the fallback notice. An unreachable AI server SHALL NOT trigger the fallback.

#### Scenario: Model can't be used
- **WHEN** alfred is on `bonsai` and the model server answers HTTP 400 that the model does not support tools
- **THEN** the current brain becomes the startup brain and the owner receives the fallback notice naming `bonsai` and the startup brain

#### Scenario: AI server unreachable
- **WHEN** alfred is on `bonsai` and the model server can't be reached
- **THEN** the current brain stays `bonsai` and the turn is retried as before

#### Scenario: Startup brain fails
- **WHEN** alfred is on the startup brain and a model request fails with a non-retryable error
- **THEN** the turn fails as before, with no fallback notice

#### Scenario: Ask turn falls back
- **WHEN** an `/ask` turn on `bonsai` hits a non-retryable model error
- **THEN** the startup brain becomes current and the owner receives the fallback notice

### Requirement: Turn runs again after a fallback
After a fallback, if no tool had run in that turn, alfred SHALL run the turn again from the start on the startup brain. If a tool had run, the turn SHALL fail with the usual failure notice. A turn SHALL fall back at most once.

#### Scenario: Before any tool
- **WHEN** the first model request of an owner turn on `bonsai` fails with a non-retryable error
- **THEN** the owner receives the fallback notice and then the startup brain's answer to the same message

#### Scenario: After a tool ran
- **WHEN** a model request on `bonsai` fails with a non-retryable error after a tool ran in that turn
- **THEN** the owner receives the fallback notice and the failure notice saying some actions may have partly completed, and the turn is not run again

#### Scenario: Startup brain fails too
- **WHEN** a turn runs again after a fallback and the startup brain's request also fails with a non-retryable error
- **THEN** the turn fails with the usual failure notice

### Requirement: Brain changes are logged
alfred SHALL log at INFO level each switch, with the old and new brain, and at WARNING level each fallback, with the failed brain, the startup brain and the model error. Each turn's log SHALL name the brain it ran on.

#### Scenario: Switch logged
- **WHEN** the owner switches from `gemma` to `bonsai`
- **THEN** the log has a line naming `gemma` and `bonsai`

#### Scenario: Fallback logged
- **WHEN** a request on `bonsai` fails and alfred falls back to `gemma`
- **THEN** the log has a warning naming `bonsai`, `gemma` and the model error
