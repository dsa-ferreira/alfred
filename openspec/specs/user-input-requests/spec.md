# user-input-requests Specification

## Purpose
Lets a turn stop and wait for its user's answer, given with Telegram buttons, before a tool call goes ahead: for tools a user must confirm, and for questions MCP servers ask during a call.

## Requirements

### Requirement: Confirm list
Each user SHALL have a confirm list, in the allow-list syntax (exact names, `prefix*` patterns, `*`). A call on that user's turn to a tool the list matches SHALL NOT run until the user confirms it. The confirm list SHALL NOT offer a tool: a tool that only the confirm list matches is still not offered.

#### Scenario: Listed tool waits
- **WHEN** `ana`'s confirm list has `mcparr_add_media` and the model calls it on her turn
- **THEN** the tool does not run, and `ana` is asked to confirm it

#### Scenario: Unlisted tool runs
- **WHEN** the model calls `mcparr_find_media` on `ana`'s turn and her confirm list doesn't match it
- **THEN** it runs at once, as before

#### Scenario: Another user's turn
- **WHEN** `diogo`'s confirm list is empty and the model calls `mcparr_add_media` on his turn
- **THEN** it runs at once

### Requirement: Waiting turns
A turn that needs its user's answer SHALL stop at that tool call and wait, without a reply. Earlier tool calls in the turn SHALL keep their results. While a turn waits, the queue SHALL go on with other items. A turn SHALL have at most one open request at a time. Until a waiting turn ends, the entries it recorded (such as the user's message that started it) SHALL NOT be in the context of any other turn: they have no reply yet, and another turn must not act on them.

#### Scenario: Others go ahead
- **WHEN** `ana`'s turn waits for her confirmation and `diogo` sends a message
- **THEN** `diogo`'s turn runs and is answered while `ana`'s turn keeps waiting

#### Scenario: Other turns don't take over a waiting request
- **WHEN** `ana`'s turn for "can you try again pls" waits for her confirmation and an `/ask` for her arrives
- **THEN** the `/ask` turn's context doesn't contain "can you try again pls", and once her turn ends, later turns see the message and its reply

#### Scenario: Two calls that need confirmation
- **WHEN** the model asks for two listed tool calls in one reply
- **THEN** the user is asked about the first; the second is asked about only after the first is answered

### Requirement: Question message
When a turn starts waiting, alfred SHALL send its user one message, written by alfred and not by the model, saying what is asked and what asked for it: the user's own request, the service of an `/ask`, or the reminder by number and text. For a confirm-list call it SHALL show the tool's model-facing name and its exact arguments. The message SHALL carry the request's buttons.

#### Scenario: Confirmation shows the real call
- **WHEN** `ana`'s turn waits to call `mcparr_add_media` with `{"title": "Dune", "year": 2021}`
- **THEN** her message names `mcparr_add_media`, shows those arguments, and has the buttons **Do it** and **Cancel**

#### Scenario: Request from a service
- **WHEN** an `/ask` from `jellyfin` makes a turn wait
- **THEN** the message says that `jellyfin` asked

#### Scenario: Long arguments
- **WHEN** the arguments don't fit in one Telegram message
- **THEN** they are cut with a marker saying so, and the buttons are still sent

### Requirement: Answering resumes the turn
A tap on a button of a waiting request SHALL resume its turn from the tool call that waited. For **Do it**, the call SHALL run and its result SHALL go to the model. For **Cancel**, the call SHALL NOT run and the model SHALL be told the user declined it. The model SHALL then carry on, and the turn SHALL end like any other turn of its kind.

#### Scenario: Confirmed
- **WHEN** `ana` taps **Do it** for `mcparr_add_media`
- **THEN** the tool runs, the model gets its result, and `ana` gets the model's answer

#### Scenario: Declined
- **WHEN** `ana` taps **Cancel**
- **THEN** the tool does not run, the model is told she declined, and she gets the model's answer

#### Scenario: Turn goes on after the answer
- **WHEN** the turn was asked to add a film and then set a reminder, and `ana` confirms the add
- **THEN** the model sees the add's result and then calls the reminder tool in the same turn

### Requirement: Question message shows the outcome
When a request is answered, cancelled or expires, alfred SHALL edit its question message to say so and remove its buttons. A failure to edit the message SHALL be logged and SHALL NOT affect the turn.

#### Scenario: After a tap
- **WHEN** `ana` taps **Do it**
- **THEN** the question message says it was confirmed and has no buttons

### Requirement: Only the turn's user answers
A tap SHALL be accepted only from the Telegram user whose turn is waiting, in their private chat with the bot. Any other tap SHALL be refused without effect and logged with the tapper's user ID only.

#### Scenario: Someone else taps
- **WHEN** a tap on `ana`'s request arrives from another Telegram user
- **THEN** the turn stays waiting and nothing runs

