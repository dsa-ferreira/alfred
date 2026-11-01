# Spec Delta

## MODIFIED Requirements

### Requirement: All tunables in the file
Every non-secret setting SHALL be settable in the config file, including the owner user ID, model base URL and name, request timeout, maximum model requests per turn, persona, the owner's form of address, context window size, retry delays, listen host and port, log level, services and MCP servers. Settings with defaults SHALL be optional.

#### Scenario: Minimal file
- **WHEN** the file sets only the owner user ID, model, services and MCP servers
- **THEN** every other setting takes its documented default

#### Scenario: Change a limit without code
- **WHEN** the context window size is changed from 30 to 50 in the file and alfred restarts
- **THEN** the model receives up to 50 entries

#### Scenario: Old system prompt key
- **WHEN** the file still sets the removed `system_prompt` key under `[ai]`
- **THEN** alfred exits at startup with an error naming the unknown key
