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
Every non-secret setting SHALL be settable in the config file, including the owner user ID, model base URL and name, request timeout, maximum model requests per turn, system prompt, context window size, retry delays, listen host and port, log level, services and MCP servers. Settings with defaults SHALL be optional.

#### Scenario: Minimal file
- **WHEN** the file sets only the owner user ID, model, services and MCP servers
- **THEN** every other setting takes its documented default

#### Scenario: Change a limit without code
- **WHEN** the context window size is changed from 30 to 50 in the file and alfred restarts
- **THEN** the model receives up to 50 entries

### Requirement: Secrets only from the environment
Secrets SHALL be accepted only from environment variables: the Telegram bot token (`ALFRED_TELEGRAM_BOT_TOKEN`), the database URL (`ALFRED_DATABASE_URL`), and the tokens of services and MCP servers. Services and MCP servers SHALL name their secret's variable with `token_env`.

#### Scenario: Token reference
- **WHEN** the mcparr entry has `token_env = "ALFRED_MCP_TOKEN_MCPARR"` and that variable is set
- **THEN** alfred uses its value as mcparr's bearer token

#### Scenario: Referenced variable missing
- **WHEN** a `token_env` names a variable that is unset or empty
- **THEN** alfred exits at startup with an error naming the variable and the entry that references it

### Requirement: Strict validation
Configuration SHALL be validated once at startup and all problems reported together. Unknown keys, wrong types, invalid URLs, duplicate service or server names, duplicate service tokens and the reserved MCP server name `alfred` SHALL be errors. Server and service names SHALL be lowercase letters, digits and hyphens.

#### Scenario: Typo in a key
- **WHEN** the file contains `max_mesages = 30`
- **THEN** alfred exits with an error naming the unknown key

#### Scenario: Several problems
- **WHEN** the file has an invalid URL and a duplicate server name
- **THEN** a single startup error lists both problems

### Requirement: Errors never contain secrets
Configuration errors and logs SHALL never include the value of a secret: tokens, the bot token, or the database password.

#### Scenario: Duplicate service tokens
- **WHEN** two services' token variables hold the same value
- **THEN** the startup error names both services and does not include the token
