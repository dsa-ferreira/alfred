# Spec Delta

## MODIFIED Requirements

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
