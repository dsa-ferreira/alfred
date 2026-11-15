# Spec Delta

## MODIFIED Requirements

### Requirement: Facts are learned from archived conversations
When a conversation is archived, alfred SHALL, in the background, ask the model for durable facts about its user from that conversation: stable preferences, identity, goals, ongoing projects, constraints and relationships, not one-off task details. A fact SHALL be asked for only if knowing it would change how alfred helps the user in a later conversation about something else. Instructions to alfred and their settings (reminders, briefings, recipes, lookups), how the user refers to people or things, and what was discussed or done in the conversation SHALL NOT be asked for as facts. The model SHALL also receive that user's current facts with their ids and origins, and at most the configured number of characters of the conversation.

#### Scenario: Allergy mentioned in passing
- **WHEN** `diogo` says "skip the prawns, I'm allergic to shellfish" in conversation #41, and #41 is archived
- **THEN** `diogo` later has a `learned` fact saying he is allergic to shellfish

#### Scenario: Nothing durable
- **WHEN** an archived conversation only asked for the time and a unit conversion
- **THEN** no fact is stored for it, and its extraction counts as done

#### Scenario: Changing a reminder
- **WHEN** `diogo`'s archived conversation only moved his weekday briefing to 06:45 with three news stories
- **THEN** the extraction request tells the model that setting up or changing reminders and briefings is not a fact

#### Scenario: How someone is named
- **WHEN** `ana` only says she calls someone "Teach"
- **THEN** the extraction request tells the model that how the user refers to someone is not a fact unless the relationship is stated

### Requirement: Extraction replaces outdated learned facts
The extraction request SHALL be able to say that a new fact replaces an existing `learned` fact, by its id and a quote of its text; that fact SHALL be removed when the new one is stored. A replacement whose quote doesn't match the text of the fact with that id, or that names a `told` fact, another user's fact, a removed fact or an unknown id, SHALL be ignored, and the new fact stored as a plain addition. Every new fact the model returns SHALL be stored; alfred SHALL NOT cap or choose among them.

#### Scenario: Moved house
- **WHEN** `diogo` has the `learned` fact 7 "lives in Lisbon" and conversation #42 says he moved to Porto, and the model replaces 7 quoting "lives in Lisbon"
- **THEN** after #42's extraction he has a fact saying he lives in Porto, and "lives in Lisbon" is gone

#### Scenario: Told facts are kept
- **WHEN** `diogo` has the `told` fact "vegetarian" and an extraction tries to replace it with "eats fish"
- **THEN** "vegetarian" is kept, and "eats fish" is stored as a new `learned` fact

#### Scenario: Wrong id
- **WHEN** `diogo` has the `learned` fact 5 "is adding low-calorie recipes to his cookbook", and the model replaces 5 quoting "lives in Lisbon" with "lives in Lisbon, Portugal"
- **THEN** fact 5 is kept, "lives in Lisbon, Portugal" is stored as a new fact, and the refused replacement is logged without either text

#### Scenario: No quote
- **WHEN** the model replaces fact 7 without quoting its text
- **THEN** fact 7 is kept and the new fact is stored as a plain addition

### Requirement: Extraction outcome is logged
After each extraction, alfred SHALL log at INFO level the conversation's id and how many facts were added and replaced, how many replacements were refused, and the prompt and completion token counts the model server reported, or that they were not reported. The log SHALL NOT contain fact or conversation text.

#### Scenario: Two new facts, one replaced
- **WHEN** #42's extraction adds two facts, one of which replaces a `learned` fact
- **THEN** the log has a line for #42 saying 2 added, 1 replaced and 0 refused, with the token counts and no fact text

#### Scenario: Refused replacement
- **WHEN** #43's extraction adds one fact whose replacement quote doesn't match
- **THEN** the log line for #43 says 1 added, 0 replaced and 1 refused

### Requirement: Near-duplicate facts are removed
When a fact is stored, alfred SHALL, in the background, find the same user's other facts within the configured distance using the embedding service, and ask the model which of them say the same thing as the new fact. Only those the model names SHALL be removed as duplicates, keeping a `told` fact over a `learned` one, otherwise the older one. A fact on the same topic, one that adds detail, or one that contradicts it SHALL NOT be removed as a duplicate. Each removed duplicate SHALL be logged at INFO level with both ids.

#### Scenario: Same fact, different words
- **WHEN** `diogo` has "allergic to shellfish", extraction stores "the user is allergic to shellfish", and the model says they are the same
- **THEN** the newer fact is removed as a duplicate of the older and the log names both ids

