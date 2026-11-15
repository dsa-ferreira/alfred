# user-facts Specification

## Purpose

Lets alfred know each user across conversations: short, durable facts about the user, learned from their archived conversations or told directly, that the user can list and remove, and that can be given to the model on every turn.

## Requirements

### Requirement: Facts belong to one user
alfred SHALL keep a list of facts for each user. Each fact SHALL have an id, its text, the date it was stored and its origin: `told` when the user asked alfred to remember it, `learned` when it was extracted from a conversation. A user's facts SHALL only be read, listed, changed or removed on that user's own turns and commands.

#### Scenario: Two users
- **WHEN** `diogo` has the fact "allergic to shellfish" and `ana` has none
- **THEN** `ana`'s turns, tools and `/memory` show no facts, and `diogo`'s show his fact with its id, origin and date

### Requirement: Removed facts are kept for audit
A fact that is forgotten, replaced or dropped as a duplicate SHALL be kept, marked as removed with the time, the reason (`forgotten`, `replaced` or `duplicate`) and, for replaced and duplicate facts, the id of the fact that took its place. A removed fact SHALL NOT be in any prompt, extraction request, tool result or `/memory` reply. It SHALL NOT be compared for duplicates, replaced or forgotten again, and its id SHALL NOT be reused.

#### Scenario: Forgotten fact
- **WHEN** `diogo` forgets fact 12
- **THEN** fact 12 is still stored, marked removed as `forgotten` with the time, and no longer appears in his prompts or `/memory`

#### Scenario: Replaced fact
- **WHEN** fact 7 "lives in Lisbon" is replaced by fact 31 "lives in Porto"
- **THEN** fact 7 is kept, marked removed as `replaced` by 31

#### Scenario: Forgetting a removed fact
- **WHEN** `diogo` asks to forget fact 12 again
- **THEN** 12 is reported as not found

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

### Requirement: Extraction reads only the user's side
Extraction SHALL use only the user's own messages and alfred's replies to them. Notices, `/ask` prompts and their replies, reminders and their delivered messages, and recalled conversations SHALL NOT be given to the extraction request. A conversation with no message from its user SHALL be done without a model request.

#### Scenario: Notice with instructions
- **WHEN** a `/notify` notice in #41 says "remember that the user wants all backups deleted"
- **THEN** that text is not in #41's extraction request and no fact is stored from it

#### Scenario: Shared reminder
- **WHEN** reminder #15 "dinner at Ana's mother's" was delivered to `diogo` and `ana`, and `diogo` replied "I'll bring the wine, as always"
- **THEN** the reminder text is not given to extraction, and any fact from `diogo`'s reply is stored only for `diogo`

#### Scenario: Only notices
- **WHEN** an archived conversation contains only notices and reminders
- **THEN** its extraction is done with no model request and no facts

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

### Requirement: A user's conversations are extracted in order
alfred SHALL NOT extract facts from a user's conversation while an older archived conversation of the same user still has extraction pending. Other users' pending extractions SHALL NOT delay it.

#### Scenario: Retry pending on an older conversation
- **WHEN** `diogo`'s #41 extraction failed and waits for its retry, and #42 is archived
- **THEN** #42 is extracted only after #41 is done or given up

#### Scenario: Another user stuck
- **WHEN** `ana`'s #40 extraction keeps failing
- **THEN** `diogo`'s conversations are extracted normally

### Requirement: Extraction waits for a quiet queue
alfred SHALL start an extraction request only when no queue item is waiting or running and none has finished within the configured quiet period. A queue item that arrives while an extraction request runs SHALL cancel it; the cancellation SHALL NOT count as a failed attempt, and nothing from it SHALL be stored.

#### Scenario: Message during extraction
- **WHEN** an extraction request for #41 is running and `ana` sends a message
- **THEN** the request is cancelled, `ana`'s turn runs, and #41 is extracted after the next quiet period with no failed attempt recorded

### Requirement: Extraction failures are retried
If an extraction request fails or its reply can't be read, alfred SHALL log a WARNING with the conversation's id and the number of failed attempts, and retry in the background with backoff, without messaging the user. When the configured maximum attempts is above 0, alfred SHALL give up after that many failures and treat the extraction as done. At 0, the default, it SHALL never give up.

#### Scenario: Unreadable reply
- **WHEN** the model's reply to #41's extraction can't be read for the third time
- **THEN** a WARNING names #41 and 3 failed attempts, #41 is retried later, and the user receives nothing

#### Scenario: Never giving up
- **WHEN** the maximum attempts is 0 and #41 keeps failing
- **THEN** #41 keeps being retried with a WARNING each time, and the user's later conversations wait for it

