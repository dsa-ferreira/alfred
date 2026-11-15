# conversation-memory Specification

## Purpose

Lets the owner go back to earlier conversations: each archived conversation gets a title and a short summary, the model can search archived conversations by meaning or keywords, and it can bring one back into the current conversation.

## Requirements

### Requirement: Summary of an ended conversation
When a conversation is archived, alfred SHALL ask the model for a short title and a summary of one or two sentences, and store both with the conversation. The model SHALL receive at most the configured number of characters of the conversation's transcript.

#### Scenario: Conversation ends
- **WHEN** the owner ends a conversation about a lemon-garlic chicken recipe
- **THEN** that conversation is stored with a title such as "Chicken thighs recipe" and a one- or two-sentence summary

#### Scenario: Very long conversation
- **WHEN** an ended conversation's transcript is longer than the configured summariser input limit
- **THEN** the model receives a shortened transcript marked as shortened, and the conversation is still summarised

### Requirement: Summary failures are retried in the background
If summarising or embedding an archived conversation fails, alfred SHALL retry in the background with backoff until it succeeds, without sending the owner any message. Background retries SHALL NOT delay owner messages or `/ask` prompts in the queue.

#### Scenario: Model fails at reset
- **WHEN** the summary request fails when a conversation ends
- **THEN** the conversation is summarised later without a message to the owner, and owner messages keep being answered meanwhile

#### Scenario: Conversations from before the upgrade
- **WHEN** alfred starts with archived conversations that have no summary
- **THEN** it summarises them in the background

### Requirement: Search archived conversations
In turns started by a user's message, the model SHALL be offered `alfred_search_conversations` when the user's allow-list matches it, taking an optional free-text query and optional dates. It SHALL search only that user's archived conversations and return at most the configured number, best match first, each with its id, title, start and end dates and summary. With a query, it SHALL match by meaning (title and summary) and by keywords (title, summary and the conversation's entries).

#### Scenario: Search by topic
- **WHEN** the user asks "remember when we talked about the new chicken recipe?" and their conversation #41 is titled "Chicken thighs recipe"
- **THEN** a search for "chicken recipe" returns #41 among its results, with its id, title, dates and summary

#### Scenario: Keyword only in the entries
- **WHEN** one of the user's conversations mentioned "sous vide" only in one user message, not in its title or summary
- **THEN** a search for "sous vide" returns that conversation

#### Scenario: Current conversation excluded
- **WHEN** the current conversation also talks about chicken
- **THEN** search results contain only archived conversations

#### Scenario: Another user's conversation
- **WHEN** `diogo` has an archived conversation about chicken and `ana` searches for "chicken"
- **THEN** `diogo`'s conversation is not among `ana`'s results

### Requirement: Search by date
`alfred_search_conversations` SHALL accept optional `after` and `before` dates (`YYYY-MM-DD`, inclusive, in the configured timezone). When given, it SHALL return only conversations whose time span, from start to end, overlaps that range. Dates in search results SHALL be shown in the configured timezone.

#### Scenario: Topic and date
- **WHEN** on Monday 9 November 2026 the owner asks "remember when we talked about the chicken recipe last week?", and there are chicken conversations from 3 and 5 November and from 10 October
- **THEN** the model can search for "chicken recipe" with `after` 2026-11-02 and `before` 2026-11-08, and only the 3 and 5 November conversations are returned

#### Scenario: Conversation spanning the range edge
- **WHEN** a conversation started on 1 November and ended on 3 November, and the search has `after` 2026-11-02
- **THEN** that conversation is returned

### Requirement: Search by date alone
A search with dates and no query SHALL return the conversations in that range, newest first, up to the configured number of results. A search with neither a query nor dates SHALL return an error result asking for one of them.

#### Scenario: What did we talk about on Tuesday
- **WHEN** the owner asks "what did we talk about on Tuesday?" and two conversations overlap that Tuesday
- **THEN** a search with only `after` and `before` set to that Tuesday returns both, newest first, with their titles and summaries

