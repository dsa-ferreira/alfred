# Spec Delta

## Purpose

Lets the owner send alfred photos, image files and PDFs and ask about them
over several messages: what is accepted, how the files are kept, and how
the model sees them when they arrive and later.

## ADDED Requirements

### Requirement: Accepted attachments
When attachments are configured, alfred SHALL accept from the owner a Telegram photo, an image sent as a file, and a PDF sent as a file, each with an optional caption. Each SHALL be queued as an owner message whose text is the caption (empty if none), with the file attached.

#### Scenario: Photo with caption
- **WHEN** the owner sends a photo captioned "what's the total?"
- **THEN** one owner message "what's the total?" with that photo attached is queued for the AI

#### Scenario: PDF without caption
- **WHEN** the owner sends `invoice.pdf` as a file with no caption
- **THEN** one owner message with empty text and that PDF attached is queued for the AI

#### Scenario: Image sent as a file
- **WHEN** the owner sends `rack.png` as a file
- **THEN** it is accepted as an image attachment

### Requirement: Albums are one message
Photos and files that Telegram delivers as one album SHALL be queued as a single owner message with all of them attached, in album order, and the album's caption as its text.

#### Scenario: Three photos in one album
- **WHEN** the owner sends three photos as one album captioned "which is cheapest?"
- **THEN** one owner message "which is cheapest?" with the three photos attached is queued, and the AI answers once

### Requirement: Attachment refusals
alfred SHALL refuse a file larger than the configured size limit, an image it cannot decode, a PDF that cannot be opened (damaged or password-protected), and a PDF with no pages. For each it SHALL reply with a short fixed notice that says why, and SHALL queue nothing for that message.

#### Scenario: Too large
- **WHEN** the owner sends a 30 MB PDF and the limit is 20 MB
- **THEN** alfred replies that the file is too large, and nothing is queued

#### Scenario: Password-protected PDF
- **WHEN** the owner sends a PDF that needs a password
- **THEN** alfred replies that it can't open that PDF, and nothing is queued

### Requirement: Storage failure is reported
If an attachment cannot be downloaded from Telegram or stored, alfred SHALL reply with a short fixed notice asking the owner to send it again, and SHALL queue nothing for that message.

#### Scenario: Bucket unreachable
- **WHEN** the owner sends a photo while the object store is unreachable
- **THEN** alfred replies that it couldn't keep the file and asks for it again, and nothing is queued

### Requirement: Attachments are kept with their conversation
An attachment's file and its stored text SHALL be kept as long as the conversation it was sent in, including after that conversation is archived. Ending a conversation SHALL NOT delete its attachments.

#### Scenario: After a reset
- **WHEN** the owner sent `invoice.pdf` in conversation #41 and then ended that conversation
- **THEN** the PDF's file and page text are still stored

### Requirement: Attachment handles
Each attachment SHALL have a number, and the model SHALL see it as a handle with that number and what it is: an image with its file name if any, or a PDF with its file name, page count and whether it has a text layer.

#### Scenario: PDF handle
- **WHEN** the owner sent `invoice.pdf`, a 12-page PDF with text, stored as attachment 9
- **THEN** the model sees a handle such as `[attachment #9: PDF "invoice.pdf", 12 pages, text]`

### Requirement: Attachments inline on the turn they arrive
On a vision brain, the attachments of the configured number of most recent owner messages (default 1: only the message being answered) SHALL be sent to the model with that message. An image SHALL be sent as an image. A PDF SHALL be sent as its handle and up to the configured number of characters of page 1's text, plus page 1 as an image when page 1 has no text.

#### Scenario: Photo question
- **WHEN** the owner sends a photo of a receipt captioned "what's the total?"
- **THEN** that turn's model request contains the caption, the handle and the image

#### Scenario: Text PDF
- **WHEN** the owner sends a 12-page PDF whose page 1 has text
- **THEN** that turn's model request contains the handle and page 1's text, and no page image

#### Scenario: Scanned PDF
- **WHEN** the owner sends a scanned PDF whose page 1 has no text
- **THEN** that turn's model request contains the handle and page 1 as an image

### Requirement: Older attachments are handles
Attachments of owner messages outside the inline window SHALL be sent to the model only as their handles, in the owner message they belong to.

#### Scenario: Follow-up question
- **WHEN** the owner sent a photo as attachment 7, and their next message is "and the date on it?"
- **THEN** that turn's model request contains attachment 7 only as its handle, not the image

### Requirement: Viewing an attachment again
In owner turns on a vision brain, the model SHALL be offered `alfred_view_attachment`, taking an attachment number and, for a PDF, a page number. It SHALL return the image, or that PDF page rendered as an image, as the tool result.

