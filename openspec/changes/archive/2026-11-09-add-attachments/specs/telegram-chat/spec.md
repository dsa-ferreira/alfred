# Spec Delta

## ADDED Requirements

### Requirement: Supported message types
alfred SHALL handle owner text messages, and, when attachments are configured, photos, image files and PDF files (see `attachments`). For any other owner message type (voice, video, sticker, other files), and for photos and files when attachments are not configured, it SHALL reply with a short fixed notice naming what it can read, and SHALL NOT queue anything.

#### Scenario: Voice message
- **WHEN** the owner sends a voice message and attachments are configured
- **THEN** alfred replies that it can read text, images and PDFs, and the AI is not called

#### Scenario: Photo without attachments configured
- **WHEN** the owner sends a photo and the config has no `[attachments]` section
- **THEN** alfred replies that it can only read text, and the AI is not called

#### Scenario: Spreadsheet file
- **WHEN** the owner sends `budget.xlsx` as a file and attachments are configured
- **THEN** alfred replies that it can read text, images and PDFs, and nothing is queued

## REMOVED Requirements

### Requirement: Text only
**Reason**: alfred now reads images and PDFs when attachments are configured.
**Migration**: Replaced by "Supported message types". Without an `[attachments]` section, alfred keeps the text-only behavior.