#### Scenario: Empty search
- **WHEN** the model calls `alfred_search_conversations` with no query and no dates
- **THEN** the tool returns an error saying a query or a date range is needed

### Requirement: Search without the embedding service
When the embedding service is unreachable, search SHALL still return keyword matches, and the result SHALL tell the model that matching by meaning was unavailable.

#### Scenario: ragman down
- **WHEN** the embedding service refuses connections and the model searches for "chicken recipe"
- **THEN** the tool returns the keyword matches and a note that meaning-based matching was unavailable

### Requirement: Choosing among matches
The search tool's description SHALL tell the model to recall the best match when one result clearly matches better than the others and to say which conversation it picked, and to ask the owner which one they meant when several match about equally.

#### Scenario: Close matches
- **WHEN** a search returns two chicken conversations that match about equally
- **THEN** the tool description the model received tells it to ask the owner which one they meant

### Requirement: Recall a conversation
In turns started by a user's message, the model SHALL be offered `alfred_recall_conversation` when the user's allow-list matches it, taking a conversation id. Recalling one of that user's archived conversations SHALL add a recall entry to their current conversation containing the recalled conversation's id, title, dates and transcript, and SHALL return that content as the tool result.

#### Scenario: Recall by id
- **WHEN** the user says "remember conversation 41" and #41 is theirs
- **THEN** the model can call `alfred_recall_conversation` with 41, and conversation #41's transcript is added to the current conversation

#### Scenario: Follow-up after recall
- **WHEN** #41 was recalled and the user's next message is "and how long in the oven?"
- **THEN** that turn's context still contains #41's transcript

### Requirement: Recall refusals
Recalling an unknown id, another user's conversation, the current conversation, or a conversation already recalled into the current conversation SHALL add no entry and SHALL return an error result saying why. Another user's conversation SHALL be refused exactly as an unknown id is, so its existence is not revealed.

#### Scenario: Unknown id
- **WHEN** the model recalls conversation 9999, which does not exist
- **THEN** the tool returns an error saying there is no such conversation, and nothing is added

#### Scenario: Another user's conversation
- **WHEN** on `ana`'s turn the model recalls #41, which is `diogo`'s
- **THEN** the tool returns the same error as for an unknown id, and nothing is added

#### Scenario: Recalled twice
- **WHEN** #41 was already recalled into the current conversation and the model recalls #41 again
- **THEN** the tool returns an error saying it is already in this conversation, and nothing is added

### Requirement: Recall size cap
A recall entry's transcript SHALL be at most the configured number of characters. A longer conversation SHALL be represented by its summary followed by the end of its transcript that fits, marked as having its earlier part omitted.

#### Scenario: Long conversation recalled
- **WHEN** a conversation whose transcript is 50,000 characters is recalled with a 32,000-character cap
- **THEN** the recall entry contains the summary, an omission marker and the transcript's last part, within the cap

### Requirement: Memory tools only in owner turns
alfred SHALL NOT offer `alfred_search_conversations` or `alfred_recall_conversation` in `/ask` turns.

#### Scenario: Ask turn
- **WHEN** an `/ask` turn runs
- **THEN** neither memory tool is among the offered tools

### Requirement: Summary request token counts are logged
After each successful summary request, alfred SHALL log at INFO level the prompt and completion token counts the model server reported, with the conversation's id. If the reply has no token counts, the line SHALL say they were not reported, and the summary SHALL be stored normally.

#### Scenario: Conversation summarised
- **WHEN** conversation #41 is summarised and the server reports 3120 prompt tokens and 58 completion tokens
- **THEN** the log has a line for conversation #41 with 3120 prompt tokens and 58 completion tokens

#### Scenario: Server without token counts
- **WHEN** the summary reply has no token counts
- **THEN** the log line says they were not reported and the title and summary are stored
