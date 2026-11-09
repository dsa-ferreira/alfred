# service-configuration Specification

## Purpose

Defines how alfred is configured: every non-secret setting in one TOML file, every secret only in `ALFRED_*` environment variables, all validated at startup so misconfiguration fails fast without leaking secrets.

## Requirements

### Requirement: Config file location
alfred SHALL read its non-secret configuration from a TOML file whose path is given by `ALFRED_CONFIG`, defaulting to `alfred.toml` in the working directory. A missing or unparseable file SHALL stop startup with an error naming the path.

#### Scenario: Custom path
- **WHEN** `ALFRED_CONFIG=/etc/alfred/alfred.toml` is set and that file exists
- **THEN** alfred loads that file

#### Scenario: Missing file
- **WHEN** the configured file does not exist
- **THEN** alfred exits with a non-zero status and an error naming the path

### Requirement: All tunables in the file
Every non-secret setting SHALL be settable in the config file, including the owner user ID, model base URL and name, the brains the owner can switch between, request timeout, maximum model requests per turn, persona, the owner's form of address, the owner's timezone, context window size, retry delays, listen host and port, log level, services and MCP servers, the conversation memory settings: summary model, embedding service URL, model and text prefixes, recall and summariser size caps, and the number of search results, and the web settings: SearXNG URL, number of web search results, fetch chunk size, fetch timeout and fetch download cap, and the reminder settings: default time for a date without a clock time, and the due-reminder check interval, and the attachment settings: object store endpoint, bucket and region, file size limit, inline window, first-page text size, document read size, image size for the model, and album wait. Settings with defaults SHALL be optional.

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

#### Scenario: Reminder defaults
- **WHEN** the file has no `[reminders]` section
- **THEN** a reminder for a date without a clock time is due at 09:00, and due reminders are checked at :00 and :30 of every minute

#### Scenario: Check interval too long
- **WHEN** `[reminders] check_interval_s` is set to 300
- **THEN** alfred exits at startup with an error naming `reminders.check_interval_s`

#### Scenario: Invalid default time
- **WHEN** `[reminders] default_time` is set to `9am`
- **THEN** alfred exits at startup with an error naming `reminders.default_time`

#### Scenario: Attachments off by default
- **WHEN** the file has no `[attachments]` section
- **THEN** alfred starts, accepts text only, and needs no object store credentials

#### Scenario: Attachment defaults
- **WHEN** `[attachments]` sets only `endpoint_url` and `bucket`
- **THEN** the region is `garage`, files up to 20 MB are accepted, attachments are inline only on the turn they arrive, the first PDF page sends up to 4000 characters, `alfred_read_document` returns up to 8000 characters, images reach the model at most 1536 pixels on their longer side, and an album is collected for 2 seconds

#### Scenario: Size limit above Telegram's
- **WHEN** `[attachments] max_bytes` is set to 50000000
- **THEN** alfred exits at startup with an error naming `attachments.max_bytes` and the 20 MB maximum

### Requirement: Secrets only from the environment
Secrets SHALL be accepted only from environment variables: the Telegram bot token (`ALFRED_TELEGRAM_BOT_TOKEN`), the database URL (`ALFRED_DATABASE_URL`), the object store key (`ALFRED_S3_ACCESS_KEY_ID` and `ALFRED_S3_SECRET_ACCESS_KEY`, required only when `[attachments]` is set), and the tokens of services and MCP servers. Services and MCP servers SHALL name their secret's variable with `token_env`.

#### Scenario: Token reference
- **WHEN** the mcparr entry has `token_env = "ALFRED_MCP_TOKEN_MCPARR"` and that variable is set
- **THEN** alfred uses its value as mcparr's bearer token

#### Scenario: Referenced variable missing
- **WHEN** a `token_env` names a variable that is unset or empty
- **THEN** alfred exits at startup with an error naming the variable and the entry that references it

#### Scenario: Object store key missing
- **WHEN** `[attachments]` is set and `ALFRED_S3_SECRET_ACCESS_KEY` is unset
- **THEN** alfred exits at startup with an error naming `ALFRED_S3_SECRET_ACCESS_KEY`

### Requirement: Strict validation
Configuration SHALL be validated once at startup and all problems reported together. Unknown keys, wrong types, invalid URLs, duplicate service or server names, duplicate service tokens and the reserved MCP server name `alfred` SHALL be errors. Server and service names SHALL be lowercase letters, digits and hyphens.

#### Scenario: Typo in a key
- **WHEN** the file contains `max_mesages = 30`
- **THEN** alfred exits with an error naming the unknown key

#### Scenario: Several problems
- **WHEN** the file has an invalid URL and a duplicate server name
- **THEN** a single startup error lists both problems

### Requirement: Errors never contain secrets
Configuration errors and logs SHALL never include the value of a secret: tokens, the bot token, the database password, or the object store secret key.

#### Scenario: Duplicate service tokens
- **WHEN** two services' token variables hold the same value
- **THEN** the startup error names both services and does not include the token

#### Scenario: Object store rejects the key
- **WHEN** the object store refuses alfred's credentials
- **THEN** the logged error does not include the secret key

### Requirement: Brain list validation
Each `[ai.models]` key SHALL be lowercase letters, digits and hyphens, and each value either a non-empty model tag or a table with a non-empty `model` and an optional boolean `vision`. Two aliases naming the same tag, or an alias equal to another brain's name, SHALL be startup errors naming `ai.models`.

#### Scenario: Bad alias
- **WHEN** `[ai.models]` has `Gemma = "gemma4:26b-a4b-it-qat"`
- **THEN** alfred exits at startup with an error naming `ai.models` and the alias

#### Scenario: Same tag twice
- **WHEN** `[ai.models]` has `gemma` and `g4` both set to `gemma4:26b-a4b-it-qat`
- **THEN** alfred exits at startup with an error naming both aliases

#### Scenario: Same tag twice, one as a table
- **WHEN** `[ai.models]` has `gemma = "gemma4:26b-a4b-it-qat"` and `g4 = { model = "gemma4:26b-a4b-it-qat", vision = true }`
- **THEN** alfred exits at startup with an error naming both aliases

#### Scenario: Empty tag
- **WHEN** `[ai.models]` has `bonsai = ""`
- **THEN** alfred exits at startup with an error naming `ai.models.bonsai`

#### Scenario: Unknown key in a brain table
- **WHEN** `[ai.models]` has `gemma = { model = "gemma4:26b-a4b-it-qat", vison = true }`
- **THEN** alfred exits at startup with an error naming `ai.models.gemma` and the unknown key
