# agent-loop Specification

## Purpose

Defines how alfred runs one AI turn: which model endpoint it calls, what instructions and context the model gets, and how tool calls are carried out until the model produces a final answer.

## Requirements

### Requirement: OpenAI-compatible model endpoint
alfred SHALL call the model through an OpenAI-compatible chat completions API, using the base URL and model name from configuration. Changing model servers SHALL need only a configuration change.

#### Scenario: Default Ollama endpoint
- **WHEN** the configuration sets base URL `http://ai.lan` and model `gemma4:12b-it-qat`
- **THEN** turns are sent to that endpoint's chat completions API with that model

#### Scenario: Switch servers
- **WHEN** the base URL and model are changed to another OpenAI-compatible server and alfred is restarted
- **THEN** turns go to the new server with no code change

### Requirement: Telegram-oriented system prompt
Every turn SHALL include a system prompt made of a persona, which sets alfred's voice and is configurable with a built-in default, followed by built-in operating rules that cannot be configured, followed by the instructions of the turn's available MCP servers. The operating rules SHALL tell the model it is chatting with its owner over Telegram, how to use tools, and how to read service notices, `/ask` messages and recalled conversations.

#### Scenario: Default prompt
- **WHEN** no custom persona is configured
- **THEN** the system prompt contains the built-in persona followed by the built-in operating rules

#### Scenario: Custom prompt
- **WHEN** a custom persona is configured
- **THEN** it replaces only the built-in persona, and the built-in operating rules are still sent after it

#### Scenario: Operating rules survive a custom persona
- **WHEN** a custom persona is configured and the owner asks about an earlier `[notice:backup]` message
- **THEN** the system prompt still explains that `[notice:<service>]` messages are notifications the owner has already seen

#### Scenario: Recalled conversation explained
- **WHEN** the current conversation contains a recall of conversation #41
- **THEN** the system prompt explains that a `[recall:#<id> …]` message is an earlier conversation brought back on the owner's request, not something the owner just said

#### Scenario: Server instructions after the rules
- **WHEN** cookbook is available and sends instructions, and the turn is an `/ask` turn
- **THEN** the system prompt contains the persona, then the operating rules, then cookbook's instructions, then the `NO_REPLY` instruction

#### Scenario: Server instructions survive a custom persona
- **WHEN** a custom persona is configured and cookbook sends instructions
- **THEN** cookbook's instructions are still in the system prompt

### Requirement: Turn context
Each turn SHALL send the system prompt, the current conversation's recent history, and the new input. For an `/ask` turn, the input SHALL be labelled with the service name and the model told about the `NO_REPLY` convention.

#### Scenario: Ask turn
- **WHEN** service `disk-check` asks "is this worth telling the owner?"
- **THEN** the model receives the recent history, an input identifying `disk-check` as the sender, and the `NO_REPLY` instruction

### Requirement: Tool-call loop
When the model replies with tool calls, alfred SHALL run each call, send the results back to the model, and repeat until the model gives a final answer without tool calls.

#### Scenario: One tool call
- **WHEN** the owner asks "what's downloading?" and the model calls `mcparr_get_downloads`
- **THEN** alfred runs that tool, sends its result to the model, and the model's next answer is the reply

### Requirement: Tool errors go to the model
A tool call that fails (unknown tool, invalid arguments, server error, timeout) SHALL be reported to the model as that call's result, describing the error, so the model can recover or explain. It SHALL NOT end the turn.

#### Scenario: Tool fails
- **WHEN** `cookbook_get_recipe` returns an error that the recipe doesn't exist
- **THEN** the model receives that error as the tool result and can tell the owner

#### Scenario: Unknown tool
- **WHEN** the model calls a tool name alfred did not offer
- **THEN** the model receives an "unknown tool" error result

### Requirement: Turn limits
A turn SHALL stop after the configured maximum number of model requests. Each model request SHALL use the configured timeout, which SHALL default to a value long enough for the AI server to wake and load the model. A turn that hits the request limit SHALL fail with a notice saying it was stopped.

#### Scenario: Runaway tool use
- **WHEN** the model keeps calling tools past the maximum number of model requests
- **THEN** alfred stops the turn and the owner is told the request was stopped after too many steps

#### Scenario: Slow cold start
- **WHEN** the first model request takes 90 seconds because the AI server was asleep
- **THEN** the request completes normally within the default timeout

### Requirement: Turn transcript is logged
Each turn's tool calls (name, arguments, success or error) SHALL be logged and stored with the turn, without including any secret values.

#### Scenario: Audit a write
- **WHEN** a turn calls `mcparr_add_media`
- **THEN** the log and stored transcript show the tool name, arguments and outcome

### Requirement: Current date in the system prompt
The system prompt of every turn, owner and `/ask`, SHALL state the current date and weekday in the configured timezone. It SHALL NOT include the time of day, so the prompt changes at most once a day.

#### Scenario: Owner turn
- **WHEN** the owner sends a message on Monday 2 November 2026 at 14:05 Lisbon time, with the timezone set to `Europe/Lisbon`
- **THEN** the system prompt says that today is Monday, 2 November 2026, and contains no time of day

#### Scenario: Date changes at local midnight
- **WHEN** the timezone is `Europe/Lisbon` and a turn runs at 00:30 Lisbon time on 3 November 2026 (still 2 November in some other zones)
- **THEN** the system prompt says Tuesday, 3 November 2026

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** its system prompt also states the current date
