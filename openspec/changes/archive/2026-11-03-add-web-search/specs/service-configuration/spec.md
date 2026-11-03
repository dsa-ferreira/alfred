# Spec Delta

## MODIFIED Requirements

### Requirement: All tunables in the file
Every non-secret setting SHALL be settable in the config file, including the owner user ID, model base URL and name, the brains the owner can switch between, request timeout, maximum model requests per turn, persona, the owner's form of address, the owner's timezone, context window size, retry delays, listen host and port, log level, services and MCP servers, the conversation memory settings: summary model, embedding service URL, model and text prefixes, recall and summariser size caps, and the number of search results, and the web settings: SearXNG URL, number of web search results, fetch chunk size, fetch timeout and fetch download cap. Settings with defaults SHALL be optional.

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

#### Scenario: Web defaults
- **WHEN** the file has no `[web]` section
- **THEN** web search uses `http://oracle.lan` with 5 results, and fetch returns 8000-character chunks with a 20-second timeout and a 5 MB download cap

#### Scenario: Change the fetch chunk size
- **WHEN** `[web] fetch_page_chars` is set to 4000 and alfred restarts
- **THEN** fetch results carry at most 4000 characters of page text

#### Scenario: Invalid SearXNG URL
- **WHEN** `[web] searxng_url` is set to `oracle.lan`
- **THEN** alfred exits at startup with an error naming `web.searxng_url`
