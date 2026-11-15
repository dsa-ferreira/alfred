# Spec Delta

## MODIFIED Requirements

### Requirement: All tunables in the file
Every non-secret setting SHALL be settable in the config file, including the users (see `users`), model base URL and name, the brains the admin can switch between, request timeout, maximum model requests per turn, persona, the timezone, context window size, the idle close time, retry delays, listen host and port, log level, services and MCP servers, the conversation memory settings: summary model, embedding service URL, model and text prefixes, recall and summariser size caps, the number of search results, and the quiet period before summaries, and the user facts settings: whether facts are in the context, the facts budget, the fact size, the extraction attempts before giving up, and the duplicate distance, and the web settings: SearXNG URL, number of web search results, fetch chunk size, fetch timeout and fetch download cap, and the reminder settings: default time for a date without a clock time, and the due-reminder check interval, and the attachment settings: object store endpoint, bucket and region, file size limit, inline window, first-page text size, document read size, image size for the model, and album wait. Settings with defaults SHALL be optional.

#### Scenario: Minimal file
- **WHEN** the file sets only one user, the model, services and MCP servers
- **THEN** every other setting takes its documented default

#### Scenario: Change a limit without code
- **WHEN** the context window size is changed from 30 to 50 in the file and alfred restarts
- **THEN** the model receives up to 50 entries

#### Scenario: Old owner keys
- **WHEN** the file still sets `[telegram] owner_user_id` or `[ai] owner_address`
- **THEN** alfred exits at startup with an error naming the unknown key

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

#### Scenario: Idle close off by default
- **WHEN** the file does not set `[history] idle_close_after_h`
- **THEN** conversations are never closed for inactivity

#### Scenario: Set the idle close time
- **WHEN** `[history] idle_close_after_h` is set to 6 and alfred restarts
- **THEN** a conversation is closed for inactivity 6 hours after its user's last message

#### Scenario: Invalid idle close time
- **WHEN** `[history] idle_close_after_h` is set to 0
- **THEN** alfred exits at startup with an error naming `history.idle_close_after_h`

#### Scenario: Default quiet period
- **WHEN** the file does not set `[memory] summary_quiet_s`
- **THEN** summaries start only after the queue has been quiet for 60 seconds

#### Scenario: Fact defaults
- **WHEN** the file sets none of the `[memory]` fact settings
- **THEN** facts are learned and can be remembered and listed, but are not in the context; when turned on, the facts budget is 3000 characters; a fact is at most 300 characters; extraction is never given up; and facts within a cosine distance of 0.1 are duplicates

#### Scenario: Turn facts on
- **WHEN** `[memory] facts_in_context` is set to true and alfred restarts
- **THEN** every turn's system prompt carries its user's facts

#### Scenario: Give up on extraction
- **WHEN** `[memory] facts_extraction_max_attempts` is set to 5 and alfred restarts
- **THEN** a conversation whose extraction fails 5 times is given up

#### Scenario: Invalid duplicate distance
- **WHEN** `[memory] facts_duplicate_max_distance` is set to 3
- **THEN** alfred exits at startup with an error naming `memory.facts_duplicate_max_distance`

