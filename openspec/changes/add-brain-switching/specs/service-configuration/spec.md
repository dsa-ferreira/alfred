# Spec Delta

## MODIFIED Requirements

### Requirement: All tunables in the file
Every non-secret setting SHALL be settable in the config file, including the owner user ID, model base URL and name, the brains the owner can switch between, request timeout, maximum model requests per turn, persona, the owner's form of address, the owner's timezone, context window size, retry delays, listen host and port, log level, services and MCP servers, and the conversation memory settings: summary model, embedding service URL, model and text prefixes, recall and summariser size caps, and the number of search results. Settings with defaults SHALL be optional.

#### Scenario: Minimal file
- **WHEN** the file sets only the owner user ID, model, services and MCP servers
- **THEN** every other setting takes its documented default

#### Scenario: Change a limit without code
- **WHEN** the context window size is changed from 30 to 50 in the file and alfred restarts
- **THEN** the model receives up to 50 entries

#### Scenario: Old system prompt key
- **WHEN** the file still sets the removed `system_prompt` key under `[ai]`
- **THEN** alfred exits at startup with an error naming the unknown key

#### Scenario: Summary model defaults to the chat model
- **WHEN** the file has no `[memory]` section
- **THEN** conversations are summarised with the model set in `[ai]`, and embeddings come from `http://ragman.lan` with `nomic-embed-text:v1.5`

#### Scenario: Default timezone
- **WHEN** the file does not set `[ai] timezone`
- **THEN** dates are given in `Europe/Lisbon`

#### Scenario: Unknown timezone
- **WHEN** `[ai] timezone` is set to `Europe/Lisboa`
- **THEN** alfred exits at startup with an error naming `ai.timezone`

#### Scenario: Change the recall cap
- **WHEN** `[memory] recall_max_chars` is set to 16000 and alfred restarts
- **THEN** recall entries are at most 16,000 characters

#### Scenario: Brains default to none
- **WHEN** the file has no `[ai.models]` table
- **THEN** alfred starts with only the `[ai] model` as a brain

## ADDED Requirements

### Requirement: Brain list validation
Each `[ai.models]` key SHALL be lowercase letters, digits and hyphens, and each value a non-empty model tag. Two aliases naming the same tag, or an alias equal to another brain's name, SHALL be startup errors naming `ai.models`.

#### Scenario: Bad alias
- **WHEN** `[ai.models]` has `Gemma = "gemma4:26b-a4b-it-qat"`
- **THEN** alfred exits at startup with an error naming `ai.models` and the alias

#### Scenario: Same tag twice
- **WHEN** `[ai.models]` has `gemma` and `g4` both set to `gemma4:26b-a4b-it-qat`
- **THEN** alfred exits at startup with an error naming both aliases

#### Scenario: Empty tag
- **WHEN** `[ai.models]` has `bonsai = ""`
- **THEN** alfred exits at startup with an error naming `ai.models.bonsai`