#### Scenario: Told over learned
- **WHEN** `diogo` has the `learned` fact "prefers metric units", asks alfred to remember "always use metric units", and the model says they are the same
- **THEN** the `told` fact is kept and the `learned` one is removed as its duplicate

#### Scenario: Close but different
- **WHEN** `ana` has "likes Teach very much", extraction stores "has a person she refers to as Teach", the two are within the configured distance, and the model says they are not the same
- **THEN** both facts are kept

#### Scenario: Other users not compared
- **WHEN** `ana` stores "allergic to shellfish" and `diogo` already has it
- **THEN** both users keep their fact

#### Scenario: Nothing close
- **WHEN** a new fact has no other fact of its user within the configured distance
- **THEN** it is kept and no model request is made for it

### Requirement: Facts without the embedding service
When the embedding service is unreachable, new facts SHALL still be stored, and they SHALL be checked for duplicates once the service answers again. Facts embedded with another model than the configured one SHALL be embedded again.

#### Scenario: ragman down
- **WHEN** the embedding service is down and `diogo` asks alfred to remember "my car is electric"
- **THEN** the fact is stored and in use right away, and checked for duplicates later

### Requirement: Remember a fact
In turns started by a user's message, the model SHALL be offered `alfred_remember` when the user's allow-list matches it, taking the fact's text and optional ids of the user's facts it replaces. It SHALL store a `told` fact for that user, remove the replaced facts, and return the new fact's id. Its duplicate check SHALL happen in the background, not in the turn. A text longer than the configured fact size SHALL be refused with an error result.

#### Scenario: Remember in natural language
- **WHEN** `diogo` says "remember that I'm vegetarian now"
- **THEN** the model calls `alfred_remember` and `diogo` has a `told` fact saying he is vegetarian

#### Scenario: Correction
- **WHEN** `diogo` has fact 7 "lives in Lisbon" and says "actually I moved to Porto"
- **THEN** the model can call `alfred_remember` with the new text replacing 7, and fact 7 is gone

#### Scenario: Replacing another user's fact
- **WHEN** `ana`'s turn calls `alfred_remember` replacing an id that belongs to `diogo`
- **THEN** the result is an error saying that id was not found, as for an unknown id, and nothing is stored or removed

#### Scenario: Remembering something already known
- **WHEN** `diogo` already has "allergic to shellfish" and asks alfred to remember it again
- **THEN** the tool saves the new fact and returns its id without a second model request in the turn, and the duplicate is resolved later in the background

## ADDED Requirements

### Requirement: Duplicate checks wait for a quiet queue
alfred SHALL start a duplicate-check model request only when no queue item is waiting or running and none has finished within the configured quiet period. A queue item that arrives while one runs SHALL cancel it; the cancellation SHALL NOT count as a failed attempt, and nothing from it SHALL be recorded.

#### Scenario: Message during a duplicate check
- **WHEN** a duplicate check for fact 88 is running and `ana` sends a message
- **THEN** the check is cancelled, `ana`'s turn runs, and fact 88 is checked again after the next quiet period with no failed attempt recorded

### Requirement: Duplicate-check failures are retried
If a duplicate-check request fails or its reply can't be read, alfred SHALL log a WARNING with the fact's id and the number of failed attempts, keep the fact in use, and retry in the background with backoff, without messaging the user. It SHALL NOT give up.

#### Scenario: Unreadable reply
- **WHEN** the model's reply to fact 88's duplicate check can't be read for the second time
- **THEN** a WARNING names fact 88 and 2 failed attempts, fact 88 stays in the user's facts, and it is checked again later

### Requirement: Duplicate check outcome is logged
After each duplicate check that made a model request, alfred SHALL log at INFO level the fact's id, how many facts were compared and how many were removed as duplicates, and the token counts the model server reported, or that they were not reported. The log SHALL NOT contain fact text.

#### Scenario: One duplicate among three
- **WHEN** fact 88 is compared with three close facts and one is the same
- **THEN** the log has a line for fact 88 saying 3 compared and 1 duplicate, with the token counts and no fact text

### Requirement: Existing facts are not checked again
Facts stored before the duplicate check was confirmed by the model, including ones the owner restores later, SHALL count as already checked.

#### Scenario: Upgrade
- **WHEN** alfred starts after the upgrade with 30 facts
- **THEN** no duplicate check runs for any of them
