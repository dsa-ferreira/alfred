# Spec Delta

## MODIFIED Requirements

### Requirement: Accepted attachments
When attachments are configured, alfred SHALL accept from any configured user a Telegram photo, an image sent as a file, and a PDF sent as a file, each with an optional caption. Each SHALL be queued as that user's message whose text is the caption (empty if none), with the file attached, and the attachment SHALL belong to that user.

#### Scenario: Photo with caption
- **WHEN** a user sends a photo captioned "what's the total?"
- **THEN** one message from that user, "what's the total?", with that photo attached is queued for the AI

#### Scenario: PDF without caption
- **WHEN** a user sends `invoice.pdf` as a file with no caption
- **THEN** one message from that user with empty text and that PDF attached is queued for the AI

#### Scenario: Image sent as a file
- **WHEN** a user sends `rack.png` as a file
- **THEN** it is accepted as an image attachment

### Requirement: Viewing an attachment again
In turns started by a user's message on a vision brain, the model SHALL be offered `alfred_view_attachment` when the user's allow-list matches it, taking an attachment number and, for a PDF, a page number. It SHALL return the image, or that PDF page rendered as an image, as the tool result.

#### Scenario: Look again
- **WHEN** attachment 7 is a photo from earlier in the conversation and the user asks "and the date on it?"
- **THEN** the model can call `alfred_view_attachment` with 7, and the next model request contains the image as that call's result

#### Scenario: PDF page
- **WHEN** the model calls `alfred_view_attachment` with attachment 9 and page 4
- **THEN** the result is page 4 of `invoice.pdf` as an image

#### Scenario: Page out of range
- **WHEN** the model asks for page 13 of a 12-page PDF
- **THEN** the result is an error saying the PDF has 12 pages

### Requirement: Reading a document's text
In turns started by a user's message, the model SHALL be offered `alfred_read_document` when the user's allow-list matches it, taking an attachment number and a page range. It SHALL return the text of those pages, each marked with its page number, up to the configured number of characters. When text is cut, the result SHALL say which page to continue from. Pages without text SHALL be marked as having no text.

#### Scenario: Read pages
- **WHEN** the model calls `alfred_read_document` with attachment 9 and pages "3-5"
- **THEN** the result is pages 3 to 5 of `invoice.pdf`, each marked with its page number

#### Scenario: Long range
- **WHEN** the requested pages hold more text than the limit
- **THEN** the result ends where the limit is reached and says which page to read from next

#### Scenario: Not a PDF
- **WHEN** the model calls `alfred_read_document` with an image attachment
- **THEN** the result is an error saying it is an image

### Requirement: Which attachments the tools can reach
The attachment tools SHALL accept only attachments sent in the turn's user's current conversation or in one of their conversations recalled into it. Any other number, including another user's attachment, SHALL return an error saying there is no such attachment in this conversation.

#### Scenario: From a recalled conversation
- **WHEN** conversation #41 had attachment 9 and #41 was recalled into the current conversation
- **THEN** `alfred_read_document` with 9 returns its text

#### Scenario: From an unrelated conversation
- **WHEN** attachment 9 belongs to archived conversation #41, which was not recalled into the current conversation
- **THEN** `alfred_view_attachment` with 9 returns an error saying there is no attachment 9 in this conversation

#### Scenario: Another user's attachment
- **WHEN** on `ana`'s turn the model asks for attachment 9, which `diogo` sent
- **THEN** the result is the same error saying there is no attachment 9 in this conversation

### Requirement: Attachment tools explained
When attachment tools are offered, the system prompt SHALL explain attachment handles, that `alfred_view_attachment` looks at an image or a PDF page again and `alfred_read_document` reads PDF text, and that a handle alone does not show the content. It SHALL describe only the attachment tools offered on the turn.

#### Scenario: Owner turn on a vision brain
- **WHEN** a turn of a user whose allow-list matches both tools runs on a vision brain with attachments configured
- **THEN** the system prompt explains handles and both tools

#### Scenario: Only reading allowed
- **WHEN** the user's allow-list matches `alfred_read_document` but not `alfred_view_attachment`
- **THEN** the system prompt explains handles and `alfred_read_document` only