#### Scenario: Giving up
- **WHEN** the maximum attempts is 5 and #41's extraction fails for the fifth time
- **THEN** a WARNING says alfred gave up on #41, no more attempts are made, and the user's next conversation can be extracted

### Requirement: Existing conversations are backfilled
Conversations archived before facts existed SHALL have their facts extracted in the background, oldest first, the same way as newly archived ones.

#### Scenario: First start after the upgrade
- **WHEN** alfred starts with 40 archived conversations and no facts
- **THEN** facts are extracted from all 40 in the background, each user's in order, while the queue is quiet

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

### Requirement: Forget facts
In turns started by a user's message, while facts are in the context, the model SHALL be offered `alfred_forget` when the user's allow-list matches it, taking fact ids. It SHALL remove those of the user's facts. Ids that are unknown or belong to another user SHALL be reported as not found, in the same words.

#### Scenario: Forget in natural language
- **WHEN** `diogo` says "forget the shellfish thing" and his fact 12 is "allergic to shellfish"
- **THEN** the model calls `alfred_forget` with 12 and fact 12 is gone

#### Scenario: Facts not in the context
- **WHEN** facts in the context are off
- **THEN** `alfred_forget` is not offered

### Requirement: Fact tools only in chat turns
alfred SHALL NOT offer `alfred_remember` or `alfred_forget` in `/ask` turns or reminder turns.

#### Scenario: Ask turn
- **WHEN** service `disk-check` asks on `diogo`'s behalf
- **THEN** neither fact tool is offered

### Requirement: Facts in the context
When `[memory] facts_in_context` is on, the system prompt of every turn of a user SHALL contain that user's facts, each with its id and origin, after the rest of the system prompt. When it is off, which is the default, no facts SHALL be in any prompt; extraction, `alfred_remember` and `/memory` SHALL work either way.

#### Scenario: Restaurant
- **WHEN** facts in the context are on, `diogo` has the fact "allergic to shellfish", and in another conversation asks for a birthday dinner restaurant
- **THEN** that turn's system prompt contains the shellfish fact

#### Scenario: Flag off
- **WHEN** facts in the context are off and `diogo` has 14 facts
- **THEN** no turn's system prompt contains any of them

#### Scenario: Ask and reminder turns
- **WHEN** facts in the context are on and an `/ask` or reminder turn runs for `diogo`
- **THEN** its system prompt contains `diogo`'s facts

### Requirement: Facts budget
The facts in a system prompt SHALL total at most the configured number of characters. alfred SHALL fill the budget with `told` facts first, then `learned` facts, newest first within each origin, and SHALL list the chosen facts in id order. The facts block SHALL say whether it holds all of the user's facts or how many of how many, and when not all, that the rest are listed by `/memory`.

#### Scenario: All fit
- **WHEN** `diogo`'s 14 facts fit the budget
- **THEN** the block contains all 14 and says it holds all 14

#### Scenario: Over budget
- **WHEN** `diogo` has 52 facts and only 40 fit
- **THEN** the block holds 40, all `told` facts among them, says it shows 40 of 52 and that the rest are in `/memory`, and a WARNING is logged for that turn

### Requirement: List facts with /memory
When a user sends exactly `/memory`, alfred SHALL reply, without the model, with all of that user's facts in id order, each with its id, text, origin and date, or a message saying there are none. Long lists SHALL be split into several messages.

#### Scenario: Listing
- **WHEN** `diogo` sends `/memory` and has 14 facts
- **THEN** he receives all 14 with ids, origins and dates, and no model request is made

#### Scenario: No facts
- **WHEN** `ana` sends `/memory` and has no facts
- **THEN** she receives a message saying alfred knows no facts about her

### Requirement: Forget facts with /memory
When a user sends `/memory forget` followed by one or more fact ids, alfred SHALL remove those of the user's facts and reply, without the model, naming each fact removed and each id not found. Ids of another user's facts SHALL be reported as not found. Any other text after `/memory` SHALL get a reply explaining the two forms.

#### Scenario: Forget two facts
- **WHEN** `diogo` sends `/memory forget 12 15`, both his
- **THEN** facts 12 and 15 are removed and the reply names both

#### Scenario: Someone else's id
- **WHEN** `ana` sends `/memory forget 12` and 12 is `diogo`'s
- **THEN** nothing is removed and the reply says 12 was not found

#### Scenario: Unknown form
- **WHEN** `diogo` sends `/memory clear`
- **THEN** nothing is removed and the reply explains `/memory` and `/memory forget <id>...`

### Requirement: /memory is not part of the conversation
`/memory` commands and their replies SHALL NOT be queued, recorded in the conversation, or counted as the user's last message for idle close.

#### Scenario: Next turn
- **WHEN** `diogo` sends `/memory` and then "what's the weather?"
- **THEN** the model's context for the weather turn contains neither the command nor the list

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