### Requirement: An answer is applied once
Each request SHALL be answered at most once. A tap on a request that is already answered, cancelled or expired SHALL do nothing except tell the tapper briefly that it is no longer open.

#### Scenario: Double tap
- **WHEN** `ana` taps **Do it** twice quickly
- **THEN** the tool runs once, and the second tap is told the request was already answered

#### Scenario: Old message
- **WHEN** `ana` taps a button on a request that expired yesterday
- **THEN** nothing runs, and she is told it is no longer open

### Requirement: A new message cancels waiting turns
When a user sends a message that is queued for the AI, every waiting turn of that user SHALL be cancelled before that message's turn runs. A cancelled turn SHALL end without running the call and without a reply. Its question message SHALL say it was cancelled because the user sent a new message.

#### Scenario: User moves on
- **WHEN** `ana`'s turn waits for her confirmation and she sends "never mind, what's downloading?"
- **THEN** the waiting turn ends without running the tool, and only her new message is answered

#### Scenario: Memory command
- **WHEN** `ana` sends `/memory` while a turn waits
- **THEN** the turn keeps waiting

### Requirement: Requests expire
A request SHALL expire after the configured time (`[input_requests] expire_after_min`, default 60), counted from when its question was sent. An expired request's turn SHALL end as a cancelled one does, and its question message SHALL say it expired.

#### Scenario: Nobody answers
- **WHEN** a request has been open for longer than the expiry time
- **THEN** its turn ends without running the call, and the message says it expired

### Requirement: Cancelled turns in the conversation
A waiting turn that ends without an answer SHALL be recorded in the conversation as a reply from alfred, written by alfred and not by the model, that says what was not done and why (cancelled by a new message, or expired). The model SHALL see it in later turns. For a reminder turn, it SHALL be recorded as that reminder's delivered message.

#### Scenario: Later turn knows
- **WHEN** `ana`'s request to add a film was cancelled by her next message
- **THEN** the model on that next turn sees that the film was not added

### Requirement: Waiting is safe across restarts
Waiting turns and their requests SHALL survive a restart of alfred, and their buttons SHALL keep working. A call SHALL be marked as started before it runs. If alfred stops after that mark and before the turn ends, the turn SHALL NOT run the call again: it SHALL fail with the notice that the request may have partly completed.

#### Scenario: Restart while waiting
- **WHEN** alfred restarts while `ana`'s turn waits, and she then taps **Do it**
- **THEN** the turn resumes and the tool runs once

#### Scenario: Crash during the confirmed call
- **WHEN** alfred stops while a confirmed call is running
- **THEN** after the restart the call is not run again, and `ana` is told the request may have partly completed

### Requirement: Server questions as buttons
When an MCP server answers a tool call by asking the user (form elicitation), the turn SHALL wait with the server's message shown, labelled with the server's name. alfred SHALL show it only when the form fits buttons: no fields (buttons **Yes** and **No**), one yes/no field (**Yes**, **No** and **Cancel**), or one field with a list of at most 8 choices (one button each, and **Cancel**). The answer SHALL be sent to the server in the same call, and its result SHALL go to the model.

#### Scenario: Server asks to confirm
- **WHEN** `mcparr_add_media` answers by asking "Add 'Dune' to the library?" with no fields
- **THEN** the user gets that message with **Yes** and **No**, and after **Yes** the call is repeated with the answer and its result goes to the model

#### Scenario: Server offers choices
- **WHEN** a server asks to pick one of "1080p" and "4K"
- **THEN** the user gets a button for each and **Cancel**, and the chosen value is sent to the server

#### Scenario: Server asks again
- **WHEN** the server answers the repeated call with another question
- **THEN** the turn waits again with a new question message

### Requirement: Questions alfred can't show
When a server asks for input that alfred can't show as buttons (text or number fields, more than one field, a URL, several questions at once, or anything other than a form), the call SHALL end with an error result for the model saying so. Nothing SHALL be sent to the user or back to the server.

#### Scenario: Text field
- **WHEN** a server asks for a free-text field
- **THEN** the model gets an error result that alfred can't show that question, and the turn continues

### Requirement: Late answers to a server
When a server refuses a repeated call because it no longer accepts the saved request, the model SHALL get an error result saying the answer could not be used and the tool can be called again. The turn SHALL go on.

#### Scenario: Server restarted while waiting
- **WHEN** `ana` confirms a server's question after that server restarted
- **THEN** the model is told her answer could not be used, and may call the tool again, which asks her again

### Requirement: All turn kinds can wait
Turns started by a user's message, by `/ask` and by a reminder SHALL all be able to wait. The question SHALL go to the turn's user. When such a turn resumes, its answer SHALL be handled as its kind's answer always is, including `NO_REPLY` for `/ask` and quiet reminders.

#### Scenario: Reminder needs confirmation
- **WHEN** a reminder turn for `diogo` calls a tool on his confirm list
- **THEN** `diogo` gets a question message naming the reminder, and after he confirms, the reminder's message is sent as usual
