# Proposal

## Why

alfred only reads text. The owner wants to send photos and PDFs (receipts,
invoices, screenshots, letters) and ask several questions about each one.
Both configured brains (`gemma4:26b-a4b-it-qat`, `bonsai-2:27b`) report
vision on `ai.lan`. A spike against `ai.lan` confirmed three things on both
brains. They read an image sent in a user message. They read an image
returned in a tool result in the standard chat-completions form. When
history only shows a handle like `[attachment #7: photo]`, they call a
"look again" tool by themselves.

## What Changes

- **BREAKING**: When attachments are configured, the owner's photos, image
  files and PDFs are accepted instead of getting the "text only" notice.
  Voice, stickers and other files still get a notice, reworded to say what
  is supported. With no `[attachments]` section, alfred stays text-only as
  today.
- Attachment files are stored in an S3-compatible bucket: the homelab's
  Garage. Metadata and the text of each PDF page go in Postgres. Files are
  kept for as long as their conversation, archived conversations included.
- On the turn an attachment arrives, it goes to the model inline. An image
  is sent as an image. A PDF is sent as page 1's text, plus page 1 rendered
  as an image when that page has no text layer. Later turns see a short
  handle (`[attachment #7: …]`), and the model fetches what it needs.
- New tools: `alfred_view_attachment` shows the model an image or a
  rendered PDF page (vision brains only). `alfred_read_document` returns
  PDF page text (every brain). Both accept only attachments from the
  current conversation or from a conversation recalled into it.
- Each brain declares whether it has vision. `[ai.models]` entries may be a
  table `{ model = "…", vision = true }`, and `[ai] vision` covers a startup
  model that no alias names. The default is `false`. A brain without vision
  never receives images: it gets a handle saying it cannot see the image.
- Several photos sent as one Telegram album become one owner message with
  all of them attached.
- Transcripts (summaries and recalls) show the handles, so a recalled
  conversation's attachments can be opened again.
- Orphan objects, left in the bucket by a crash between upload and the
  database write, are deleted in the background.

## Capabilities

### New Capabilities
- `attachments`: images and PDFs from the owner. What is accepted, how
  files are stored and kept, how the model sees them on the arriving turn
  and later turns, the view and read tools and which attachments they can
  reach, and handles in transcripts and recalls.

### Modified Capabilities
- `telegram-chat`: the "Text only" requirement becomes "Supported message
  types": text always, plus photos, image files and PDFs when attachments
  are configured, with notices for everything else.
- `model-selection`: every brain has a vision flag, `false` unless
  configured, and a brain without vision is never sent images.
- `service-configuration`: the new `[attachments]` settings, the S3
  credentials as environment secrets, and the brain list validation
  accepting the table form with `vision`.

## Impact

- Code: `telegram/bot.py` (receiving, albums), `queue.py` and `history.py`
  (linking attachments to the owner entry, loading them with history),
  `agent/turn.py` and `tools/base.py` (image content parts in user and
  tool messages), `agent/context.py` (counting images in the size log),
  `agent/brains.py` and `config.py` (vision flag, `[attachments]`),
  `memory/transcript.py` (handles), `notices.py`, `prompts.py`, a new
  `alfred/attachments/` package (store, PDF, image, tools, sweeper),
  `app.py` (wiring).
- Database: migration `0004` adds `attachments` and `attachment_pages`.
- Dependencies: `aiobotocore` (S3 client), `pypdfium2` (PDF text and
  rendering), `Pillow` (decoding images, resizing and re-encoding them for
  the model).
- Homelab: a Garage bucket and a key limited to it. New secrets
  `ALFRED_S3_ACCESS_KEY_ID` and `ALFRED_S3_SECRET_ACCESS_KEY`. An
  `[attachments]` section and `vision = true` on both brains in the real
  `alfred.toml`. The handover update covers all of this, plus backups,
  which Postgres backups no longer fully cover.
- Not changed: the `/ask` and `/notify` API (text only), images in MCP tool
  results, voice notes and other file types.