#### Scenario: Look again
- **WHEN** attachment 7 is a photo from earlier in the conversation and the owner asks "and the date on it?"
- **THEN** the model can call `alfred_view_attachment` with 7, and the next model request contains the image as that call's result

#### Scenario: PDF page
- **WHEN** the model calls `alfred_view_attachment` with attachment 9 and page 4
- **THEN** the result is page 4 of `invoice.pdf` as an image

#### Scenario: Page out of range
- **WHEN** the model asks for page 13 of a 12-page PDF
- **THEN** the result is an error saying the PDF has 12 pages

### Requirement: Reading a document's text
In owner turns, the model SHALL be offered `alfred_read_document`, taking an attachment number and a page range. It SHALL return the text of those pages, each marked with its page number, up to the configured number of characters. When text is cut, the result SHALL say which page to continue from. Pages without text SHALL be marked as having no text.

#### Scenario: Read pages
- **WHEN** the model calls `alfred_read_document` with attachment 9 and pages "3-5"
- **THEN** the result is pages 3 to 5 of `invoice.pdf`, each marked with its page number

#### Scenario: Long range
- **WHEN** the requested pages hold more text than the limit
- **THEN** the result ends where the limit is reached and says which page to read from next

#### Scenario: Not a PDF
- **WHEN** the model calls `alfred_read_document` with an image attachment
- **THEN** the result is an error saying it is an image

### Requirement: Attachment results are sized by their own limits
Results of `alfred_read_document` SHALL be bounded by the attachments read limit, not by the global tool result limit. An image in a tool result SHALL NOT count toward any character limit.

#### Scenario: Larger than the global limit
- **WHEN** the read limit is 8000 characters, the global tool result limit is 4000, and the pages hold 6000 characters
- **THEN** the model receives all 6000 characters

### Requirement: Which attachments the tools can reach
The attachment tools SHALL accept only attachments sent in the current conversation or in a conversation recalled into it. Any other number SHALL return an error saying there is no such attachment in this conversation.

#### Scenario: From a recalled conversation
- **WHEN** conversation #41 had attachment 9 and #41 was recalled into the current conversation
- **THEN** `alfred_read_document` with 9 returns its text

#### Scenario: From an unrelated conversation
- **WHEN** attachment 9 belongs to archived conversation #41, which was not recalled into the current conversation
- **THEN** `alfred_view_attachment` with 9 returns an error saying there is no attachment 9 in this conversation

### Requirement: Images reach the model in a model-friendly form
Images sent to the model, both attachments and rendered PDF pages, SHALL be scaled down so their longer side is at most the configured number of pixels, turned upright per their orientation metadata, and sent in a format the model server accepts. The stored file SHALL stay unchanged.

#### Scenario: Large photo
- **WHEN** the owner sends a 4000x3000 image file and the limit is 1536 pixels
- **THEN** the model receives it at 1536x1152, and the stored file is still 4000x3000

### Requirement: Unavailable files don't fail the turn
If an attachment's file can't be fetched from the store when it is due to be sent inline, the model SHALL receive its handle marked as not available right now, and the turn SHALL continue. If it can't be fetched by `alfred_view_attachment`, the tool result SHALL be an error saying so.

#### Scenario: Store down on a follow-up
- **WHEN** the object store is unreachable and the model calls `alfred_view_attachment` with 7
- **THEN** the model receives an error saying the file isn't available right now, and can tell the owner

### Requirement: Attachments in transcripts
Transcripts used for summaries and recalls SHALL show each owner message's attachment handles with the message's text, and SHALL NOT contain image data or PDF text.

#### Scenario: Recall shows handles
- **WHEN** conversation #41, in which the owner sent `invoice.pdf` as attachment 9 captioned "file this", is recalled
- **THEN** the recalled transcript has the owner line with "file this" and the handle for attachment 9

### Requirement: Attachment tools explained
When attachment tools are offered, the system prompt SHALL explain attachment handles, that `alfred_view_attachment` looks at an image or a PDF page again and `alfred_read_document` reads PDF text, and that a handle alone does not show the content.

#### Scenario: Owner turn on a vision brain
- **WHEN** an owner turn runs on a vision brain with attachments configured
- **THEN** the system prompt explains handles and both tools

### Requirement: Orphan files are removed
A stored file that no attachment refers to, older than one day, SHALL be deleted from the object store in the background. This SHALL NOT delay or fail any turn.

#### Scenario: Crash between upload and record
- **WHEN** a file was uploaded but alfred stopped before recording its attachment, and a day has passed
- **THEN** the file is deleted from the bucket, and no attachment is affected
